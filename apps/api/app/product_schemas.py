"""API contracts for the WooCommerce -> Blog -> Notify product pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PAYLOAD_BYTES = 256 * 1024


class WooCommerceWebhookIn(BaseModel):
    """Body posted by the WooCommerce 'product created' webhook.

    We accept it as-is (extra keys allowed) but never trust it blindly:
    only whitelisted fields are normalized into the snapshot.
    """

    model_config = ConfigDict(extra="allow")

    id: int | str = Field(..., description="WooCommerce product id")
    name: str = Field(default="", max_length=500)
    slug: str | None = None
    type: str | None = None
    status: str | None = None
    permalink: str | None = None
    description: str = ""
    short_description: str = ""
    price: str | int | float | None = None
    regular_price: str | int | float | None = None
    sale_price: str | int | float | None = None
    sku: str | None = None
    categories: list[dict] = []
    tags: list[dict] = []
    images: list[dict] = []
    meta_ list[dict] = []


class ProductSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    credential_id: uuid.UUID
    external_product_id: str
    source_hash: str
    schema_version: int
    fetched_at: datetime
    created_at: datetime


class ProductJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    snapshot_id: uuid.UUID
    content_id: uuid.UUID
    status: str
    revision_count: int
    max_revisions: int
    attempts: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def _enum_to_str(cls, value):
        return value.value if hasattr(value, "value") else value


class ContentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_id: uuid.UUID
    version_no: int
    source: str
    payload: dict
    content_hash: str
    model: str | None
    prompt_version: str | None
    validation_report: dict | None
    quality_report: dict | None
    created_at: datetime

    @field_validator("source", mode="before")
    @classmethod
    def _enum_to_str(cls, value):
        return value.value if hasattr(value, "value") else value


class NotificationChannelCreate(BaseModel):
    channel: str = Field(min_length=2, max_length=40)
    target_ref: str = Field(min_length=1, max_length=255)
    display_name: str = Field(default="", max_length=120)
    credential_id: uuid.UUID | None = None
    template_key: str = Field(default="product_default", max_length=60)

    @field_validator("channel")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class NotificationChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    channel: str
    target_ref: str
    display_name: str
    enabled: bool
    credential_id: uuid.UUID | None
    template_key: str
    created_at: datetime


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notification_id: uuid.UUID
    channel: str
    target_ref: str
    status: str
    attempts: int
    external_message_id: str | None
    error_code: str | None
    last_error: str | None
    delivered_at: datetime | None

    @field_validator("status", mode="before")
    @classmethod
    def _enum_to_str(cls, value):
        return value.value if hasattr(value, "value") else value


class ProductNotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    content_id: uuid.UUID
    job_id: uuid.UUID | None
    message: str
    links_json: dict
    status: str
    deliveries: list[DeliveryRead] = []

    @field_validator("status", mode="before")
    @classmethod
    def _enum_to_str(cls, value):
        return value.value if hasattr(value, "value") else value


class OutboxEventRef(BaseModel):
    event_id: uuid.UUID
    event_type: str
    payload: dict


class WebhookAccepted(BaseModel):
    duplicate: bool
    snapshot: ProductSnapshotRead
    job: ProductJobRead | None = None
    events: list[OutboxEventRef] = []


class JobEnqueued(BaseModel):
    duplicate: bool
    job: ProductJobRead
    events: list[OutboxEventRef] = []


class JobStatusUpdateIn(BaseModel):
    status: str = Field(description="Target ProductJobStatus value")


class JobStatusUpdateOut(BaseModel):
    job: ProductJobRead
    events: list[OutboxEventRef] = []


class DraftGenerateIn(BaseModel):
    instruction: str = Field(default="", max_length=2000)


class DraftGenerateOut(BaseModel):
    job: ProductJobRead
    version: ContentVersionRead
    events: list[OutboxEventRef] = []


class JobPublished(BaseModel):
    job: ProductJobRead
    publication: dict | None = None
    events: list[OutboxEventRef] = []
    skipped: bool = False


class PublishCallbackIn(BaseModel):
    publication_id: uuid.UUID
    idempotency_key: str
    status: str = Field(pattern=r"^(published|failed)$")
    external_id: str | None = Field(default=None, max_length=255)
    external_url: str | None = Field(default=None, max_length=500)
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = None


class PublishCallbackOut(BaseModel):
    job: ProductJobRead
    publication: dict
    events: list[OutboxEventRef] = []


class NotifyAckIn(BaseModel):
    notification_id: uuid.UUID
    idempotency_key: str
    channel: str = Field(min_length=2, max_length=40)
    target_ref: str = Field(min_length=1, max_length=255)
    delivered: bool
    external_message_id: str | None = Field(default=None, max_length=255)
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = None


class NotifyAckOut(BaseModel):
    delivery: DeliveryRead
    notification_status: str


class WordPressPostCallbackIn(BaseModel):
    """Optional callback from the n8n WordPress node with the created post URL."""

    content_id: uuid.UUID
    external_post_id: str | None = Field(default=None, max_length=255)
    url: str | None = Field(default=None, max_length=500)
