from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain import Base, TimestampMixin


class KnowledgeSourceStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"


class ConsentStatus(str, enum.Enum):
    granted = "granted"
    revoked = "revoked"
    unknown = "unknown"


class SLAStatus(str, enum.Enum):
    active = "active"
    breached = "breached"
    resolved = "resolved"


class ProviderCredentialStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"
    invalid = "invalid"


class IdentityVerificationStatus(str, enum.Enum):
    unverified = "unverified"
    verified = "verified"
    conflicted = "conflicted"


class KnowledgeSource(Base, TimestampMixin):
    __tablename__ = "knowledge_sources"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    status: Mapped[KnowledgeSourceStatus] = mapped_column(Enum(KnowledgeSourceStatus, name="knowledge_source_status"), default=KnowledgeSourceStatus.active, index=True)


class KnowledgeChunk(Base, TimestampMixin):
    __tablename__ = "knowledge_chunks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    __table_args__ = (UniqueConstraint("source_id", "chunk_index", name="uq_knowledge_chunk_index"),)


class RetrievalEvidence(Base, TimestampMixin):
    __tablename__ = "retrieval_evidence"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    ai_draft_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_reply_drafts.id", ondelete="CASCADE"), index=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_chunks.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)


class ConsentRecord(Base, TimestampMixin):
    __tablename__ = "consent_records"
    __table_args__ = (UniqueConstraint("workspace_id", "customer_id", "purpose", "channel", name="uq_customer_consent_scope"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(80), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ConsentStatus] = mapped_column(Enum(ConsentStatus, name="consent_status"), default=ConsentStatus.unknown, index=True)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SLATracker(Base, TimestampMixin):
    __tablename__ = "sla_trackers"
    __table_args__ = (UniqueConstraint("workspace_id", "conversation_id", name="uq_sla_conversation"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    status: Mapped[SLAStatus] = mapped_column(Enum(SLAStatus, name="sla_status"), default=SLAStatus.active, index=True)
    first_response_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    resolution_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    first_responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ProviderCredential(Base, TimestampMixin):
    __tablename__ = "provider_credentials"
    __table_args__ = (
        UniqueConstraint("workspace_id", "provider", "credential_name", name="uq_provider_credential_workspace"),
        # Composite key so downstream tables (product snapshots, notification channels)
        # can declare workspace-scoped FKs that prevent cross-tenant references.
        UniqueConstraint("workspace_id", "id", name="uq_provider_credential_workspace_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    credential_name: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ProviderCredentialStatus] = mapped_column(Enum(ProviderCredentialStatus, name="provider_credential_status"), default=ProviderCredentialStatus.active, index=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdentityVerification(Base, TimestampMixin):
    __tablename__ = "identity_verifications"
    __table_args__ = (UniqueConstraint("workspace_id", "identity_id", name="uq_identity_verification"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    identity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customer_identities.id", ondelete="CASCADE"), index=True)
    status: Mapped[IdentityVerificationStatus] = mapped_column(Enum(IdentityVerificationStatus, name="identity_verification_status"), default=IdentityVerificationStatus.unverified, index=True)
    verification_method: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    verified_value_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
