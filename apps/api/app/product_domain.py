"""Product Content domain (Phase 1 of the WooCommerce -> Blog -> Notify pipeline).

Entities:
- ProductSnapshot: immutable, normalized view of an external (WooCommerce) product.
- ProductContentJob: pipeline state for generating the content of one product.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column

from app.domain import Base, ImmutableRecordError, TimestampMixin

# Hard backend ceiling for revisions. Never taken from AI output or client input beyond this bound.
HARD_MAX_REVISIONS = 5
DEFAULT_MAX_REVISIONS = 2
SNAPSHOT_SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 128 * 1024


class ProductJobStatus(str, enum.Enum):
    queued = "queued"
    generating = "generating"
    validating = "validating"
    quality_review = "quality_review"
    revising = "revising"
    ready_for_review = "ready_for_review"
    needs_attention = "needs_attention"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


S = ProductJobStatus

TERMINAL_JOB_STATES = frozenset({S.completed, S.failed, S.cancelled})
# States in which a pipeline step is actively executing (entering one counts as an attempt).
PROCESSING_JOB_STATES = frozenset({S.generating, S.validating, S.quality_review, S.revising})

ALLOWED_JOB_TRANSITIONS: dict[ProductJobStatus, frozenset[ProductJobStatus]] = {
    S.queued: frozenset({S.generating, S.failed, S.cancelled}),
    S.generating: frozenset({S.validating, S.failed, S.cancelled}),
    S.validating: frozenset({S.quality_review, S.revising, S.needs_attention, S.failed, S.cancelled}),
    S.quality_review: frozenset({S.ready_for_review, S.revising, S.needs_attention, S.failed, S.cancelled}),
    S.revising: frozenset({S.validating, S.needs_attention, S.failed, S.cancelled}),
    # Human edits re-enter deterministic validation.
    S.ready_for_review: frozenset({S.validating, S.completed, S.failed, S.cancelled}),
    S.needs_attention: frozenset({S.validating, S.failed, S.cancelled}),
    S.completed: frozenset(),
    S.failed: frozenset(),
    S.cancelled: frozenset(),
}


class ProductSnapshot(Base):
    """Normalized, immutable view of an external (WooCommerce) product at one point in time."""

    __tablename__ = "product_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "credential_id",
            "external_product_id",
            "source_hash",
            name="uq_product_snapshot_source",
        ),
        UniqueConstraint("workspace_id", "id", name="uq_product_snapshot_workspace_id"),
        ForeignKeyConstraint(
            ["workspace_id", "credential_id"],
            ["provider_credentials.workspace_id", "provider_credentials.id"],
            name="fk_product_snapshot_workspace_credential",
        ),
        CheckConstraint("char_length(source_hash) = 64", name="ck_product_snapshot_hash_len"),
        CheckConstraint("char_length(external_product_id) >= 1", name="ck_product_snapshot_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    credential_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    external_product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=SNAPSHOT_SCHEMA_VERSION)
     Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@event.listens_for(ProductSnapshot, "before_update")
def _snapshots_are_immutable(mapper, connection, target) -> None:  # noqa: ARG001
    raise ImmutableRecordError("ProductSnapshot is immutable; a changed source is a new snapshot")


class ProductContentJob(Base, TimestampMixin):
    """Pipeline state for generating the content of one product. Holds NO content itself."""

    __tablename__ = "product_content_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_product_job_workspace_idempotency"),
        UniqueConstraint("content_id", name="uq_product_job_content"),
        ForeignKeyConstraint(
            ["workspace_id", "snapshot_id"],
            ["product_snapshots.workspace_id", "product_snapshots.id"],
            name="fk_product_job_workspace_snapshot",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "content_id"],
            ["content_items.workspace_id", "content_items.id"],
            name="fk_product_job_workspace_content",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            f"max_revisions >= 0 AND max_revisions <= {HARD_MAX_REVISIONS}",
            name="ck_product_job_max_revisions",
        ),
        CheckConstraint(
            "revision_count >= 0 AND revision_count <= max_revisions", name="ck_product_job_revision_count"
        ),
        CheckConstraint("attempts >= 0", name="ck_product_job_attempts"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    content_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[ProductJobStatus] = mapped_column(
        Enum(ProductJobStatus, name="product_job_status"),
        default=ProductJobStatus.queued,
        nullable=False,
        index=True,
    )
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_revisions: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_MAX_REVISIONS)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def notify_idempotency_key(self) -> str:
        """Deterministic key for the fan-out notification event of this job."""
        return f"product-notify:{self.id}"

    def publication_idempotency_key(self) -> str:
        return f"product-publish:{self.id}"


def utcnow() -> datetime:
    return datetime.now(UTC)
