"""Decision-layer tables: matching, allocation, classification, review and audit.

Everything here records *how a conclusion was reached*, not just the conclusion:
which rule fired, which evidence supported it, what was missing, which agent
proposed it, whether the critic agreed and whether a human overrode it. That trail
is the product — a number without it is what reviewers already distrust.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    WorkspaceScopedMixin,
    currency_column,
    money_column,
)
from app.models.enums import (
    ActorType,
    AnomalySeverity,
    CriticVerdict,
    EvidenceStrength,
    MatchDecision,
    RevenueClass,
    ReviewStatus,
    RunStatus,
)


class VerificationRun(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One execution of the LangGraph verification pipeline.

    Pins the versions that produced the result (policy, prompt, model) so a figure
    can be reproduced later, and so a report diff can explain *why* a number moved
    when the underlying evidence did not.
    """

    __tablename__ = "verification_runs"

    status: Mapped[RunStatus] = mapped_column(String(30), nullable=False, default="pending")
    triggered_by: Mapped[str] = mapped_column(String(40), nullable=False, default="manual")
    current_stage: Mapped[str | None] = mapped_column(String(80))
    progress_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    policy_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")
    proposer_model: Mapped[str | None] = mapped_column(String(120))
    critic_model: Mapped[str | None] = mapped_column(String(120))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    # Per-feature counters shown on the processing-trace screen.
    stage_stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Set when the run is only reprocessing entities affected by a source change.
    is_incremental: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    affected_entity_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (Index("ix_run_workspace_status", "workspace_id", "status"),)


class EntityMatchProposal(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A proposed identity link between two source records (Feature 2).

    Stores the *evidence* for the match, not only a score, because the Match Critic
    receives original identifiers rather than the first agent's summary.
    """

    __tablename__ = "entity_match_proposals"

    # Left/right are source-record references, e.g. ("invoice", <uuid>).
    left_type: Mapped[str] = mapped_column(String(40), nullable=False)
    left_id: Mapped[str] = mapped_column(String(100), nullable=False)
    left_label: Mapped[str] = mapped_column(String(300), nullable=False)
    right_type: Mapped[str] = mapped_column(String(40), nullable=False)
    right_id: Mapped[str] = mapped_column(String(100), nullable=False)
    right_label: Mapped[str] = mapped_column(String(300), nullable=False)

    # Which tier of evidence produced this: exact | fuzzy | semantic
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Individual signals with their contributions, for explainability.
    signals: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    decision: Mapped[MatchDecision] = mapped_column(String(20), nullable=False, default="REVIEW")
    decided_by: Mapped[ActorType] = mapped_column(String(20), nullable=False, default="system")
    critic_note: Mapped[str | None] = mapped_column(Text)

    resulting_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "left_type", "left_id", "right_type", "right_id",
            name="one_proposal_per_pair",
        ),
        Index("ix_match_decision", "workspace_id", "decision"),
    )


class Allocation(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """One link in the cash chain: part of a payment applied to part of an invoice.

    Modelling allocations as rows (rather than a foreign key on either side) is what
    makes partial, combined and split payments representable, and what lets the
    conservation invariant be checked by summing a column.
    """

    __tablename__ = "allocations"

    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    bank_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bank_transactions.id", ondelete="SET NULL"), index=True
    )

    currency: Mapped[str] = currency_column(default="INR")
    allocated_amount: Mapped[int] = money_column(default=0)

    # How this link was established: exact_reference | amount_date | solver | manual
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    rule_id: Mapped[str | None] = mapped_column(String(60))
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Set when a later refund/chargeback reopened this match.
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("allocated_amount >= 0", name="allocation_non_negative"),
        CheckConstraint(
            "invoice_id IS NOT NULL OR payment_id IS NOT NULL OR bank_transaction_id IS NOT NULL",
            name="allocation_links_something",
        ),
        Index("ix_allocation_active", "workspace_id", "reversed_at"),
    )


class RevenueItem(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A unit of claimed revenue being tested, and its classification (Feature 5).

    Anchored on whichever evidence exists — usually an invoice, sometimes a contract
    obligation with no invoice, sometimes a payment with no invoice at all (which is
    exactly the PAYMENT_WITHOUT_SUPPORT case reviewers care about).
    """

    __tablename__ = "revenue_items"

    customer_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL"), index=True
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="SET NULL")
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL")
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id", ondelete="SET NULL")
    )

    description: Mapped[str] = mapped_column(String(500), nullable=False)
    currency: Mapped[str] = currency_column(default="INR")
    # Gross amount before period allocation.
    gross_amount: Mapped[int] = money_column(default=0)
    # Portion attributable to the reporting period, after proration.
    recognized_amount: Mapped[int] = money_column(default=0)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)

    classification: Mapped[RevenueClass] = mapped_column(String(40), nullable=False)
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_strength: Mapped[EvidenceStrength] = mapped_column(
        String(20), nullable=False, default="LIMITED"
    )

    rule_id: Mapped[str] = mapped_column(String(60), nullable=False)
    rule_explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    missing_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Day-count ratio and inputs used for proration, so the maths is inspectable.
    calculation_detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    agent_confidence: Mapped[float | None] = mapped_column(Float)
    is_material: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    critic_verdict: Mapped[CriticVerdict | None] = mapped_column(String(30))
    human_decision: Mapped[str | None] = mapped_column(String(20))
    # Terminal state: only published once approved by critic or resolved by a human.
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verification_runs.id", ondelete="SET NULL"), index=True
    )
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")

    __table_args__ = (
        CheckConstraint("gross_amount >= 0", name="gross_non_negative"),
        CheckConstraint("recognized_amount >= 0", name="recognized_non_negative"),
        CheckConstraint(
            "recognized_amount <= gross_amount", name="recognized_within_gross"
        ),
        Index("ix_revenue_item_class", "workspace_id", "classification"),
        Index("ix_revenue_item_published", "workspace_id", "is_published"),
    )


class Anomaly(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """An investigation prompt, not an accusation (Feature 6).

    Wording is constrained by design: `title` and `explanation` describe what was
    observed against what baseline, and `required_check` says what a human should do
    to resolve it. core_resoruces.md forbids the word "fraud" in findings.
    """

    __tablename__ = "anomalies"

    rule_id: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[AnomalySeverity] = mapped_column(String(20), nullable=False, default="medium")

    customer_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customer_entities.id", ondelete="SET NULL"), index=True
    )
    # References to the records that triggered it, as {type, id} pairs.
    related_records: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    observed_value: Mapped[str | None] = mapped_column(String(200))
    baseline_value: Mapped[str | None] = mapped_column(String(200))
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    required_check: Mapped[str] = mapped_column(Text, nullable=False)
    caveats: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Graph path evidence for related-party / circular-flow findings.
    graph_path: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Populated only when the ML scorer (rather than a rule) raised this.
    model_version: Mapped[str | None] = mapped_column(String(60))
    model_score: Mapped[float | None] = mapped_column(Float)

    status: Mapped[ReviewStatus] = mapped_column(String(20), nullable=False, default="open")
    # Set when a reviewer marks a flag as a false positive; feeds precision metrics.
    is_false_positive: Mapped[bool | None] = mapped_column(Boolean)

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verification_runs.id", ondelete="SET NULL")
    )

    __table_args__ = (Index("ix_anomaly_status_severity", "workspace_id", "status", "severity"),)


class CriticDecision(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """The independent critic's verdict on a proposed classification (Feature 7)."""

    __tablename__ = "critic_decisions"

    revenue_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("revenue_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    verdict: Mapped[CriticVerdict] = mapped_column(String(30), nullable=False)
    issue_codes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    challenged_evidence_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requested_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Which feature a dispute is routed back to (2-6), if any.
    routed_to_feature: Mapped[int | None] = mapped_column(Integer)

    critic_model: Mapped[str | None] = mapped_column(String(120))
    # Deterministic checks that ran before the LLM saw anything.
    deterministic_findings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verification_runs.id", ondelete="SET NULL")
    )


class ReviewItem(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """A case awaiting human resolution (Feature 7's queue)."""

    __tablename__ = "review_items"

    # ambiguous_match | unreadable_contract | partial_payment | clause_conflict |
    # missing_bank_evidence | related_party | agent_disagreement
    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[AnomalySeverity] = mapped_column(String(20), nullable=False, default="medium")

    # Whichever object needs the decision.
    revenue_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("revenue_items.id", ondelete="CASCADE")
    )
    match_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entity_match_proposals.id", ondelete="CASCADE")
    )
    anomaly_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("anomalies.id", ondelete="CASCADE")
    )
    contract_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="CASCADE")
    )

    # Everything the reviewer needs to decide, assembled server-side.
    evidence_packet: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[ReviewStatus] = mapped_column(String(20), nullable=False, default="open")
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolution: Mapped[str | None] = mapped_column(String(20))
    # Mandatory: idea_features.md §7 requires an override to carry a reason.
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # LangGraph thread that is interrupted waiting on this decision.
    graph_thread_id: Mapped[str | None] = mapped_column(String(100), index=True)

    __table_args__ = (Index("ix_review_open", "workspace_id", "status", "severity"),)


class CorrectionMemory(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Human-confirmed corrections, scoped to one workspace.

    core_resoruces.md rejects automatic cross-tenant learning outright, so this
    table is always queried with a workspace filter and never aggregated globally.
    """

    __tablename__ = "correction_memory"

    # alias | classification_override | match_rule | related_party
    correction_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    corrected_value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    times_applied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_until: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_correction_lookup", "workspace_id", "correction_type", "is_active"),
    )


class AuditEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """Append-only, hash-chained record of every consequential action.

    `previous_hash`/`event_hash` form a chain so that deleting or editing a past
    event is detectable. core_resoruces.md notes this is the justified integrity
    tool here — RevenueProof is the single writer, so blockchain would add cost
    without solving a real consensus problem.
    """

    __tablename__ = "audit_events"

    actor_type: Mapped[ActorType] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(60), nullable=False)
    object_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    before_state: Mapped[dict | None] = mapped_column(JSONB)
    after_state: Mapped[dict | None] = mapped_column(JSONB)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)

    # Hash chain over the whole audit log.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    policy_version: Mapped[str | None] = mapped_column(String(50))

    __table_args__ = (
        UniqueConstraint("workspace_id", "sequence", name="audit_sequence_unique"),
        Index("ix_audit_object", "workspace_id", "object_type", "object_id"),
    )


class ReportVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin, WorkspaceScopedMixin):
    """An immutable published report (Feature 8).

    Totals are denormalised onto the row so a historical report keeps showing what
    it said at publication time, even after the underlying items are reclassified.
    """

    __tablename__ = "report_versions"

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verification_runs.id", ondelete="SET NULL")
    )

    currency: Mapped[str] = currency_column(default="INR")
    claimed_revenue: Mapped[int] = money_column(default=0)
    claimed_arr: Mapped[int] = money_column(default=0)
    cash_received: Mapped[int] = money_column(default=0)
    verified_recurring: Mapped[int] = money_column(default=0)
    verified_one_time: Mapped[int] = money_column(default=0)
    contracted_unpaid: Mapped[int] = money_column(default=0)
    invoiced_unpaid: Mapped[int] = money_column(default=0)
    refunded_reversed: Mapped[int] = money_column(default=0)
    unsupported: Mapped[int] = money_column(default=0)
    supported_arr: Mapped[int] = money_column(default=0)

    items_awaiting_review: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    largest_customer_concentration_pct: Mapped[float | None] = mapped_column(Float)
    hhi: Mapped[float | None] = mapped_column(Float)

    # Narrative sections, each sentence carrying its evidence IDs.
    summary_sections: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    waterfall: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    concentration: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    missing_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    methodology_notes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Deterministic diff against the previous version.
    changes_from_previous: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    change_explanation: Mapped[str | None] = mapped_column(Text)

    policy_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="report_version_unique"),
        Index("ix_report_latest", "workspace_id", "version"),
    )
