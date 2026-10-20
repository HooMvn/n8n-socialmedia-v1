from __future__ import annotations

import enum
import uuid
from datetime import datetime

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
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


CONTENT_KIND_SOCIAL = "social"
CONTENT_KIND_PRODUCT = "product"


class ContentVersionSource(str, enum.Enum):
    ai_generation = "ai_generation"
    ai_revision = "ai_revision"
    human_edit = "human_edit"


class ImmutableRecordError(RuntimeError):
    """Raised when code tries to modify a record that is immutable by design."""


class WorkspaceRole(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    editor = "editor"
    reviewer = "reviewer"
    analyst = "analyst"
    viewer = "viewer"


class ContentStatus(str, enum.Enum):
    draft = "draft"
    in_review = "in_review"
    approved = "approved"
    rejected = "rejected"
    publish_requested = "publish_requested"
    published = "published"
    failed = "failed"


class PublicationStatus(str, enum.Enum):
    pending = "pending"
    published = "published"
    failed = "failed"


class OutboxStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    delivered = "delivered"
    failed = "failed"
    dead_letter = "dead_letter"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)


class Membership(Base, TimestampMixin):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        Enum(WorkspaceRole, name="workspace_role"), nullable=False, default=WorkspaceRole.viewer
    )


class BrandProfile(Base, TimestampMixin):
    __tablename__ = "brand_profiles"
    __table_args__ = (UniqueConstraint("workspace_id", name="uq_brand_workspace"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    tone_of_voice: Mapped[str] = mapped_column(Text, default="", nullable=False)
    audience: Mapped[str] = mapped_column(Text, default="", nullable=False)
    forbidden_terms: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ContentItem(Base, TimestampMixin):
    """Unit of review/approval/publication.

    kind="social": ``title``/``body`` are the writable content (legacy behaviour).
    kind="product": ``ContentVersion`` is the ONLY source of truth. ``title``/``body`` are a
    read-only mirror of ``current_version_id`` written exclusively by
    ``product_services.add_content_version``.
    """

    __tablename__ = "content_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_content_items_workspace_id"),
        CheckConstraint("kind IN ('social', 'product')", name="ck_content_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("brand_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ContentStatus] = mapped_column(
        Enum(ContentStatus, name="content_status"), default=ContentStatus.draft, nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(20), default=CONTENT_KIND_SOCIAL, server_default=CONTENT_KIND_SOCIAL, nullable=False
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Approval(Base, TimestampMixin):
    """Review decision. ``approver_user_id`` is always the authenticated user (never client input).

    ``actor`` is a server-derived display label kept for legacy rows. For product content
    ``content_version_id`` pins the decision to the exact version that was reviewed.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "content_id"],
            ["content_items.workspace_id", "content_items.id"],
            name="fk_approval_workspace_content",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    approver_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Publication(Base, TimestampMixin):
    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_publication_workspace_idempotency"),
        ForeignKeyConstraint(
            ["workspace_id", "content_id"],
            ["content_items.workspace_id", "content_items.id"],
            name="fk_publication_workspace_content",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    content_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("content_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[PublicationStatus] = mapped_column(
        Enum(PublicationStatus, name="publication_status"), default=PublicationStatus.pending, nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class OutboxEvent(Base, TimestampMixin):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_outbox_workspace_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status"), default=OutboxStatus.pending, nullable=False, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContentVersion(Base):
    """Immutable, auditable snapshot of generated/edited content (product content source of truth).

    Immutability: enforced by a PostgreSQL trigger (``content_versions_guard``) and an ORM
    ``before_update`` guard. The only permitted change is attaching ``validation_report`` /
    ``quality_report`` once (write-once, NULL -> value).
    """

    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint("content_id", "version_no", name="uq_content_version_no"),
        UniqueConstraint("content_id", "id", name="uq_content_version_content_id"),
        UniqueConstraint("workspace_id", "id", name="uq_content_version_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "content_id"],
            ["content_items.workspace_id", "content_items.id"],
            name="fk_content_version_workspace_content",
            ondelete="CASCADE",
        ),
        # The current version must belong to this very content item (DB-enforced, added in migration).
        CheckConstraint("version_no >= 1", name="ck_content_version_no_positive"),
        CheckConstraint("char_length(content_hash) = 64", name="ck_content_version_hash_len"),
        CheckConstraint(
            "kind <> 'product' OR status = 'draft' OR current_version_id IS NOT NULL",
            name="ck_content_product_requires_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[ContentVersionSource] = mapped_column(
        Enum(ContentVersionSource, name="content_version_source"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    validation_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    quality_report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


_VERSION_FROZEN = (
    "id",
    "workspace_id",
    "content_id",
    "version_no",
    "source",
    "payload",
    "content_hash",
    "model",
    "prompt_version",
    "created_at",
)
_VERSION_WRITE_ONCE = ("validation_report", "quality_report")


@event.listens_for(ContentVersion, "before_update")
def _guard_content_version_update(mapper, connection, target) -> None:  # noqa: ARG001
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(target)
    for name in _VERSION_FROZEN:
        if state.attrs[name].history.has_changes():
            raise ImmutableRecordError(f"ContentVersion.{name} is immutable")
    for name in _VERSION_WRITE_ONCE:
        history = state.attrs[name].history
        if history.has_changes() and history.deleted and history.deleted[0] is not None:
            raise ImmutableRecordError(f"ContentVersion.{name} is write-once")


@event.listens_for(ContentItem, "before_update")
def _guard_product_content_item(mapper, connection, target) -> None:  # noqa: ARG001
    """Product ContentVersion is the only writable source; title/body are a service-written mirror."""
    from sqlalchemy import inspect as sa_inspect

    state = sa_inspect(target)
    if state.attrs["kind"].history.has_changes() and state.attrs["kind"].history.deleted:
        raise ImmutableRecordError("ContentItem.kind is immutable")
    if target.kind == CONTENT_KIND_PRODUCT and not getattr(target, "_mirror_write_ok", False):
        for name in ("title", "body", "current_version_id"):
            if state.attrs[name].history.has_changes():
                raise ImmutableRecordError(
                    f"ContentItem.{name} of product content is written only via add_content_version"
                )
