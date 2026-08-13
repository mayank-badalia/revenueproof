"""Provenance vault and canonical financial records — Feature 1, sub-features 5-7.

Two layers, deliberately separated:

* `RawRecord` is the immutable, hashed capture of exactly what a provider returned.
  It is never edited. If normalisation logic improves, we re-derive canonical rows
  from the raw payload rather than mutating history.
* The canonical tables (`Invoice`, `Payment`, ...) are RevenueProof's own schema,
  each pointing back at the raw record it came from.

This separation is what lets the UI answer "where did this number come from?" with
the original provider payload rather than our interpretation of it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    WorkspaceScopedMixin,
    currency_column,
    money_column,
)
from app.models.enums import (
    InvoiceStatus,
    PaymentStatus,
    QuarantineReason,
    RecordType,
    SourceSystem,
    TransactionDirection,
)


class RawRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Immutable capture of one source record, with tamper-evident hashes.

    `content_hash` covers the canonical JSON serialisation of the payload, so an
    edited or replaced source file is detectable (idea_features.md §18). The
    `(workspace, source_system, source_id, content_hash)` uniqueness constraint is
    the idempotency guard: a duplicate webhook delivery or a replayed backfill
    re-inserts nothing, while a *changed* record is stored as a new version.
    """

    __tablename__ = "raw_records"

    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    record_type: Mapped[RecordType] = mapped_column(String(40), nullable=False)
    # The provider's own identifier, e.g. Razorpay "pay_XXXX".
    source_id: Mapped[str] = mapped_column(String(300), nullable=False)

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Increments when the same source_id returns different content later.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )

    # Which connector run produced this, for lineage (W3C PROV activity).
    ingestion_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Pointer into encrypted object storage for file-backed evidence (contracts).
    storage_key: Mapped[str | None] = mapped_column(Text)
    file_hash: Mapped[str | None] = mapped_column(String(64))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(120))

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "source_system",
            "source_id",
            "content_hash",
            name="raw_record_idempotency",
        ),
        Index("ix_raw_record_lookup", "workspace_id", "source_system", "record_type"),
        Index("ix_raw_record_source", "workspace_id", "source_id"),
    )


class QuarantinedRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Evidence that failed validation and must not enter downstream processing.

    PROJECT_WORKFLOW.md is explicit: quarantined evidence never reaches identity,
    contract or cash processing. Keeping it visible (rather than dropping it) is
    what turns "we ignored 12 malformed rows" into a reviewable finding.
    """

    __tablename__ = "quarantined_records"

    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    record_type: Mapped[RecordType] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(300))
    reason: Mapped[QuarantineReason] = mapped_column(String(40), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    # Field-level errors from Pydantic, kept verbatim for the reviewer.
    validation_errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_quarantine_open", "workspace_id", "resolved_at"),
    )


class CustomerEntity(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A canonical customer — the result of Feature 2's identity resolution.

    Source records point here once their identity link is accepted. Until then they
    carry `customer_entity_id = NULL` and appear in the review queue, because
    idea_features.md §14 forbids an unresolved match from supporting verified revenue.
    """

    __tablename__ = "customer_entities"

    canonical_name: Mapped[str] = mapped_column(String(300), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(300))
    # Normalised for blocking/matching: lowercased, suffixes stripped.
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False, index=True)

    domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    tax_identifiers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    known_aliases: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    email_addresses: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    addresses: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    # Surfaced for investigation, never asserted as a legal determination
    # (core_resoruces.md: relationship flags do not prove legal status).
    related_party_status: Mapped[str | None] = mapped_column(String(40))
    related_party_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    match_confidence: Mapped[float | None] = mapped_column()
    # True once a human confirmed this cluster; blocks automatic re-merging.
    human_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index("ix_customer_normalized", "workspace_id", "normalized_name"),
    )


class Contract(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Structured commercial terms extracted from a contract document (Feature 3).

    Amounts are split into recurring / one-time / future-period because treating a
    whole contract value as ARR is the single most common overstatement the product
    exists to catch (idea_features.md §24, Feature 3).
    """

    __tablename__ = "contracts"

    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )
    customer_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL"), index=True
    )

    document_name: Mapped[str] = mapped_column(String(500), nullable=False)
    # Customer name exactly as written in the contract, before identity resolution.
    stated_customer_name: Mapped[str | None] = mapped_column(String(300))

    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    billing_frequency: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown")

    currency: Mapped[str] = currency_column(default="INR")
    recurring_amount: Mapped[int] = money_column(default=0)
    one_time_amount: Mapped[int] = money_column(default=0)
    future_period_amount: Mapped[int] = money_column(default=0)

    auto_renewal: Mapped[bool | None] = mapped_column(Boolean)
    termination_notice_days: Mapped[int | None] = mapped_column(Integer)
    renewal_terms: Mapped[str | None] = mapped_column(Text)
    termination_terms: Mapped[str | None] = mapped_column(Text)
    refund_terms: Mapped[str | None] = mapped_column(Text)

    # Amendment precedence (Feature 3, sub-feature 7).
    supersedes_contract_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="SET NULL")
    )
    is_amendment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effective_from: Mapped[date | None] = mapped_column(Date)

    # Document handling
    is_scanned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ocr_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extraction_confidence: Mapped[float | None] = mapped_column()
    page_count: Mapped[int | None] = mapped_column(Integer)
    # Fields the extractor could not determine — recorded as unknown rather than
    # invented, per core_resoruces.md's "allow null rather than inventing terms".
    unknown_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint("recurring_amount >= 0", name="recurring_non_negative"),
        CheckConstraint("one_time_amount >= 0", name="one_time_non_negative"),
        CheckConstraint("future_period_amount >= 0", name="future_non_negative"),
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR end_date >= start_date",
            name="contract_end_after_start",
        ),
        Index("ix_contract_customer", "workspace_id", "customer_entity_id"),
    )

    citations: Mapped[list[Citation]] = relationship(
        back_populates="contract", cascade="all, delete-orphan"
    )


class Citation(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A page-level pointer proving where an extracted contract value came from.

    `verified` is set only after the Clause Verification Agent re-fetched the cited
    span from the original document and confirmed the quote hash — core_resoruces.md
    requires the verifier to fetch the span itself rather than trust generated
    citation text.
    """

    __tablename__ = "citations"

    contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    field_value: Mapped[str | None] = mapped_column(Text)

    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Character offsets into the extracted page text.
    span_start: Mapped[int | None] = mapped_column(Integer)
    span_end: Mapped[int | None] = mapped_column(Integer)
    # Layout coordinates (x0, y0, x1, y1) when available from PyMuPDF/Document AI.
    bbox: Mapped[list | None] = mapped_column(JSONB)

    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_citation_contract_field", "contract_id", "field_name"),)

    contract: Mapped[Contract] = relationship(back_populates="citations")


class Invoice(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A canonical invoice. An invoice is a claim on cash, never proof of cash."""

    __tablename__ = "invoices"

    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )
    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(300), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(120), index=True)

    customer_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL"), index=True
    )
    stated_customer_name: Mapped[str | None] = mapped_column(String(300))

    issue_date: Mapped[date | None] = mapped_column(Date, index=True)
    due_date: Mapped[date | None] = mapped_column(Date)

    currency: Mapped[str] = currency_column(default="INR")
    subtotal: Mapped[int] = money_column(default=0)
    tax: Mapped[int] = money_column(default=0)
    total: Mapped[int] = money_column(default=0)
    # Provider-reported balance; recomputed independently during reconciliation.
    amount_due: Mapped[int] = money_column(default=0)

    status: Mapped[InvoiceStatus] = mapped_column(String(30), nullable=False, default="unknown")
    line_items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # True when a line item looks like setup/implementation/consulting work.
    # spec §14: these are not recurring unless the contract explicitly says so.
    has_one_time_items: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "source_system", "source_id", name="invoice_source_unique"),
        CheckConstraint("total >= 0", name="invoice_total_non_negative"),
        Index("ix_invoice_customer_date", "workspace_id", "customer_entity_id", "issue_date"),
    )


class CreditNote(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Accounting-side reversal. Ignoring these treats reversed value as retained."""

    __tablename__ = "credit_notes"

    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )
    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(300), nullable=False)
    credit_note_number: Mapped[str | None] = mapped_column(String(120))

    customer_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL"), index=True
    )

    issue_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = currency_column(default="INR")
    total: Mapped[int] = money_column(default=0)
    reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source_system", "source_id", name="credit_note_source_unique"
        ),
    )


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A processor payment event.

    `amount` is gross. `fee` and `tax` are deducted by the processor before
    settlement, which is why a bank receipt legitimately differs from the payment
    amount (idea_features.md §18) — the reconciliation engine compares net, not gross.
    """

    __tablename__ = "payments"

    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )
    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(300), nullable=False)

    customer_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL"), index=True
    )
    stated_customer_name: Mapped[str | None] = mapped_column(String(300))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    contact_phone: Mapped[str | None] = mapped_column(String(40))

    currency: Mapped[str] = currency_column(default="INR")
    amount: Mapped[int] = money_column(default=0)
    fee: Mapped[int] = money_column(default=0)
    tax: Mapped[int] = money_column(default=0)
    amount_refunded: Mapped[int] = money_column(default=0)

    status: Mapped[PaymentStatus] = mapped_column(String(30), nullable=False, default="unknown")
    payment_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    method: Mapped[str | None] = mapped_column(String(40))
    # Free-text narration/description — the ambiguous field an LLM may interpret,
    # but never override structured transaction data with.
    description: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(300), index=True)

    settlement_id: Mapped[str | None] = mapped_column(String(200))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("workspace_id", "source_system", "source_id", name="payment_source_unique"),
        CheckConstraint("amount >= 0", name="payment_amount_non_negative"),
        CheckConstraint("amount_refunded >= 0", name="refunded_non_negative"),
        CheckConstraint("amount_refunded <= amount", name="refund_not_exceeding_payment"),
        Index("ix_payment_customer_time", "workspace_id", "customer_entity_id", "payment_time"),
    )


class Refund(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Money returned. Subtracted deterministically — never inferred by an LLM."""

    __tablename__ = "refunds"

    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )
    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(300), nullable=False)

    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    source_payment_id: Mapped[str | None] = mapped_column(String(300))

    currency: Mapped[str] = currency_column(default="INR")
    amount: Mapped[int] = money_column(default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="processed")
    refund_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    is_chargeback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("workspace_id", "source_system", "source_id", name="refund_source_unique"),
        CheckConstraint("amount >= 0", name="refund_amount_non_negative"),
    )


class BankTransaction(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Independent bank evidence — the strongest confirmation that cash arrived.

    `account_fingerprint` is a hash of the account number rather than the number
    itself: the reconciliation engine only needs to know two rows share an account,
    and storing the raw number would put unnecessary PII in the vault.
    """

    __tablename__ = "bank_transactions"

    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )
    source_system: Mapped[SourceSystem] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(300), nullable=False)

    account_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    value_date: Mapped[date | None] = mapped_column(Date)

    currency: Mapped[str] = currency_column(default="INR")
    amount: Mapped[int] = money_column(default=0)
    direction: Mapped[TransactionDirection] = mapped_column(String(10), nullable=False)

    counterparty: Mapped[str | None] = mapped_column(String(300), index=True)
    reference: Mapped[str | None] = mapped_column(String(300), index=True)
    narration: Mapped[str | None] = mapped_column(Text)
    balance_after: Mapped[int | None] = money_column(nullable=True, default=None)

    customer_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "source_system", "source_id", name="bank_txn_source_unique"),
        CheckConstraint("amount >= 0", name="bank_amount_non_negative"),
        Index("ix_bank_date_amount", "workspace_id", "transaction_date", "amount"),
    )
