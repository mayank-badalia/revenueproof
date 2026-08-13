"""Canonical evidence schemas — Feature 1, sub-feature 5.

Every provider payload is mapped into one of these before it reaches any later
feature. The mapping is **deterministic code**, not an LLM: Razorpay's `amount` is
already an integer in paise and Zoho's `total` is already a decimal string, so
inferring those semantically would add a failure mode for no benefit.
core_resoruces.md is explicit that structured-output extraction is "useful only for
ambiguous semantic mapping; direct provider fields should be mapped deterministically."

Two rules run through all of these models:

* **Money is (minor units, currency).** Validators convert at the boundary and
  reject anything unparseable, so a bad amount is quarantined as one row rather
  than silently becoming 0 or NaN downstream.
* **Unknown is a value.** Where a provider does not supply a field, the model
  carries `None` and the record notes it. Nothing invents a plausible default —
  an invented date silently reassigns revenue to the wrong period.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.money import MoneyError, to_minor_units
from app.models.enums import (
    BillingFrequency,
    InvoiceStatus,
    PaymentStatus,
    RecordType,
    SourceSystem,
    TransactionDirection,
)


class CanonicalBase(BaseModel):
    """Shared provenance fields carried by every normalised record."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    source_system: SourceSystem
    # The provider's own identifier. Combined with source_system it is the natural
    # key used for idempotent upserts.
    source_id: Annotated[str, Field(min_length=1, max_length=300)]
    # Fields the provider did not supply, recorded rather than defaulted.
    unknown_fields: list[str] = Field(default_factory=list)

    def natural_key(self) -> str:
        return f"{self.source_system}:{self.source_id}"


def _coerce_money(value: Any, currency: str, field_name: str) -> int:
    """Convert a provider amount to minor units, or fail with a clear message."""
    try:
        return to_minor_units(value, currency)
    except MoneyError as exc:
        raise ValueError(f"{field_name}: {exc}") from exc


class CanonicalCustomer(CanonicalBase):
    """A customer as one source sees it — *not* yet a resolved identity.

    Deliberately pre-resolution. Feature 2 decides whether several of these are the
    same real customer; conflating them here would destroy the very evidence that
    decision needs.
    """

    record_type: Literal[RecordType.CUSTOMER] = RecordType.CUSTOMER

    display_name: Annotated[str, Field(min_length=1, max_length=300)]
    legal_name: Annotated[str | None, Field(max_length=300)] = None
    email: Annotated[str | None, Field(max_length=320)] = None
    phone: Annotated[str | None, Field(max_length=40)] = None
    website: Annotated[str | None, Field(max_length=300)] = None
    # GSTIN / PAN / VAT — the strongest deterministic identity signal available.
    tax_identifiers: list[str] = Field(default_factory=list)
    billing_address: Annotated[str | None, Field(max_length=500)] = None
    country: Annotated[str | None, Field(max_length=100)] = None
    raw_attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, value: str | None) -> str | None:
        return value.lower() if value else None

    @property
    def email_domain(self) -> str | None:
        """Domain half of the email — a mid-strength matching signal in Feature 2."""
        if not self.email or "@" not in self.email:
            return None
        domain = self.email.rsplit("@", 1)[1].strip().lower()
        # Free providers say nothing about company identity, so they are not a signal.
        if domain in {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}:
            return None
        return domain or None


class CanonicalLineItem(BaseModel):
    """One invoice line. `is_one_time_hint` drives a rule, not a conclusion."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    description: Annotated[str, Field(max_length=1000)] = ""
    quantity: Decimal = Decimal("1")
    unit_amount_minor: int = 0
    total_minor: int = 0
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    # Set by keyword heuristics at normalisation time. spec §14: setup and
    # implementation items are not recurring unless the contract says so. The
    # contract (Feature 3) is authoritative; this is only a prior.
    is_one_time_hint: bool = False
    # recurring | one_time | ambiguous | unknown. `ambiguous` means the description
    # mixes both vocabularies ("Annual subscription — implementation programme"),
    # which is how a one-time fee gets presented as ARR. It forces a contract check
    # rather than being silently resolved in either direction.
    classification_hint: Literal["recurring", "one_time", "ambiguous", "unknown"] = "unknown"


class CanonicalInvoice(CanonicalBase):
    """An invoice: a claim on cash, never evidence that cash arrived."""

    record_type: Literal[RecordType.INVOICE] = RecordType.INVOICE

    invoice_number: Annotated[str | None, Field(max_length=120)] = None
    customer_source_id: Annotated[str | None, Field(max_length=300)] = None
    customer_name: Annotated[str | None, Field(max_length=300)] = None

    issue_date: date | None = None
    due_date: date | None = None

    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    subtotal_minor: int = 0
    tax_minor: int = 0
    total_minor: int = 0
    # Provider-reported balance. Recorded but never trusted: Feature 4 recomputes
    # it from actual allocations, because a provider's "paid" flag can be set
    # manually without any money moving.
    amount_due_minor: int = 0

    status: InvoiceStatus = InvoiceStatus.UNKNOWN
    line_items: list[CanonicalLineItem] = Field(default_factory=list)
    reference: Annotated[str | None, Field(max_length=300)] = None
    notes: Annotated[str | None, Field(max_length=2000)] = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check_totals(self) -> CanonicalInvoice:
        if self.total_minor < 0:
            raise ValueError("invoice total cannot be negative; use a credit note")
        if self.amount_due_minor > self.total_minor:
            raise ValueError(
                f"amount_due ({self.amount_due_minor}) exceeds total ({self.total_minor})"
            )
        return self

    @property
    def has_one_time_items(self) -> bool:
        return any(item.is_one_time_hint for item in self.line_items)


class CanonicalCreditNote(CanonicalBase):
    """Accounting-side reversal. Missing these treats returned value as retained."""

    record_type: Literal[RecordType.CREDIT_NOTE] = RecordType.CREDIT_NOTE

    credit_note_number: Annotated[str | None, Field(max_length=120)] = None
    customer_source_id: Annotated[str | None, Field(max_length=300)] = None
    invoice_source_id: Annotated[str | None, Field(max_length=300)] = None
    issue_date: date | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    total_minor: int = 0
    reason: Annotated[str | None, Field(max_length=1000)] = None


class CanonicalPayment(CanonicalBase):
    """A processor payment event.

    `amount_minor` is gross. Processor fee and tax are withheld before settlement,
    which is why a bank receipt legitimately differs from the payment amount
    (idea_features.md §18). Feature 4 compares net figures, so both are preserved.
    """

    record_type: Literal[RecordType.PAYMENT] = RecordType.PAYMENT

    customer_source_id: Annotated[str | None, Field(max_length=300)] = None
    customer_name: Annotated[str | None, Field(max_length=300)] = None
    email: Annotated[str | None, Field(max_length=320)] = None
    phone: Annotated[str | None, Field(max_length=40)] = None

    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    amount_minor: int = 0
    fee_minor: int = 0
    tax_minor: int = 0
    amount_refunded_minor: int = 0

    status: PaymentStatus = PaymentStatus.UNKNOWN
    payment_time: datetime | None = None
    method: Annotated[str | None, Field(max_length=40)] = None
    # Free-text narration. The one field an LLM may interpret — and never one it
    # may use to override a structured amount, date or status.
    description: Annotated[str | None, Field(max_length=2000)] = None
    reference: Annotated[str | None, Field(max_length=300)] = None
    invoice_source_id: Annotated[str | None, Field(max_length=300)] = None

    settlement_id: Annotated[str | None, Field(max_length=200)] = None
    settled_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check_amounts(self) -> CanonicalPayment:
        if self.amount_minor < 0:
            raise ValueError("payment amount cannot be negative")
        if self.amount_refunded_minor > self.amount_minor:
            raise ValueError(
                f"refunded ({self.amount_refunded_minor}) exceeds "
                f"payment amount ({self.amount_minor})"
            )
        return self

    @property
    def net_amount_minor(self) -> int:
        """Gross less processor fee and tax — what should actually reach the bank."""
        return self.amount_minor - self.fee_minor - self.tax_minor

    @property
    def retained_minor(self) -> int:
        """Cash kept after refunds. spec §14: a failed payment contributes zero."""
        if not self.status.is_successful:
            return 0
        return self.amount_minor - self.amount_refunded_minor


class CanonicalRefund(CanonicalBase):
    """Money returned, including chargebacks."""

    record_type: Literal[RecordType.REFUND] = RecordType.REFUND

    payment_source_id: Annotated[str | None, Field(max_length=300)] = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    amount_minor: int = 0
    status: Annotated[str, Field(max_length=30)] = "processed"
    refund_time: datetime | None = None
    reason: Annotated[str | None, Field(max_length=1000)] = None
    # Disputes/chargebacks are reported separately by processors; a refund-only
    # integration misses them entirely.
    is_chargeback: bool = False

    @model_validator(mode="after")
    def _non_negative(self) -> CanonicalRefund:
        if self.amount_minor < 0:
            raise ValueError("refund amount cannot be negative")
        return self


class CanonicalBankTransaction(CanonicalBase):
    """Independent bank evidence — the strongest confirmation cash truly arrived."""

    record_type: Literal[RecordType.BANK_TRANSACTION] = RecordType.BANK_TRANSACTION

    # Hash of the account number, salted per workspace. The engine only needs to
    # know two rows share an account; the number itself is unnecessary PII.
    account_fingerprint: Annotated[str, Field(min_length=1, max_length=64)]
    transaction_date: date
    value_date: date | None = None

    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    amount_minor: int = 0
    direction: TransactionDirection = TransactionDirection.CREDIT

    counterparty: Annotated[str | None, Field(max_length=300)] = None
    reference: Annotated[str | None, Field(max_length=300)] = None
    narration: Annotated[str | None, Field(max_length=1000)] = None
    balance_after_minor: int | None = None

    @model_validator(mode="after")
    def _non_negative(self) -> CanonicalBankTransaction:
        # Direction carries the sign, so the amount itself is always positive.
        if self.amount_minor < 0:
            raise ValueError(
                "bank amount must be positive; use `direction` to express debit/credit"
            )
        return self


class CanonicalContractDocument(CanonicalBase):
    """A contract *file* located by the collector — terms are extracted in Feature 3.

    Feature 1's job stops at retrieving and hashing the document. Reading it is a
    different capability with different failure modes, and separating them keeps a
    parsing bug from looking like a collection failure.
    """

    record_type: Literal[RecordType.CONTRACT] = RecordType.CONTRACT

    file_name: Annotated[str, Field(min_length=1, max_length=500)]
    mime_type: Annotated[str | None, Field(max_length=120)] = None
    size_bytes: int | None = None
    modified_time: datetime | None = None
    web_link: Annotated[str | None, Field(max_length=1000)] = None
    folder_path: Annotated[str | None, Field(max_length=1000)] = None
    # Set once bytes are in the vault; None means metadata was listed but the file
    # was not (yet) downloaded.
    storage_key: Annotated[str | None, Field(max_length=500)] = None
    file_hash: Annotated[str | None, Field(max_length=64)] = None


class CanonicalCrmAccount(CanonicalBase):
    """Optional CRM context — a supporting identity signal, never proof."""

    record_type: Literal[RecordType.CRM_ACCOUNT] = RecordType.CRM_ACCOUNT

    name: Annotated[str, Field(min_length=1, max_length=300)]
    domain: Annotated[str | None, Field(max_length=300)] = None
    owner: Annotated[str | None, Field(max_length=200)] = None
    lifecycle_stage: Annotated[str | None, Field(max_length=100)] = None
    associated_deal_value_minor: int | None = None
    currency: Annotated[str, Field(min_length=3, max_length=3)] = "INR"
    raw_attributes: dict[str, Any] = Field(default_factory=dict)


# Union of everything a connector may emit.
CanonicalRecord = (
    CanonicalCustomer
    | CanonicalInvoice
    | CanonicalCreditNote
    | CanonicalPayment
    | CanonicalRefund
    | CanonicalBankTransaction
    | CanonicalContractDocument
    | CanonicalCrmAccount
)

# Maps a record type to its model, used by the quarantine validator to pick a schema.
RECORD_MODEL: dict[RecordType, type[CanonicalBase]] = {
    RecordType.CUSTOMER: CanonicalCustomer,
    RecordType.INVOICE: CanonicalInvoice,
    RecordType.CREDIT_NOTE: CanonicalCreditNote,
    RecordType.PAYMENT: CanonicalPayment,
    RecordType.REFUND: CanonicalRefund,
    # A dispute/chargeback validates as a refund: it removes cash in exactly the
    # same way, and modelling it separately would force every downstream
    # calculation to remember to subtract both.
    RecordType.DISPUTE: CanonicalRefund,
    RecordType.BANK_TRANSACTION: CanonicalBankTransaction,
    RecordType.CONTRACT: CanonicalContractDocument,
    RecordType.CRM_ACCOUNT: CanonicalCrmAccount,
}


__all__ = [
    "CanonicalBankTransaction",
    "CanonicalBase",
    "CanonicalContractDocument",
    "CanonicalCreditNote",
    "CanonicalCrmAccount",
    "CanonicalCustomer",
    "CanonicalInvoice",
    "CanonicalLineItem",
    "CanonicalPayment",
    "CanonicalRecord",
    "CanonicalRefund",
    "RECORD_MODEL",
    "BillingFrequency",
    "_coerce_money",
]
