"""Multi-channel product notification domain (Telegram / WhatsApp / Bale / Rubika / ...).

Design decisions:
- Channels are *data*, not code: adding a new network = one row in ``notification_channels``
  plus an adapter function inside the n8n fan-out workflow. No schema change needed.
- Delivery state is tracked per channel (fan-out), never as one global flag, so partial
  failures are visible and retryable per destination.
- Secrets live only in ProviderCredential (encrypted) or environment settings; this module
  never stores tokens in plaintext and never serializes them.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain import Base, TimestampMixin

# The set of channels with first-class adapters in the shipped n8n workflow.
SUPPORTED_CHANNELS = ("telegram", "whatsapp", "bale", "rubika")


class NotifyStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class NotificationChannel(Base, TimestampMixin):
    """One enabled destination for product fan-out notifications inside a workspace."""

    __tablename__ = "notification_channels"
    __table_args__ = (
        UniqueConstraint("workspace_id", "channel", "target_ref", name="uq_notification_channel_target"),
        ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            ["provider_credentials.workspace_id", "provider_credentials.id"],
            name="fk_notification_channel_workspace_credential",
        ),
        CheckConstraint("char_length(channel) BETWEEN 2 AND 40", name="ck_notification_channel_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # telegram | whatsapp | bale | rubika | <future>: any string; adapters resolve by value.
    channel: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Chat id / phone number / channel handle — provider-specific destination identifier.
    target_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Optional provider credential holding the bot token for this channel (never exposed via API).
    credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    template_key: Mapped[str] = mapped_column(String(60), nullable=False, default="product_default")
    extra_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ProductNotification(Base, TimestampMixin):
    """Aggregate delivery ledger for one content publication fan-out."""

    __tablename__ = "product_notifications"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_product_notification_idempotency"),
        # Composite key so per-channel delivery rows can FK back workspace-scoped.
        UniqueConstraint("workspace_id", "id", name="uq_product_notification_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "content_id"],
            ["content_items.workspace_id", "content_items.id"],
            name="fk_product_notification_workspace_content",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_content_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Rendered message shared by all channels (title + summary + links).
    message: Mapped[str] = mapped_column(Text, nullable=False)
    links_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[NotifyStatus] = mapped_column(
        Enum(NotifyStatus, name="notify_status"), default=NotifyStatus.pending, nullable=False, index=True
    )

    deliveries: Mapped[list["ProductNotificationDelivery"]] = relationship(
        "ProductNotificationDelivery",
        back_populates="notification",
        cascade="all, delete-orphan",
        foreign_keys="ProductNotificationDelivery.notification_id",
    )


class ProductNotificationDelivery(Base):
    """Per-channel attempt record; callback from the automation layer updates exactly one row."""

    __tablename__ = "product_notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", "channel", "target_ref", name="uq_notification_delivery_target"
        ),
        ForeignKeyConstraint(
            ["notification_id", "workspace_id"],
            ["product_notifications.id", "product_notifications.workspace_id"],
            name="fk_delivery_workspace_consistency",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_notifications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    notification: Mapped["ProductNotification"] = relationship(
        back_populates="deliveries", foreign_keys=[notification_id]
    )
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[NotifyStatus] = mapped_column(
        Enum(NotifyStatus, name="notify_status", create_type=False),
        default=NotifyStatus.pending,
        nullable=False,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
