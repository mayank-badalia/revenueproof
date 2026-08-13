"""Controlled vocabularies shared by the database, the agents and the UI.

These are `StrEnum`s so they serialise as readable strings in JSON, Postgres and
LLM structured output. core_resoruces.md is explicit that free-form labels are
rejected: an agent that invents a ninth revenue state must fail schema validation
rather than quietly widen the taxonomy.
"""

from __future__ import annotations

from enum import StrEnum


class RevenueClass(StrEnum):
    """The eight mutually exclusive revenue states from idea_features.md §6.6."""

    VERIFIED_RECURRING = "VERIFIED_RECURRING"
    VERIFIED_ONE_TIME = "VERIFIED_ONE_TIME"
    CONTRACTED_UNPAID = "CONTRACTED_UNPAID"
    INVOICED_UNPAID = "INVOICED_UNPAID"
    REFUNDED_OR_REVERSED = "REFUNDED_OR_REVERSED"
    PAYMENT_WITHOUT_SUPPORT = "PAYMENT_WITHOUT_SUPPORT"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    HUMAN_REVIEW = "HUMAN_REVIEW"

    @property
    def counts_as_verified(self) -> bool:
        """Only these two states may be added into 'evidence-supported revenue'."""
        return self in {RevenueClass.VERIFIED_RECURRING, RevenueClass.VERIFIED_ONE_TIME}

    @property
    def counts_toward_arr(self) -> bool:
        """ARR includes recurring components only (idea_features.md §8)."""
        return self is RevenueClass.VERIFIED_RECURRING


class EvidenceStrength(StrEnum):
    """Evidence completeness, not a probability that the company is honest (§9)."""

    STRONG = "STRONG"        # contract + invoice + payment + bank receipt agree
    MODERATE = "MODERATE"    # contract + invoice + payment, no bank confirmation
    LIMITED = "LIMITED"      # only an invoice or only a contract
    DISPUTED = "DISPUTED"    # sources contradict, or the critic rejected it


class CriticVerdict(StrEnum):
    """Feature 7's structured verdict contract."""

    APPROVED = "APPROVED"
    DISPUTED = "DISPUTED"
    MORE_EVIDENCE_REQUIRED = "MORE_EVIDENCE_REQUIRED"


class HumanDecisionType(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CORRECT = "CORRECT"


class MatchDecision(StrEnum):
    """Entity-resolution outcome for a proposed identity link."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REVIEW = "REVIEW"


class SourceSystem(StrEnum):
    RAZORPAY = "razorpay"
    ZOHO_BOOKS = "zoho_books"
    GOOGLE_DRIVE = "google_drive"
    HUBSPOT = "hubspot"
    BANK_CSV = "bank_csv"
    ACCOUNT_AGGREGATOR = "account_aggregator"
    MANUAL = "manual"           # founder-entered claim figures
    SYNTHETIC = "synthetic"     # the demonstration dataset from spec §15


class RecordType(StrEnum):
    """Canonical object types (idea_features.md §5 step 3)."""

    CUSTOMER = "customer"
    CONTRACT = "contract"
    INVOICE = "invoice"
    CREDIT_NOTE = "credit_note"
    PAYMENT = "payment"
    REFUND = "refund"
    DISPUTE = "dispute"
    SETTLEMENT = "settlement"
    BANK_TRANSACTION = "bank_transaction"
    CRM_ACCOUNT = "crm_account"
    REVENUE_CLAIM = "revenue_claim"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"
    OVERDUE = "overdue"
    VOID = "void"
    UNKNOWN = "unknown"


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    UNKNOWN = "unknown"

    @property
    def is_successful(self) -> bool:
        """Only captured money is cash. spec §14: a failed payment contributes zero."""
        return self in {
            PaymentStatus.CAPTURED,
            PaymentStatus.REFUNDED,
            PaymentStatus.PARTIALLY_REFUNDED,
        }


class BillingFrequency(StrEnum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    ANNUAL = "annual"
    USAGE_BASED = "usage_based"
    UNKNOWN = "unknown"

    @property
    def periods_per_year(self) -> int | None:
        """Annualisation multiplier; None where normalisation is not meaningful."""
        return {
            BillingFrequency.MONTHLY: 12,
            BillingFrequency.QUARTERLY: 4,
            BillingFrequency.HALF_YEARLY: 2,
            BillingFrequency.ANNUAL: 1,
        }.get(self)

    @property
    def is_recurring(self) -> bool:
        return self.periods_per_year is not None


class TransactionDirection(StrEnum):
    CREDIT = "credit"   # money into the company's account
    DEBIT = "debit"     # money out


class AnomalySeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED_FOR_REVIEW = "paused_for_review"
    COMPLETED = "completed"
    FAILED = "failed"


class QuarantineReason(StrEnum):
    SCHEMA_INVALID = "schema_invalid"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_CURRENCY = "invalid_currency"
    INVALID_AMOUNT = "invalid_amount"
    INVALID_DATE = "invalid_date"
    DUPLICATE_RECORD = "duplicate_record"
    UNREADABLE_DOCUMENT = "unreadable_document"
    UNSAFE_FILE = "unsafe_file"


class ActorType(StrEnum):
    """Who performed an audited action."""

    SYSTEM = "system"
    AGENT = "agent"
    HUMAN = "human"


class UserRole(StrEnum):
    """RBAC roles (idea_features.md §17: founders, analysts, external reviewers)."""

    OWNER = "owner"          # founder; full control of their workspace
    ANALYST = "analyst"      # can resolve review items
    REVIEWER = "reviewer"    # external; read + comment, no material override
    ADMIN = "admin"          # platform operator
