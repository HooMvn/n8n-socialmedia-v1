"""Product pipeline services: WooCommerce webhook -> snapshot -> job -> AI draft -> publish -> notify.

Design decisions (mine, informed by the prior Claude session in text.md):
- The API never talks to Telegram/WhatsApp/Bale/Rubika directly. It records intents as
  OutboxEvents; the n8n worker fans them out to channels and reports per-channel results back.
- Every external interaction is idempotent: webhooks dedupe on source_hash, jobs on
  idempotency_key, notifications on their own deterministic key.
- Product content lives in immutable ContentVersion rows; ContentItem.title/body are a
  mirror written only through ``add_content_version`` (enforced by ORM guard in app.domain).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import (
    CONTENT_KIND_PRODUCT,
    ContentItem,
    ContentStatus,
    ContentVersion,
    ContentVersionSource,
    OutboxEvent,
    OutboxStatus,
    Publication,
    PublicationStatus,
)
from app.notify_domain import (
    NotificationChannel,
    NotifyStatus,
    ProductNotification,
    ProductNotificationDelivery,
)
from app.product_domain import (
    ALLOWED_JOB_TRANSITIONS,
    PROCESSING_JOB_STATES,
    MAX_SNAPSHOT_BYTES,
    S,
    ProductContentJob,
    ProductJobStatus,
    ProductSnapshot,
)
from app.settings import settings

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Normalization / hashing
# --------------------------------------------------------------------------- #
def strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_product_payload(raw: dict) -> dict:
    """Whitelist + canonicalize the WooCommerce webhook body. Deterministic output."""
    images = []
    for img in raw.get("images") or []:
        if isinstance(img, dict) and img.get("src"):
            images.append(
                {
                    "src": str(img["src"])[:1000],
                    "alt": strip_html(str(img.get("alt") or ""))[:200],
                }
            )
    categories = [
        {"id": c.get("id"), "name": strip_html(str(c.get("name") or ""))[:120]}
        for c in raw.get("categories") or []
        if isinstance(c, dict)
    ]
    tags = [
        {"id": t.get("id"), "name": strip_html(str(t.get("name") or ""))[:120]}
        for t in raw.get("tags") or []
        if isinstance(t, dict)
    ]
    meta = [
        {"key": str(m.get("key") or "")[:120], "value": str(m.get("value") or "")[:2000]}
        for m in raw.get("meta_data") or []
        if isinstance(m, dict)
    ]
    price = raw.get("price")
    normalized = {
        "external_product_id": str(raw.get("id")),
        "name": strip_html(str(raw.get("name") or ""))[:500],
        "slug": str(raw.get("slug") or "")[:200],
        "type": str(raw.get("type") or "simple")[:40],
        "status": str(raw.get("status") or "")[:40],
        "permalink": str(raw.get("permalink") or "")[:1000],
        "sku": str(raw.get("sku") or "")[:120],
        "price": str(price) if price not in (None, "") else None,
        "regular_price": str(raw.get("regular_price")) if raw.get("regular_price") not in (None, "") else None,
        "sale_price": str(raw.get("sale_price")) if raw.get("sale_price") not in (None, "") else None,
        "description": strip_html(str(raw.get("description") or ""))[:20000],
        "short_description": strip_html(str(raw.get("short_description") or ""))[:4000],
        "categories": categories[:30],
        "tags": tags[:50],
        "images": images[:10],
        "meta_data": meta[:50],
    }
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    # Per-field clamping keeps ordinary payloads small; anything this large means the
    # product carries abnormally huge blobs (e.g. meta_data) and must be rejected.
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise HTTPException(status_code=413, detail="Product payload exceeds snapshot size limit")
    return normalized


def canonical_hash(normalized: dict) -> str:
    blob = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def version_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# --------------------------------------------------------------------------- #
# Outbox helper
# --------------------------------------------------------------------------- #
async def enqueue_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    idempotency_key: str,
    payload: dict,
) -> OutboxEvent | None:
    """Insert an outbox intent unless one with the same (workspace, key) already exists."""
    existing = await session.execute(
        select(OutboxEvent).where(
            OutboxEvent.workspace_id == workspace_id,
            OutboxEvent.idempotency_key == idempotency_key,
        )
    )
    if existing.scalar_one_or_none():
        return None
    event = OutboxEvent(
        workspace_id=workspace_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        idempotency_key=idempotency_key,
        payload_json=json.dumps(payload, ensure_ascii=False),
        status=OutboxStatus.pending,
    )
    session.add(event)
    await session.flush()
    return event


def _event_ref(event: OutboxEvent | None) -> dict | None:
    if event is None:
        return None
    return {
        "event_id": event.id,
        "event_type": event.event_type,
        "payload": json.loads(event.payload_json),
    }


# --------------------------------------------------------------------------- #
# Webhook ingestion
# --------------------------------------------------------------------------- #
async def _default_brand_id(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    from app.domain import BrandProfile

    result = await session.execute(
        select(BrandProfile).where(BrandProfile.workspace_id == workspace_id).limit(1)
    )
    brand = result.scalar_one_or_none()
    if brand is None:
        raise HTTPException(
            status_code=409,
            detail="Workspace has no brand profile; create one before ingesting products",
        )
    return brand.id


async def _resolve_woo_credential(session: AsyncSession, workspace_id: uuid.UUID) -> uuid.UUID:
    from app.hardening_domain import ProviderCredential, ProviderCredentialStatus

    result = await session.execute(
        select(ProviderCredential)
        .where(
            ProviderCredential.workspace_id == workspace_id,
            ProviderCredential.provider.in_(("woocommerce", "wordpress")),
            ProviderCredential.status == ProviderCredentialStatus.active,
        )
        .order_by(ProviderCredential.created_at.asc())
        .limit(1)
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        raise HTTPException(
            status_code=409,
            detail="No active woocommerce/wordpress provider credential for this workspace",
        )
    return credential.id


async def ingest_woocommerce_webhook(
    session: AsyncSession, workspace_id: uuid.UUID, raw: dict
) -> dict:
    credential_id = await _resolve_woo_credential(session, workspace_id)
    normalized = normalize_product_payload(raw)
    if not normalized["external_product_id"] or normalized["external_product_id"] == "None":
        raise HTTPException(status_code=422, detail="Webhook body must include product id")
    source_hash = canonical_hash(normalized)

    existing = await session.execute(
        select(ProductSnapshot).where(
            ProductSnapshot.workspace_id == workspace_id,
            ProductSnapshot.credential_id == credential_id,
            ProductSnapshot.external_product_id == normalized["external_product_id"],
            ProductSnapshot.source_hash == source_hash,
        )
    )
    snapshot = existing.scalar_one_or_none()
    duplicate = snapshot is not None
    if snapshot is None:
        snapshot = ProductSnapshot(
            workspace_id=workspace_id,
            credential_id=credential_id,
            external_product_id=normalized["external_product_id"],
            source_hash=source_hash,
            data=normalized,
            fetched_at=utcnow(),
        )
        session.add(snapshot)
        await session.flush()

    job_result = await session.execute(
        select(ProductContentJob).where(
            ProductContentJob.workspace_id == workspace_id,
            ProductContentJob.snapshot_id == snapshot.id,
        )
    )
    job = job_result.scalars().first()

    events: list[dict] = []
    if job is None and not duplicate:
        brand_id = await _default_brand_id(session, workspace_id)
        content = ContentItem(
            workspace_id=workspace_id,
            brand_id=brand_id,
            platform="wordpress",
            title=normalized["name"],
            body="",
            status=ContentStatus.draft,
            kind=CONTENT_KIND_PRODUCT,
        )
        session.add(content)
        await session.flush()
        idem = f"product-job:{snapshot.id}"
        job = ProductContentJob(
            workspace_id=workspace_id,
            snapshot_id=snapshot.id,
            content_id=content.id,
            status=S.queued,
            max_revisions=settings.product_max_revisions,
            idempotency_key=idem,
        )
        session.add(job)
        await session.flush()
        event = await enqueue_event(
            session,
            workspace_id=workspace_id,
            event_type="product.content.generate.requested",
            aggregate_type="product_content_job",
            aggregate_id=job.id,
            idempotency_key=f"outbox:{idem}",
            payload={
                "job_id": str(job.id),
                "content_id": str(content.id),
                "snapshot_id": str(snapshot.id),
                "workspace_id": str(workspace_id),
            },
        )
        ref = _event_ref(event)
        if ref:
            events.append(ref)

    await session.commit()
    await session.refresh(snapshot)
    if job is not None:
        await session.refresh(job)

    return {
        "duplicate": duplicate,
        "snapshot": snapshot,
        "job": job,
        "events": events,
    }


# --------------------------------------------------------------------------- #
# Job state machine
# --------------------------------------------------------------------------- #
async def get_job(session: AsyncSession, workspace_id: uuid.UUID, job_id: uuid.UUID) -> ProductContentJob:
    job = await session.get(ProductContentJob, job_id)
    if job is None or job.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Product job not found in workspace")
    return job


async def transition_job(
    session: AsyncSession,
    job: ProductContentJob,
    target: ProductJobStatus,
    *,
    error_code: str | None = None,
    last_error: str | None = None,
) -> ProductContentJob:
    current = job.status if isinstance(job.status, ProductJobStatus) else ProductJobStatus(job.status)
    if target not in ALLOWED_JOB_TRANSITIONS[current]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Invalid job transition: {current.value} -> {target.value}",
        )
    if target in PROCESSING_JOB_STATES:
        job.attempts += 1
        if job.attempts > settings.product_job_max_attempts:
            job.status = S.needs_attention
            job.error_code = "attempts_exhausted"
            job.last_error = f"Job exceeded {settings.product_job_max_attempts} attempts"
            await session.commit()
            await session.refresh(job)
            return job
    job.status = target
    if error_code is not None:
        job.error_code = error_code
    if last_error is not None:
        job.last_error = last_error[:2000]
    if target in (S.failed, S.cancelled):
        job.error_code = error_code or job.error_code
    await session.commit()
    await session.refresh(job)
    return job


# --------------------------------------------------------------------------- #
# Content versions (immutable) + mirror
# --------------------------------------------------------------------------- #
async def add_content_version(
    session: AsyncSession,
    *,
    job: ProductContentJob,
    payload: dict,
    source: ContentVersionSource,
    model: str | None,
    prompt_version: str | None,
    created_by_user_id: uuid.UUID | None = None,
) -> ContentVersion:
    content = await session.get(ContentItem, job.content_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Content item missing for job")
    next_no = (
        await session.execute(
            select(func.max(ContentVersion.version_no)).where(
                ContentVersion.content_id == content.id
            )
        )
    ).scalar() or 0
    version = ContentVersion(
        workspace_id=job.workspace_id,
        content_id=content.id,
        version_no=next_no + 1,
        source=source,
        payload=payload,
        content_hash=version_hash(payload),
        model=model,
        prompt_version=prompt_version,
        created_by_user_id=created_by_user_id,
    )
    session.add(version)
    await session.flush()

    # Mirror the version onto the ContentItem (only allowed path for product title/body).
    content._mirror_write_ok = True
    content.title = str(payload.get("title") or content.title)[:500]
    # Mirror the blog article so the existing review/publish UI shows product content.
    content.body = str(payload.get("body_markdown") or payload.get("body") or "")
    content.current_version_id = version.id
    await session.commit()
    await session.refresh(version)
    return version


# --------------------------------------------------------------------------- #
# Draft generation (openai-compatible chat completion, grounded on snapshot)
# --------------------------------------------------------------------------- #
PROMPT_VERSION = "product-blog-v1"


def build_messages(snapshot_ dict, instruction: str) -> list[dict]:
    system = (
        "You are a Persian content writer for an online shop blog. "
        "Write ONLY based on the provided product data (grounding); never invent specs, prices or claims. "
        "Return strict JSON: {\"title\": str, \"summary\": str (<=280 chars, for messaging), "
        "\"body_markdown\": str (a full Persian blog post in markdown), "
        "\"hashtags\": [str,...] (max 8)}."
    )
    user = {
        "product": snapshot_data,
        "extra_instruction": instruction,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


class AiUnavailableError(RuntimeError):
    pass


async def call_ai(messages: list[dict]) -> tuple[dict, str]:
    """Returns (parsed_json, model). Raises AiUnavailableError on any provider failure."""
    if not settings.ai_api_base_url or not settings.ai_api_key or not settings.ai_model:
        raise AiUnavailableError("AI provider is not configured")
    import httpx

    url = settings.ai_api_base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                json={
                    "model": settings.ai_model,
                    "messages": messages,
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("AI response is not a JSON object")
        return parsed, str(data.get("model") or settings.ai_model)
    except Exception as exc:  # noqa: BLE001 - provider boundary: normalize everything
        raise AiUnavailableError(f"AI generation failed: {exc}") from exc


def validate_draft(snapshot_ dict, draft: dict) -> dict:
    """Deterministic validation of AI output against the snapshot (no AI judging AI)."""
    errors: list[str] = []
    warnings: list[str] = []
    title = str(draft.get("title") or "").strip()
    summary = str(draft.get("summary") or "").strip()
    body = str(draft.get("body_markdown") or "").strip()
    if not title:
        errors.append("missing_title")
    if len(summary) < 20:
        errors.append("summary_too_short")
    if len(summary) > 600:
        errors.append("summary_too_long")
    if len(body) < 100:
        errors.append("body_too_short")
    hashtags = draft.get("hashtags")
    if not isinstance(hashtags, list):
        warnings.append("hashtags_missing")
        hashtags = []
    # Grounding check: every numeric-looking token in the summary should exist in source data.
    source_blob = json.dumps(snapshot_data, ensure_ascii=False)
    numbers = set(re.findall(r"\d[\d٬.,]*", summary))
    ungrounded = [n for n in numbers if n.replace("٬", ",").replace("،", ",") not in source_blob]
    if len(ungrounded) > 2:
        warnings.append("possible_ungrounded_numbers")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized_hashtags": [str(h)[:60] for h in hashtags][:8],
    }


async def generate_draft_for_job(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    instruction: str = "",
    created_by_user_id: uuid.UUID | None = None,
) -> dict:
    job = await get_job(session, workspace_id, job_id)
    current = job.status if isinstance(job.status, ProductJobStatus) else ProductJobStatus(job.status)
    if current not in (S.queued, S.validating, S.quality_review, S.revising):
        # Already drafted or terminal: idempotent no-op returning latest version.
        latest = await session.execute(
            select(ContentVersion)
            .where(ContentVersion.content_id == job.content_id)
            .order_by(ContentVersion.version_no.desc())
            .limit(1)
        )
        version = latest.scalar_one_or_none()
        if version is None:
            raise HTTPException(
                status_code=409,
                detail=f"Job in state {current.value} cannot be drafted",
            )
        return {"job": job, "version": version, "events": [], "skipped": True}

    snapshot = await session.get(ProductSnapshot, job.snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=409, detail="Snapshot missing for job")

    is_revision = current in (S.validating, S.quality_review, S.revising) or job.revision_count > 0
    if is_revision and job.revision_count >= job.max_revisions:
        await transition_job(session, job, S.needs_attention, error_code="revision_limit")
        raise HTTPException(status_code=409, detail="Revision limit reached; needs human attention")

    await transition_job(session, job, S.generating)

    source = ContentVersionSource.ai_revision if is_revision else ContentVersionSource.ai_generation
    try:
        draft, model = await call_ai(build_messages(snapshot.data, instruction))
    except AiUnavailableError as exc:
        job = await session.get(ProductContentJob, job_id)
        await transition_job(session, job, S.failed, error_code="ai_unavailable", last_error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc))

    report = validate_draft(snapshot.data, draft)
    payload = {
        "title": str(draft.get("title") or snapshot.data["name"])[:500],
        "summary": str(draft.get("summary") or "")[:600],
        "body_markdown": str(draft.get("body_markdown") or ""),
        "hashtags": report["normalized_hashtags"],
        "product_url": snapshot.data.get("permalink") or "",
        "image": (snapshot.data.get("images") or [{}])[0].get("src", ""),
    }
    version = await add_content_version(
        session,
        job=job,
        payload=payload,
        source=source,
        model=model,
        prompt_version=PROMPT_VERSION,
        created_by_user_id=created_by_user_id,
    )
    # Attach write-once validation report.
    version.validation_report = report
    await session.commit()
    await session.refresh(version)

    job = await get_job(session, workspace_id, job_id)
    if not report["ok"]:
        if job.revision_count < job.max_revisions:
            job.revision_count += 1
            await transition_job(session, job, S.revising)
            await transition_job(session, job, S.validating)
            # Re-draft once automatically with the errors echoed back.
            retry_instruction = (
                instruction
                + "\nPrevious draft was rejected by validation: "
                + ", ".join(report["errors"])
                + ". Fix these issues."
            )
            return await generate_draft_for_job(
                session,
                workspace_id=workspace_id,
                job_id=job_id,
                instruction=retry_instruction,
                created_by_user_id=created_by_user_id,
            )
        await transition_job(session, job, S.needs_attention, error_code="validation_failed")
        return {"job": job, "version": version, "events": [], "auto_publish": False}

    await transition_job(session, job, S.quality_review)
    await transition_job(session, job, S.ready_for_review)

    events: list[dict] = []
    if settings.product_auto_publish:
        result = await publish_product_content(session, workspace_id=workspace_id, job_id=job_id)
        events = result["events"]
    return {"job": job, "version": version, "events": events, "auto_publish": bool(settings.product_auto_publish)}


# --------------------------------------------------------------------------- #
# Publish (WordPress via outbox) then fan-out notification
# --------------------------------------------------------------------------- #
def render_notification_message(payload: dict, links: dict) -> str:
    parts = [
        f"🛍 {payload.get('title', '')}",
        "",
        payload.get("summary", ""),
    ]
    if payload.get("hashtags"):
        parts.append("")
        parts.append(" ".join(f"#{h.lstrip('#')}" for h in payload["hashtags"]))
    parts.append("")
    blog_url = links.get("blog_url") or ""
    shop_url = links.get("product_url") or payload.get("product_url") or ""
    if blog_url:
        parts.append(f"📄 مطلب کامل: {blog_url}")
    if shop_url:
        parts.append(f"🛒 خرید: {shop_url}")
    return "\n".join(parts).strip()


async def publish_product_content(session: AsyncSession, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> dict:
    job = await get_job(session, workspace_id, job_id)
    current = job.status if isinstance(job.status, ProductJobStatus) else ProductJobStatus(job.status)
    events: list[dict] = []

    if current is S.completed:
        publication = await session.execute(
            select(Publication).where(
                Publication.workspace_id == workspace_id,
                Publication.idempotency_key == job.publication_idempotency_key(),
            )
        )
        pub = publication.scalar_one_or_none()
        return {"job": job, "publication": pub, "events": [], "skipped": True}
    if current is not S.ready_for_review:
        raise HTTPException(
            status_code=409,
            detail=f"Only ready_for_review jobs can be published (state: {current.value})",
        )

    content = await session.get(ContentItem, job.content_id)
    version = await session.get(ContentVersion, content.current_version_id)
    snapshot = await session.get(ProductSnapshot, job.snapshot_id)

    # Keep the review ledger honest: record an automated approval decision.
    from app.domain import Approval

    content.status = ContentStatus.approved
    session.add(
        Approval(
            workspace_id=workspace_id,
            content_id=content.id,
            decision="approved",
            actor="product-pipeline",
            note=f"Auto-approved after validation (job {job.id}, version {version.id})",
            content_version_id=version.id,
        )
    )
    await session.flush()

    publication = Publication(
        workspace_id=workspace_id,
        content_id=content.id,
        platform="wordpress",
        idempotency_key=job.publication_idempotency_key(),
        status=PublicationStatus.pending,
    )
    session.add(publication)
    await session.flush()

    wp_event = await enqueue_event(
        session,
        workspace_id=workspace_id,
        event_type="product.wordpress.publish.requested",
        aggregate_type="publication",
        aggregate_id=publication.id,
        idempotency_key=f"outbox:{publication.idempotency_key}",
        payload={
            "publication_id": str(publication.id),
            "content_id": str(content.id),
            "job_id": str(job.id),
            "workspace_id": str(workspace_id),
            "title": version.payload.get("title"),
            "body_markdown": version.payload.get("body_markdown"),
            "excerpt": version.payload.get("summary"),
            "image": version.payload.get("image"),
            "product_external_id": snapshot.external_product_id,
            "idempotency_key": publication.idempotency_key,
        },
    )
    ref = _event_ref(wp_event)
    if ref:
        events.append(ref)
    content.status = ContentStatus.publish_requested
    await session.commit()
    await session.refresh(publication)
    return {"job": job, "publication": publication, "events": events, "skipped": False}


async def apply_publish_callback(session: AsyncSession, *, workspace_id: uuid.UUID, payload) -> dict:
    publication = await session.get(Publication, payload.publication_id)
    if publication is None or publication.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Publication not found in workspace")
    if publication.idempotency_key != payload.idempotency_key:
        raise HTTPException(status_code=409, detail="Idempotency key mismatch")

    desired = PublicationStatus(payload.status)
    events: list[dict] = []
    if publication.status is desired:
        job = await _job_for_content(session, workspace_id, publication.content_id)
        return {"job": job, "publication": publication, "events": [], "skipped": True}
    if publication.status in (PublicationStatus.published, PublicationStatus.failed):
        raise HTTPException(status_code=409, detail="Publication already terminal")

    publication.status = desired
    publication.external_id = payload.external_id
    publication.external_url = payload.external_url
    publication.error_code = payload.error_code
    publication.error_message = payload.error_message

    content = await session.get(ContentItem, publication.content_id)
    job = await _job_for_content(session, workspace_id, publication.content_id)

    if desired is PublicationStatus.published:
        content.status = ContentStatus.published
        notify_event = await _create_fanout_notification(session, job=job, content=content, publication=publication)
        ref = _event_ref(notify_event)
        if ref:
            events.append(ref)
        await transition_job(session, job, S.completed)
    else:
        content.status = ContentStatus.failed
        await transition_job(
            session, job, S.failed, error_code=payload.error_code, last_error=payload.error_message
        )
    await session.commit()
    return {"job": job, "publication": publication, "events": events, "skipped": False}


async def _job_for_content(session: AsyncSession, workspace_id: uuid.UUID, content_id: uuid.UUID) -> ProductContentJob:
    result = await session.execute(
        select(ProductContentJob).where(
            ProductContentJob.workspace_id == workspace_id,
            ProductContentJob.content_id == content_id,
        )
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="No product job for this content")
    return job


async def _create_fanout_notification(
    session: AsyncSession,
    *,
    job: ProductContentJob,
    content: ContentItem,
    publication: Publication,
) -> OutboxEvent | None:
    version = await session.get(ContentVersion, content.current_version_id)
    links = {"blog_url": publication.external_url or "", "product_url": (version.payload.get("product_url") if version else "") or ""}
    message = render_notification_message(version.payload if version else {}, links)

    idem = job.notify_idempotency_key()
    existing = await session.execute(
        select(ProductNotification).where(
            ProductNotification.workspace_id == job.workspace_id,
            ProductNotification.idempotency_key == idem,
        )
    )
    notification = existing.scalar_one_or_none()
    if notification is not None:
        return None  # already fanned out once

    channels_result = await session.execute(
        select(NotificationChannel).where(
            NotificationChannel.workspace_id == job.workspace_id,
            NotificationChannel.enabled.is_(True),
        )
    )
    channels = list(channels_result.scalars().all())

    notification = ProductNotification(
        workspace_id=job.workspace_id,
        content_id=content.id,
        job_id=job.id,
        message=message,
        links_json=links,
        idempotency_key=idem,
        status=NotifyStatus.pending if channels else NotifyStatus.skipped,
    )
    session.add(notification)
    await session.flush()

    fanout_targets = []
    for channel in channels:
        delivery = ProductNotificationDelivery(
            workspace_id=job.workspace_id,
            notification_id=notification.id,
            channel=channel.channel,
            target_ref=channel.target_ref,
            status=NotifyStatus.pending,
        )
        session.add(delivery)
        fanout_targets.append(
            {
                "channel": channel.channel,
                "target_ref": channel.target_ref,
                "template_key": channel.template_key,
            }
        )
    await session.flush()

    if not fanout_targets:
        return None
    return await enqueue_event(
        session,
        workspace_id=job.workspace_id,
        event_type="product.notify.fanout.requested",
        aggregate_type="product_notification",
        aggregate_id=notification.id,
        idempotency_key=f"outbox:{idem}",
        payload={
            "notification_id": str(notification.id),
            "idempotency_key": idem,
            "workspace_id": str(job.workspace_id),
            "message": message,
            "links": links,
            "targets": fanout_targets,
        },
    )


async def ack_notification_delivery(session: AsyncSession, *, workspace_id: uuid.UUID, payload) -> dict:
    notification = await session.get(ProductNotification, payload.notification_id)
    if notification is None or notification.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Notification not found in workspace")
    if notification.idempotency_key != payload.idempotency_key:
        raise HTTPException(status_code=409, detail="Idempotency key mismatch")

    result = await session.execute(
        select(ProductNotificationDelivery).where(
            ProductNotificationDelivery.notification_id == notification.id,
            ProductNotificationDelivery.channel == payload.channel.lower(),
            ProductNotificationDelivery.target_ref == payload.target_ref,
        )
    )
    delivery = result.scalar_one_or_none()
    if delivery is None:
        raise HTTPException(status_code=404, detail="Unknown channel/target for this notification")

    delivery.attempts += 1
    if payload.delivered:
        delivery.status = NotifyStatus.sent
        delivery.external_message_id = payload.external_message_id
        delivery.delivered_at = utcnow()
        delivery.error_code = None
        delivery.last_error = None
    else:
        delivery.status = NotifyStatus.failed
        delivery.error_code = payload.error_code
        delivery.last_error = (payload.error_message or "")[:2000]

    # Roll up aggregate status: sent only when every delivery succeeded.
    all_result = await session.execute(
        select(ProductNotificationDelivery.status).where(
            ProductNotificationDelivery.notification_id == notification.id
        )
    )
    statuses = [row[0] for row in all_result]
    if all(s == NotifyStatus.sent for s in statuses):
        notification.status = NotifyStatus.sent
    elif any(s == NotifyStatus.failed for s in statuses):
        notification.status = NotifyStatus.failed
    await session.commit()
    await session.refresh(delivery)
    return {"delivery": delivery, "notification_status": notification.status.value}


# --------------------------------------------------------------------------- #
# Channels CRUD
# --------------------------------------------------------------------------- #
async def list_channels(session: AsyncSession, workspace_id: uuid.UUID) -> list[NotificationChannel]:
    result = await session.execute(
        select(NotificationChannel)
        .where(NotificationChannel.workspace_id == workspace_id)
        .order_by(NotificationChannel.created_at.asc())
    )
    return list(result.scalars().all())


async def create_channel(session: AsyncSession, workspace_id: uuid.UUID, payload) -> NotificationChannel:
    if payload.credential_id is not None:
        from app.hardening_domain import ProviderCredential

        cred = await session.get(ProviderCredential, payload.credential_id)
        if cred is None or cred.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Credential not found in workspace")
    channel = NotificationChannel(
        workspace_id=workspace_id,
        channel=payload.channel,
        target_ref=payload.target_ref,
        display_name=payload.display_name,
        credential_id=payload.credential_id,
        template_key=payload.template_key,
    )
    session.add(channel)
    try:
        await session.commit()
    except Exception as exc:  # unique violation => duplicate destination
        await session.rollback()
        raise HTTPException(status_code=409, detail="Channel/target already registered") from exc
    await session.refresh(channel)
    return channel


async def toggle_channel(
    session: AsyncSession, workspace_id: uuid.UUID, channel_id: uuid.UUID, enabled: bool
) -> NotificationChannel:
    channel = await session.get(NotificationChannel, channel_id)
    if channel is None or channel.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Channel not found in workspace")
    channel.enabled = enabled
    await session.commit()
    await session.refresh(channel)
    return channel


async def list_notifications(session: AsyncSession, workspace_id: uuid.UUID, job_id: uuid.UUID) -> list[ProductNotification]:
    result = await session.execute(
        select(ProductNotification)
        .where(
            ProductNotification.workspace_id == workspace_id,
            ProductNotification.job_id == job_id,
        )
        .order_by(ProductNotification.created_at.asc())
    )
    return list(result.scalars().all())
