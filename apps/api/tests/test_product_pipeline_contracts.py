"""Unit contracts for the WooCommerce -> Blog -> Notify product pipeline.

These tests touch no database and no network: normalization, hashing, state machine,
rendering and routing decisions must be deterministic on their own.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.domain import ContentVersionSource, OutboxStatus
from app.notify_domain import NotifyStatus, SUPPORTED_CHANNELS
from app.outbox_worker import webhook_for_event
from app.product_domain import (
    ALLOWED_JOB_TRANSITIONS,
    S,
    ProductJobStatus,
)
from app.product_schemas import (
    DraftGenerateIn,
    NotificationChannelCreate,
    NotifyAckIn,
    PublishCallbackIn,
    WooCommerceWebhookIn,
)
from app.product_services import (
    canonical_hash,
    normalize_product_payload,
    render_notification_message,
    strip_html,
    validate_draft,
    version_hash,
)


def _woo_body(**overrides) -> dict:
    body = {
        "id": 4210,
        "name": "هدفانه بلوتوثی مدل X",
        "slug": "bluetooth-headset-x",
        "type": "simple",
        "status": "publish",
        "permalink": "https://shop.example.com/product/bluetooth-headset-x",
        "description": "<p>باتری ۳۰ ساعت، <strong>ضد آب</strong> با قیمت ۱,۲۵۰,۰۰۰ تومان</p>",
        "short_description": "باتری ۳۰ ساعت و قیمت ۱,۲۵۰,۰۰۰ تومان",
        "price": "1250000",
        "sku": "BHX-001",
        "categories": [{"id": 3, "name": "صوتی"}],
        "tags": [{"id": 9, "name": "بلوتوث"}],
        "images": [{"src": "https://cdn.example.com/bhx.jpg", "alt": "هدفونه"}],
        "meta_data": [{"key": "warranty", "value": "۱۸ ماه"}],
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Normalization / hashing
# --------------------------------------------------------------------------- #
def test_strip_html_removes_tags_and_entities() -> None:
    assert strip_html("<p>سلام &amp; دنیا</p>  <b>خوب</b>") == "سلام & دنیا خوب"


def test_normalize_whitelists_and_is_deterministic() -> None:
    first = normalize_product_payload(_woo_body(unexpected_field="ignored"))
    second = normalize_product_payload(_woo_body())
    assert first == second  # unknown keys never leak into the snapshot
    assert "unexpected_field" not in first
    assert first["external_product_id"] == "4210"
    assert "<" not in first["description"]  # html stripped
    assert canonical_hash(first) == canonical_hash(second)


def test_normalize_clamps_description_length() -> None:
    # Per-field clamping guarantees the snapshot always fits DB/size limits.
    normalized = normalize_product_payload(_woo_body(description="<p>" + "الف" * 60000 + "</p>"))
    assert len(normalized["description"]) <= 20000


def test_normalize_rejects_oversized_meta_blobs() -> None:
    huge_meta = [{"key": f"k{i}", "value": "ی" * 2000} for i in range(50)]
    with pytest.raises(HTTPException) as exc:
        normalize_product_payload(_woo_body(meta_data=huge_meta))
    assert exc.value.status_code == 413


def test_version_hash_changes_with_payload() -> None:
    a = version_hash({"title": "a"})
    b = version_hash({"title": "b"})
    assert a != b and len(a) == 64


# --------------------------------------------------------------------------- #
# Job state machine
# --------------------------------------------------------------------------- #
def test_job_transition_table_is_closed_over_states() -> None:
    assert set(ALLOWED_JOB_TRANSITIONS) == set(ProductJobStatus)
    assert S.cancelled in ALLOWED_JOB_TRANSITIONS[S.queued]
    assert S.generating in ALLOWED_JOB_TRANSITIONS[S.queued]
    assert ALLOWED_JOB_TRANSITIONS[S.completed] == frozenset()
    assert ALLOWED_JOB_TRANSITIONS[S.failed] == frozenset()


def test_terminal_states_accept_nothing() -> None:
    for terminal in (S.completed, S.failed, S.cancelled):
        assert ALLOWED_JOB_TRANSITIONS[terminal] == frozenset()


# --------------------------------------------------------------------------- #
# Draft validation (deterministic grounding, no AI judging AI)
# --------------------------------------------------------------------------- #
def _snapshot_for_validation() -> dict:
    return normalize_product_payload(_woo_body())


def test_validate_draft_accepts_grounded_draft() -> None:
    snapshot = _snapshot_for_validation()
    draft = {
        "title": "بررسی هدفانه بلوتوثی X",
        "summary": "باتری ۳۰ ساعت با قیمت ۱,۲۵۰,۰۰۰ تومان؛ ضد آب و مناسب ورزش.",
        "body_markdown": "## معرفی\n" + "این یک متن بلاگ کامل فارسی درباره محصول است. " * 5,
        "hashtags": ["هدفانه", "بلوتوث"],
    }
    report = validate_draft(snapshot, draft)
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["normalized_hashtags"] == ["هدفانه", "بلوتوث"]


def test_validate_draft_flags_ungrounded_numbers() -> None:
    snapshot = _snapshot_for_validation()
    # Persian digits never appear in the source blob, so they count as invented.
    draft = {
        "title": "عنوان",
        "summary": "گارانتی ۹۹ ساله، قیمت ۴۷,۰۰۰,۰۰۰ تومان و وزن ۳۳ کیلوگرم!",
        "body_markdown": "م" * 120,
        "hashtags": [],
    }
    report = validate_draft(snapshot, draft)
    assert "possible_ungrounded_numbers" in report["warnings"]


def test_validate_draft_rejects_missing_parts() -> None:
    report = validate_draft(_snapshot_for_validation(), {"title": "", "summary": "کوتاه", "body_markdown": "کوتاه"})
    assert report["ok"] is False
    assert set(report["errors"]) >= {"missing_title", "summary_too_short", "body_too_short"}


# --------------------------------------------------------------------------- #
# Notification rendering
# --------------------------------------------------------------------------- #
def test_render_notification_message_contains_links_and_hashtags() -> None:
    message = render_notification_message(
        {
            "title": "هدفانه X",
            "summary": "باتری ۳۰ ساعت.",
            "hashtags": ["هدفانه", "#بلوتوث"],
            "product_url": "https://shop.example.com/p/1",
        },
        {"blog_url": "https://blog.example.com/post/9", "product_url": ""},
    )
    assert "هدفانه X" in message
    assert "#بلوتوث" in message
    assert "https://blog.example.com/post/9" in message
    assert "https://shop.example.com/p/1" in message


# --------------------------------------------------------------------------- #
# Schemas / contracts
# --------------------------------------------------------------------------- #
def test_channel_schema_lowercases_channel() -> None:
    assert NotificationChannelCreate(channel=" Telegram ", target_ref="@chan").channel == "telegram"


def test_supported_channels_contract() -> None:
    assert SUPPORTED_CHANNELS == ("telegram", "whatsapp", "bale", "rubika")
    assert {NotifyStatus.pending, NotifyStatus.sent, NotifyStatus.failed, NotifyStatus.skipped} == set(NotifyStatus)


def test_publish_callback_status_is_bounded() -> None:
    base = {"publication_id": uuid.uuid4(), "idempotency_key": "k"}
    assert PublishCallbackIn(**base, status="published").status == "published"
    with pytest.raises(Exception):
        PublishCallbackIn(**base, status="halfway_there")


def test_notify_ack_requires_destination() -> None:
    with pytest.raises(Exception):
        NotifyAckIn(notification_id=uuid.uuid4(), idempotency_key="k", channel="tg", target_ref="", delivered=True)


def test_webhook_in_accepts_numeric_or_string_product_id() -> None:
    assert WooCommerceWebhookIn(id=7).id == 7
    assert WooCommerceWebhookIn(id="7").id == "7"


def test_draft_instruction_is_bounded() -> None:
    assert DraftGenerateIn().instruction == ""
    with pytest.raises(Exception):
        DraftGenerateIn(instruction="x" * 2001)


# --------------------------------------------------------------------------- #
# Outbox routing
# --------------------------------------------------------------------------- #
def test_product_events_route_to_product_webhook() -> None:
    from app.settings import settings

    product_url = settings.n8n_product_webhook_url
    assert webhook_for_event("product.content.generate.requested") == product_url
    assert webhook_for_event("product.wordpress.publish.requested") == product_url
    assert webhook_for_event("product.notify.fanout.requested") == product_url
    # Legacy events keep their historical routes.
    assert webhook_for_event("message.outbound.requested") == settings.n8n_outbound_webhook_url
    assert webhook_for_event("content.publish.requested") == settings.n8n_publish_webhook_url


def test_content_version_source_covers_product_flow() -> None:
    assert {"ai_generation", "ai_revision", "human_edit"} <= {e.value for e in ContentVersionSource}


def test_outbox_status_values_stable() -> None:
    assert {e.value for e in OutboxStatus} == {"pending", "processing", "delivered", "failed", "dead_letter"}
