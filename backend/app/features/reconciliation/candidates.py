"""Candidate generation for invoice/payment/bank links — F4 sub-feature 1.

Produces *plausible* links for the allocation solver to choose between. Nothing here
decides anything: a candidate is a hypothesis with a score and a reason, and the
solver in `allocation.py` picks the combination that satisfies every balance
constraint at least cost.

The scoring is deliberately deterministic and inspectable. core_resoruces.md is
explicit that "AI is limited to weak narrations/references; exact candidate filters
remain code" — an amount, a date window and an invoice number are facts, and a model
adds nothing but variance to comparing them.

Two shapes of evidence are generated:
  * **invoice ↔ payment** — did this receipt settle this bill?
  * **payment ↔ bank receipt** — did the processor actually pass the money on?

The second matters as much as the first. idea_features.md §18 lists processor fees
causing bank receipts to differ from gross payments, and "captured" is not "in the
bank" — a payment can be captured, never settle, and still look paid in a dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.models import BankTransaction, Invoice, Payment
from app.models.enums import PaymentStatus, TransactionDirection

# A payment normally lands within a few days of the invoice, but statements and
# reminders stretch this. Wide enough to catch real settlements, narrow enough that
# an unrelated payment a year later is not a candidate.
INVOICE_PAYMENT_WINDOW_DAYS = 120
# Processor settlement to bank is typically T+2 or T+3; allow generously for
# weekends, holidays and batch cycles.
SETTLEMENT_WINDOW_DAYS = 10

# Bank credit vs gross payment differ by the processor's fee and its tax. Anything
# inside this band is a plausible settlement of that payment.
MAX_FEE_FRACTION = 0.06
# Absolute tolerance for rounding differences between systems, in minor units (₹1).
ROUNDING_TOLERANCE_MINOR = 100


@dataclass
class LinkCandidate:
    """One proposed link, with the evidence that suggested it."""

    left_type: str          # invoice | payment
    left_id: str
    right_type: str         # payment | bank_transaction
    right_id: str
    # 0-1. Not a probability — a ranking of how well the evidence agrees.
    score: float
    reasons: list[str] = field(default_factory=list)
    # Highest-strength signal that fired, used as the allocation's `method`.
    method: str = "amount_date"
    amount_delta_minor: int = 0
    day_delta: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": {"type": self.left_type, "id": self.left_id},
            "right": {"type": self.right_type, "id": self.right_id},
            "score": round(self.score, 4),
            "method": self.method,
            "reasons": self.reasons,
            "amount_delta_minor": self.amount_delta_minor,
            "day_delta": self.day_delta,
        }


def _as_date(value: date | datetime | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def normalise_reference(value: str | None) -> str:
    """Strip formatting so `INV-2026-001` and `inv2026001` compare equal."""
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def reference_matches(invoice: Invoice, text: str | None) -> bool:
    """Whether a payment description or bank narration names this invoice.

    The strongest non-identifier signal available, and the one a founder would point
    at first. Requires a reasonably long invoice number so that a short one like "7"
    does not match any string containing a 7.
    """
    if not text:
        return False
    haystack = normalise_reference(text)
    for candidate in (invoice.invoice_number, invoice.source_id):
        needle = normalise_reference(candidate)
        if len(needle) >= 5 and needle in haystack:
            return True
    return False


# ---------------------------------------------------------------------------
# invoice ↔ payment
# ---------------------------------------------------------------------------


def score_invoice_payment(invoice: Invoice, payment: Payment) -> LinkCandidate | None:
    """Score one invoice/payment pair. None means "not a plausible link at all"."""
    # A failed payment moved no money, so it can never settle anything (spec §14).
    if not PaymentStatus(payment.status).is_successful:
        return None
    if invoice.currency != payment.currency:
        # Cross-currency settlement is real but needs an explicit FX decision;
        # `fx.py` handles it and creates the candidate there instead of guessing here.
        return None

    invoice_date = _as_date(invoice.issue_date)
    payment_date = _as_date(payment.payment_time)
    reasons: list[str] = []
    score = 0.0
    method = "amount_date"

    # --- date window ---------------------------------------------------
    day_delta: int | None = None
    if invoice_date and payment_date:
        day_delta = (payment_date - invoice_date).days
        # A payment materially *before* its invoice is suspicious rather than
        # impossible (advance payments exist), so it is allowed but not rewarded.
        if day_delta < -30 or day_delta > INVOICE_PAYMENT_WINDOW_DAYS:
            return None
        if 0 <= day_delta <= 45:
            score += 0.25
            reasons.append(f"paid {day_delta} days after the invoice date")
        else:
            score += 0.05
            reasons.append(f"payment is {day_delta} days from the invoice date")

    # --- explicit reference (strongest) ---------------------------------
    if reference_matches(invoice, payment.description) or reference_matches(
        invoice, payment.reference
    ):
        score += 0.5
        method = "exact_reference"
        reasons.append(f"payment text names invoice {invoice.invoice_number}")

    # --- customer agreement, and the hard filter that keeps the solver small ---
    #
    # An allocation never spans two customers, so a pair whose customers are known
    # and different is not a candidate at all. Without this the whole workspace
    # fuses into one connected component: 53 invoices x 56 payments produced ~940
    # links, and CP-SAT could not search that space inside its time limit — which
    # surfaced as an empty allocation, indistinguishable from "nothing matched".
    if (
        invoice.customer_entity_id
        and payment.customer_entity_id
        and invoice.customer_entity_id != payment.customer_entity_id
    ):
        return None

    if invoice.stated_customer_name and payment.stated_customer_name:
        from app.features.identity.identifiers import name_tokens

        invoice_tokens = name_tokens(invoice.stated_customer_name)
        payment_tokens = name_tokens(payment.stated_customer_name)
        # Before identity resolution has run, names are all there is. Sharing no
        # distinctive token at all means these are different customers.
        if invoice_tokens and payment_tokens and not (invoice_tokens & payment_tokens):
            return None

    if (
        invoice.customer_entity_id
        and payment.customer_entity_id
        and invoice.customer_entity_id == payment.customer_entity_id
    ):
        score += 0.3
        reasons.append("both resolve to the same customer")
    elif invoice.stated_customer_name and payment.stated_customer_name:
        from app.features.identity.identifiers import normalize_name

        if normalize_name(invoice.stated_customer_name) == normalize_name(
            payment.stated_customer_name
        ):
            score += 0.2
            reasons.append("customer names agree")

    # --- amount agreement -------------------------------------------------
    delta = payment.amount - invoice.total
    if abs(delta) <= ROUNDING_TOLERANCE_MINOR:
        score += 0.45
        if method == "amount_date":
            method = "exact_amount"
        reasons.append("payment amount equals the invoice total")
    elif payment.amount < invoice.total:
        # A partial payment is a first-class case, not a mismatch.
        score += 0.2
        reasons.append(
            f"partial: payment covers {payment.amount / invoice.total:.0%} of the invoice"
        )
    else:
        # One payment covering several invoices — the combined-payment case.
        score += 0.15
        reasons.append("payment exceeds this invoice; may cover several")

    if score < 0.25:
        return None

    return LinkCandidate(
        left_type="invoice",
        left_id=str(invoice.id),
        right_type="payment",
        right_id=str(payment.id),
        score=min(score, 1.0),
        reasons=reasons,
        method=method,
        amount_delta_minor=delta,
        day_delta=day_delta,
    )


def generate_invoice_payment_candidates(
    invoices: list[Invoice], payments: list[Payment]
) -> list[LinkCandidate]:
    """Every plausible invoice/payment pair, best first."""
    candidates: list[LinkCandidate] = []
    for invoice in invoices:
        # A voided or draft invoice is not a claim on cash and must not attract one.
        if invoice.status in {"void", "draft"}:
            continue
        for payment in payments:
            candidate = score_invoice_payment(invoice, payment)
            if candidate is not None:
                candidates.append(candidate)
    return sorted(candidates, key=lambda c: -c.score)


# ---------------------------------------------------------------------------
# payment ↔ bank receipt
# ---------------------------------------------------------------------------


def score_payment_bank(payment: Payment, bank: BankTransaction) -> LinkCandidate | None:
    """Did this bank credit settle this processor payment?

    The comparison is against the payment **net of processor fee and tax**, not
    gross. Comparing gross would reject every genuine settlement, and "the bank
    shows less than the payment" is a fee, not a discrepancy.
    """
    if bank.direction != TransactionDirection.CREDIT:
        return None
    if not PaymentStatus(payment.status).is_successful:
        return None
    if payment.currency != bank.currency:
        return None

    payment_date = _as_date(payment.payment_time)
    bank_date = _as_date(bank.transaction_date)
    if payment_date and bank_date:
        day_delta = (bank_date - payment_date).days
        # Money cannot reach the bank before it is taken.
        if day_delta < -1 or day_delta > SETTLEMENT_WINDOW_DAYS:
            return None
    else:
        day_delta = None

    net = payment.amount - payment.fee - payment.tax
    reasons: list[str] = []
    score = 0.0
    method = "settlement"

    delta = bank.amount - net
    if abs(delta) <= ROUNDING_TOLERANCE_MINOR:
        score += 0.6
        reasons.append("bank credit equals the payment net of processor fee and tax")
    elif payment.amount and abs(bank.amount - payment.amount) <= max(
        ROUNDING_TOLERANCE_MINOR, int(payment.amount * MAX_FEE_FRACTION)
    ):
        score += 0.35
        reasons.append(
            f"bank credit is within {MAX_FEE_FRACTION:.0%} of the gross payment "
            f"(consistent with a processor fee)"
        )
    else:
        return None

    if day_delta is not None:
        if 0 <= day_delta <= 4:
            score += 0.25
            reasons.append(f"settled {day_delta} day(s) after the payment")
        else:
            score += 0.1
            reasons.append(f"settled {day_delta} days after the payment")

    if payment.settlement_id and bank.reference:
        if normalise_reference(payment.settlement_id) in normalise_reference(bank.reference):
            score += 0.3
            method = "exact_settlement_id"
            reasons.append(f"bank reference names settlement {payment.settlement_id}")

    return LinkCandidate(
        left_type="payment",
        left_id=str(payment.id),
        right_type="bank_transaction",
        right_id=str(bank.id),
        score=min(score, 1.0),
        reasons=reasons,
        method=method,
        amount_delta_minor=delta,
        day_delta=day_delta,
    )


def generate_payment_bank_candidates(
    payments: list[Payment], bank_rows: list[BankTransaction]
) -> list[LinkCandidate]:
    candidates: list[LinkCandidate] = []
    for payment in payments:
        for bank in bank_rows:
            candidate = score_payment_bank(payment, bank)
            if candidate is not None:
                candidates.append(candidate)
    return sorted(candidates, key=lambda c: -c.score)


def unmatched_bank_credits(
    bank_rows: list[BankTransaction], matched_ids: set[str]
) -> list[BankTransaction]:
    """Incoming money with no processor payment behind it.

    These are the receipts that become `PAYMENT_WITHOUT_SUPPORT` in Feature 5 — cash
    arriving with no invoice or contract to explain it, which is exactly what a
    reviewer is looking for.
    """
    return [
        row
        for row in bank_rows
        if row.direction == TransactionDirection.CREDIT
        and str(row.id) not in matched_ids
    ]
