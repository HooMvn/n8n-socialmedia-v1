"""HTTP surface of the WooCommerce -> Blog -> Notify pipeline.

Two auth planes:
- Human/admin endpoints: JWT + workspace role (same as the rest of the API).
- Machine endpoints (WooCommerce webhook, n8n callbacks): shared secrets in headers,
  never JWT. WooCommerce delivers its own X-WC-Webhook-Signature too; we accept our
  own secret via X-Webhook-Secret (set in the WooCommerce webhook "secret" field) and
  also validate the WC HMAC signature when the delivery includes it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_optional_user, require_roles, require_workspace_member
from app.database import get_session
from app.domain import Membership, User, WorkspaceRole
from app.product_schemas import (
    DraftGenerateIn,
    DraftGenerateOut,
    JobPublished,
    NotificationChannelCreate,
    NotificationChannelRead,
    NotifyAckIn,
    NotifyAckOut,
    ProductJobRead,
    ProductNotificationRead,
    PublishCallbackIn,
    PublishCallbackOut,
    WooCommerceWebhookIn,
    WebhookAccepted,
)
from app.product_services import (
    ack_notification_delivery,
    apply_publish_callback,
    create_channel,
    generate_draft_for_job,
    get_job,
    ingest_woocommerce_webhook,
    list_channels,
    list_notifications,
    publish_product_content,
    toggle_channel,
)
from app.settings import settings

router = APIRouter(prefix="/api/v1")

EDIT_ROLES = (WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.editor)
REVIEW_ROLES = (WorkspaceRole.owner, WorkspaceRole.admin, WorkspaceRole.reviewer)


def _constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8", "ignore"), b.encode("utf-8", "ignore"))


async def _verify_woocommerce_secret(request: Request, x_webhook_secret: str | None) -> bytes:
    body = await request.body()
    if not settings.woocommerce_webhook_secret or settings.woocommerce_webhook_secret == "change-me":
        raise HTTPException(status_code=503, detail="WooCommerce webhook intake is not configured")
    provided = x_webhook_secret or ""
    if _constant_time_equals(provided, settings.woocommerce_webhook_secret):
        return body
    # WooCommerce HMAC-SHA256 signature support (X-WC-Webhook-Signature).
    signature = request.headers.get("x-wc-webhook-signature") or request.headers.get("X-WC-Webhook-Signature")
    if signature:
        digest = hmac.new(
            settings.woocommerce_webhook_secret.encode("utf-8"), body, hashlib.sha256
        ).digest()
        expected = base64.b64encode(digest).decode()
        candidate = signature.split(",")[0].strip()
        if _constant_time_equals(candidate, expected):
            return body
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret/signature")


def _verify_automation_secret(value: str | None) -> None:
    if not value or value != settings.n8n_webhook_secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid automation secret")


# --------------------------------------------------------------------------- #
# WooCommerce inbound (machine auth)
# --------------------------------------------------------------------------- #
@router.post(
    "/integrations/woocommerce/webhooks/{workspace_id}",
    response_model=WebhookAccepted,
    status_code=202,
)
async def woocommerce_product_webhook(
    workspace_id: uuid.UUID,
    payload: WooCommerceWebhookIn,
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    await _verify_woocommerce_secret(request, x_webhook_secret)
    raw = payload.model_dump()
    # Preserve unknown-but-useful fields for normalization without trusting them blindly.
    extra = getattr(payload, "__pydantic_extra__", None) or {}
    raw.update(extra)
    result = await ingest_woocommerce_webhook(session, workspace_id, raw)
    return result


# --------------------------------------------------------------------------- #
# Admin/user endpoints
# --------------------------------------------------------------------------- #
@router.get("/workspaces/{workspace_id}/products/jobs", response_model=list[ProductJobRead])
async def list_product_jobs(
    workspace_id: uuid.UUID,
    status_filter: str | None = None,
    _: Membership = Depends(require_workspace_member),
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy import select

    from app.product_domain import ProductContentJob

    query = select(ProductContentJob).where(ProductContentJob.workspace_id == workspace_id)
    if status_filter:
        query = query.where(ProductContentJob.status == status_filter)
    query = query.order_by(ProductContentJob.created_at.desc()).limit(200)
    result = await session.execute(query)
    return list(result.scalars().all())


@router.post(
    "/workspaces/{workspace_id}/products/jobs/{job_id}/generate-draft",
    response_model=DraftGenerateOut,
)
async def generate_draft(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    payload: DraftGenerateIn,
    request: Request,
    current_user: User | None = Depends(get_optional_user),
    session: AsyncSession = Depends(get_session),
):
    # Dual auth: human editors use JWT; n8n automation uses the x-automation-secret
    # service header (same secret as the machine callbacks below).
    automation_header = request.headers.get("x-automation-secret")
    if automation_header:
        _verify_automation_secret(automation_header)
        created_by_user_id = None
    elif current_user is not None:
        membership = await require_workspace_member(workspace_id, current_user=current_user, session=session)
        if membership.role not in EDIT_ROLES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient workspace role")
        created_by_user_id = current_user.id
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (JWT or x-automation-secret)",
        )
    return await generate_draft_for_job(
        session,
        workspace_id=workspace_id,
        job_id=job_id,
        instruction=payload.instruction,
        created_by_user_id=created_by_user_id,
    )


@router.post("/workspaces/{workspace_id}/products/jobs/{job_id}/publish", response_model=JobPublished)
async def publish_job(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    _: Membership = Depends(require_roles(*REVIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    return await publish_product_content(session, workspace_id=workspace_id, job_id=job_id)


@router.post("/workspaces/{workspace_id}/products/jobs/{job_id}/cancel", response_model=ProductJobRead)
async def cancel_job(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    _: Membership = Depends(require_roles(*EDIT_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    from app.product_domain import S
    from app.product_services import transition_job

    job = await get_job(session, workspace_id, job_id)
    return await transition_job(session, job, S.cancelled)


@router.get(
    "/workspaces/{workspace_id}/products/jobs/{job_id}/notifications",
    response_model=list[ProductNotificationRead],
)
async def job_notifications(
    workspace_id: uuid.UUID,
    job_id: uuid.UUID,
    _: Membership = Depends(require_workspace_member),
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy.orm import selectinload

    from sqlalchemy import select

    from app.notify_domain import ProductNotification

    result = await session.execute(
        select(ProductNotification)
        .where(
            ProductNotification.workspace_id == workspace_id,
            ProductNotification.job_id == job_id,
        )
        .options(selectinload(ProductNotification.deliveries))
        .order_by(ProductNotification.created_at.asc())
    )
    return list(result.scalars().all())


# Channels CRUD ------------------------------------------------------------- #
@router.get(
    "/workspaces/{workspace_id}/notification-channels",
    response_model=list[NotificationChannelRead],
)
async def channels(
    workspace_id: uuid.UUID,
    _: Membership = Depends(require_workspace_member),
    session: AsyncSession = Depends(get_session),
):
    return await list_channels(session, workspace_id)


@router.post(
    "/workspaces/{workspace_id}/notification-channels",
    response_model=NotificationChannelRead,
    status_code=201,
)
async def add_channel(
    workspace_id: uuid.UUID,
    payload: NotificationChannelCreate,
    _: Membership = Depends(require_roles(*EDIT_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    return await create_channel(session, workspace_id, payload)


@router.patch(
    "/workspaces/{workspace_id}/notification-channels/{channel_id}",
    response_model=NotificationChannelRead,
)
async def patch_channel(
    workspace_id: uuid.UUID,
    channel_id: uuid.UUID,
    enabled: bool,
    _: Membership = Depends(require_roles(*EDIT_ROLES)),
    session: AsyncSession = Depends(get_session),
):
    return await toggle_channel(session, workspace_id, channel_id, enabled)


# --------------------------------------------------------------------------- #
# Automation callbacks (n8n, machine auth)
# --------------------------------------------------------------------------- #
@router.post("/automation/products/publish-callback", response_model=PublishCallbackOut)
async def product_publish_callback(
    payload: PublishCallbackIn,
    x_automation_secret: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    _verify_automation_secret(x_automation_secret)
    # workspace_id comes from the publication itself; verify consistency server-side.
    from app.domain import Publication

    publication = await session.get(Publication, payload.publication_id)
    if publication is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    return await apply_publish_callback(session, workspace_id=publication.workspace_id, payload=payload)


@router.post("/automation/products/notify-ack", response_model=NotifyAckOut)
async def product_notify_ack(
    payload: NotifyAckIn,
    x_automation_secret: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    _verify_automation_secret(x_automation_secret)
    from app.notify_domain import ProductNotification

    notification = await session.get(ProductNotification, payload.notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return await ack_notification_delivery(session, workspace_id=notification.workspace_id, payload=payload)
