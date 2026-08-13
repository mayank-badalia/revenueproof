"""Item-level revenue-state classification — F5 sub-features 3-5.

Assigns each revenue item exactly one of the eight states in idea_features.md §6.6,
using an ordered decision tree of deterministic rules.

**Order is the design.** The rules are checked in a fixed sequence and the first
match wins, which is what makes the eight states mutually exclusive. Refunds are
checked before anything else, because money returned is not revenue no matter how
complete the rest of the evidence looks — and that is precisely the item a company
is most likely to still be counting.

No LLM is involved. idea_features.md §14 is explicit that AI must not perform
calculations ordinary code handles reliably, and every decision here reduces to
comparing amounts, dates and the presence of evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.features.revenue.policy import EvidenceSet, RevenuePolicy, explain
from app.models.enums import EvidenceStrength, RevenueClass


@dataclass
class RevenueItemInput:
    """Everything known about one candidate revenue item."""

    item_id: str
    description: str
    currency: str
    # Gross amount before period allocation.
    gross_minor: int
    evidence: EvidenceSet

    customer_id: str | None = None
    customer_name: str | None = None
    contract_id: str | None = None
    invoice_id: str | None = None
    payment_ids: list[str] = field(default_factory=list)
    bank_ids: list[str] = field(default_factory=list)

    # From Feature 4.
    allocated_minor: int = 0
    retained_minor: int = 0
    refunded_minor: int = 0
    bank_confirmed_minor: int = 0

    # From Feature 3.
    contract_recurring_minor: int = 0
    contract_one_time_minor: int = 0
    contract_start: date | None = None
    contract_end: date | None = None
    billing_frequency: str = "unknown"
    # Portion of the contract's recurring value falling inside the period.
    in_period_minor: int = 0
    future_period_minor: int = 0
    annualised_recurring_minor: int = 0
    allocation_detail: dict[str, Any] = field(default_factory=dict)

    invoice_has_one_time_items: bool = False
    invoice_status: str = "unknown"


@dataclass
class Classification:
    """One item's verdict, with everything needed to defend it."""

    item_id: str
    classification: RevenueClass
    rule_id: str
    explanation: str
    # Amount attributable to the reporting period under this classification.
    recognized_minor: int
    gross_minor: int
    currency: str
    is_recurring: bool
    evidence_strength: EvidenceStrength
    evidence_ids: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    calculation_detail: dict[str, Any] = field(default_factory=dict)
    # Counts toward ARR only if recurring and verified. ARR belongs to the
    # *contract*, so the contract is carried here: totals sum it once per contract,
    # not once per invoice raised under it.
    arr_contribution_minor: int = 0
    contract_id: str | None = None
    is_material: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "classification": str(self.classification),
            "rule_id": self.rule_id,
            "explanation": self.explanation,
            "recognized_minor": self.recognized_minor,
            "gross_minor": self.gross_minor,
            "currency": self.currency,
            "is_recurring": self.is_recurring,
            "evidence_strength": str(self.evidence_strength),
            "evidence_ids": self.evidence_ids,
            "missing_evidence": self.missing_evidence,
            "calculation_detail": self.calculation_detail,
            "arr_contribution_minor": self.arr_contribution_minor,
            "contract_id": self.contract_id,
            "is_material": self.is_material,
        }


def classify(
    item: RevenueItemInput,
    policy: RevenuePolicy,
    *,
    claimed_revenue_minor: int = 0,
) -> Classification:
    """Assign exactly one revenue state. First matching rule wins."""
    evidence = item.evidence
    evidence_ids = _collect_evidence_ids(item)

    def build(
        classification: RevenueClass,
        rule_id: str,
        recognized: int,
        *,
        is_recurring: bool = False,
        detail: str = "",
        calculation: dict[str, Any] | None = None,
    ) -> Classification:
        result = Classification(
            item_id=item.item_id,
            contract_id=item.contract_id,
            classification=classification,
            rule_id=rule_id,
            explanation=explain(rule_id, detail),
            recognized_minor=max(0, recognized),
            gross_minor=item.gross_minor,
            currency=item.currency,
            is_recurring=is_recurring,
            evidence_strength=evidence.strength(policy),
            evidence_ids=evidence_ids,
            missing_evidence=evidence.missing_for(classification, policy),
            calculation_detail=calculation or {},
        )
        # ARR counts recurring components only (§8).
        if classification is RevenueClass.VERIFIED_RECURRING and is_recurring:
            result.arr_contribution_minor = _arr_contribution(item, policy)
        # Materiality decides whether Feature 7 must agree before publication.
        if claimed_revenue_minor > 0:
            share = (result.recognized_minor / claimed_revenue_minor) * 100
            result.is_material = share >= policy.materiality_pct
        return result

    # --- 0. Contradictions and unresolved identity outrank everything ------
    # spec §18: for uncertain cases the safe output is HUMAN_REVIEW, not a
    # confident guess.
    if evidence.has_contradiction:
        return build(
            RevenueClass.HUMAN_REVIEW,
            "R08_HUMAN_REVIEW_CONTRADICTION",
            0,
            detail=evidence.contradiction_detail,
        )

    if policy.require_resolved_customer and evidence.has_customer and not evidence.customer_resolved:
        return build(
            RevenueClass.HUMAN_REVIEW, "R09_HUMAN_REVIEW_UNRESOLVED_IDENTITY", 0
        )

    # --- 1. A void or draft invoice is not a claim on cash ------------------
    if evidence.has_invoice and not evidence.invoice_is_live:
        return build(
            RevenueClass.UNSUPPORTED_CLAIM,
            "R11_VOID_INVOICE",
            0,
            detail=f"Invoice status is '{item.invoice_status}'.",
        )

    # --- 2. Money returned is not revenue -----------------------------------
    # Checked before any verification rule: an item that was paid and then refunded
    # has complete-looking evidence, and is exactly what a company is most likely to
    # still be counting.
    if evidence.fully_refunded or (
        item.allocated_minor > 0 and item.retained_minor == 0
    ):
        return build(
            RevenueClass.REFUNDED_OR_REVERSED,
            "R01_FULLY_REFUNDED",
            0,
            detail=(
                f"{item.refunded_minor} of {item.allocated_minor} minor units "
                f"were returned."
            ),
            calculation={
                "allocated_minor": item.allocated_minor,
                "refunded_minor": item.refunded_minor,
                "retained_minor": item.retained_minor,
            },
        )

    # --- 3. Cash with no invoice behind it ----------------------------------
    # Checked BEFORE the verification rules. Ordered the other way round, a receipt
    # with no invoice and no contract matched "retained cash + successful payment"
    # and was classified as verified revenue — counting unexplained money as proven
    # revenue, which is the exact inversion this product exists to prevent.
    #
    # It also precedes the contract rules below, because those describe money that
    # never arrived. Money that *did* arrive is a different finding, and saying
    # "nothing has been received" about a receipt would be plainly false.
    if item.retained_minor > 0 and not evidence.has_invoice:
        return build(
            RevenueClass.PAYMENT_WITHOUT_SUPPORT,
            "R12_CASH_WITHOUT_INVOICE" if evidence.has_contract
            else "R06_PAYMENT_WITHOUT_SUPPORT",
            0,
            detail=f"{item.retained_minor} minor units received.",
            calculation={"retained_minor": item.retained_minor},
        )

    # --- 4. A contract entirely outside the period supports nothing (§14) ---
    if (
        evidence.has_contract
        and not evidence.contract_covers_period
        and not evidence.has_invoice
    ):
        return build(
            RevenueClass.CONTRACTED_UNPAID,
            "R10_OUTSIDE_PERIOD",
            0,
            detail=(
                f"Contract runs {item.contract_start} to {item.contract_end}, "
                f"outside the reporting period."
            ),
            calculation=item.allocation_detail,
        )

    # --- 5. Cash retained against real evidence: verified revenue -----------
    if item.retained_minor > 0 and evidence.payment_succeeded and evidence.has_invoice:
        recurring = _is_recurring(item, evidence, policy)

        # The recognised amount is the retained cash, capped at the portion of the
        # contract term falling inside the period. A prepaid annual contract does
        # not all belong to this period simply because the cash arrived in it.
        recognized = item.retained_minor
        capped_by_period = False
        if recurring and item.in_period_minor > 0:
            if item.in_period_minor < recognized:
                recognized = item.in_period_minor
                capped_by_period = True

        calculation = {
            "allocated_minor": item.allocated_minor,
            "refunded_minor": item.refunded_minor,
            "retained_minor": item.retained_minor,
            "recognized_minor": recognized,
            "capped_by_period_allocation": capped_by_period,
            "bank_confirmed_minor": item.bank_confirmed_minor,
            **item.allocation_detail,
        }

        if recurring:
            return build(
                RevenueClass.VERIFIED_RECURRING,
                "R02_VERIFIED_RECURRING",
                recognized,
                is_recurring=True,
                detail=(
                    f"Billing frequency {item.billing_frequency}; "
                    f"{'bank confirmed' if evidence.bank_confirmed else 'not bank confirmed'}."
                ),
                calculation=calculation,
            )

        # Not recurring. This is the Quantum Retail case: an implementation fee the
        # company presents as ARR. It is real revenue, but it is one-time.
        return build(
            RevenueClass.VERIFIED_ONE_TIME,
            "R03_VERIFIED_ONE_TIME",
            recognized,
            is_recurring=False,
            detail=_one_time_reason(item, evidence, policy),
            calculation=calculation,
        )

    # --- 6. Invoiced but not paid --------------------------------------------
    if evidence.has_invoice and item.retained_minor == 0:
        return build(
            RevenueClass.INVOICED_UNPAID,
            "R04_INVOICED_UNPAID",
            0,
            detail=f"Invoice status '{item.invoice_status}'; no cash received.",
            calculation={"invoice_total_minor": item.gross_minor},
        )

    # --- 7. Contracted but not invoiced --------------------------------------
    if evidence.has_contract and not evidence.has_invoice:
        return build(
            RevenueClass.CONTRACTED_UNPAID,
            "R05_CONTRACTED_UNPAID",
            0,
            detail=(
                f"Contract obliges {item.in_period_minor} minor units in this "
                f"period, none of it invoiced."
            ),
            calculation=item.allocation_detail,
        )

    # --- 8. Nothing at all ----------------------------------------------------
    return build(RevenueClass.UNSUPPORTED_CLAIM, "R07_UNSUPPORTED_CLAIM", 0)


def _is_recurring(
    item: RevenueItemInput, evidence: EvidenceSet, policy: RevenuePolicy
) -> bool:
    """Whether this item's revenue genuinely recurs.

    The contract decides, never the invoice description. spec §14: setup and
    implementation line items are not recurring unless the contract explicitly makes
    them so — and "Annual subscription — implementation programme" on an invoice is
    exactly the wording that makes a one-time fee look like ARR.
    """
    if policy.require_contract_for_recurring and not evidence.has_contract:
        return False
    if evidence.has_contract:
        if not evidence.contract_states_recurring:
            return False
        if not evidence.contract_covers_period:
            return False
        # A contract with both components, invoiced with a one-time line item, is
        # the one-time part of that contract.
        if item.invoice_has_one_time_items and item.contract_one_time_minor > 0:
            # Recurring only if the invoiced amount cannot be the one-time fee.
            return item.gross_minor > item.contract_one_time_minor
        return True
    return False


def _one_time_reason(
    item: RevenueItemInput, evidence: EvidenceSet, policy: RevenuePolicy
) -> str:
    if not evidence.has_contract:
        return "No contract establishes a recurring charge, so this is treated as one-time."
    if not evidence.contract_states_recurring:
        return "The contract states no recurring charge."
    if not evidence.contract_covers_period:
        return "The contract term does not cover the reporting period."
    if item.invoice_has_one_time_items and item.contract_one_time_minor > 0:
        return (
            f"The contract names a non-recurring fee of "
            f"{item.contract_one_time_minor} minor units, and this invoice matches it."
        )
    return "The contract does not make this charge recurring."


def _arr_contribution(item: RevenueItemInput, policy: RevenuePolicy) -> int:
    """Annualised run-rate contribution for a verified recurring item.

    Uses the contract's annualised figure rather than the cash received, because ARR
    is a forward run-rate: a customer who paid one month of a monthly contract
    contributes twelve months of ARR, not one.
    """
    if item.annualised_recurring_minor > 0:
        return item.annualised_recurring_minor
    # No contract annualisation available — fall back to the recognised amount,
    # which is conservative for anything billed more often than annually.
    return item.retained_minor


# ---------------------------------------------------------------------------
# Double-count prevention — sub-feature 5
# ---------------------------------------------------------------------------


def detect_double_counting(
    classifications: list[Classification],
    payment_retained: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Find cash counted more than once across verified items.

    spec §14: a payment cannot be counted twice across multiple invoices.

    The check that matters is *how much* of a payment is recognised, not how many
    items cite it. A single bank credit settling four invoices legitimately appears
    as evidence on all four — each taking its own share — and flagging that as a
    conflict produced 241 false positives on a dataset with no double-counting at
    all. Worse, a detector that cries wolf on every combined payment is one a
    reviewer learns to ignore, which is how a real double-count gets through.

    So two genuine failures are checked instead:

    * the same **invoice** supporting two verified items — always a construction bug;
    * recognised amounts against one payment exceeding what that payment retained.
    """
    conflicts: list[dict[str, Any]] = []
    verified = [c for c in classifications if c.classification.counts_as_verified]

    # 1. One invoice cannot support two verified items.
    invoice_owner: dict[str, str] = {}
    for classification in verified:
        for evidence_id in classification.evidence_ids:
            if not evidence_id.startswith("invoice:"):
                continue
            previous = invoice_owner.get(evidence_id)
            if previous and previous != classification.item_id:
                conflicts.append(
                    {
                        "evidence_id": evidence_id,
                        "items": [previous, classification.item_id],
                        "reason": (
                            "The same invoice supports two verified revenue items, "
                            "so its value is counted twice."
                        ),
                    }
                )
            else:
                invoice_owner[evidence_id] = classification.item_id

    # 2. Recognised amounts against one payment cannot exceed what it retained.
    if payment_retained:
        recognised_per_payment: dict[str, int] = {}
        items_per_payment: dict[str, list[str]] = {}
        for classification in verified:
            payments = [
                e[len("payment:"):]
                for e in classification.evidence_ids
                if e.startswith("payment:")
            ]
            if not payments:
                continue
            # Split the item's recognised amount across the payments that funded
            # it, matching how the available ceiling is derived. Charging each
            # payment the *full* amount instead looked conservative but was simply
            # wrong: an invoice settled by three instalments then billed every one
            # of them three times over and reported a conflict on clean data.
            share = classification.recognized_minor // len(payments)
            remainder = classification.recognized_minor - share * len(payments)
            for index, payment_id in enumerate(payments):
                recognised_per_payment[payment_id] = (
                    recognised_per_payment.get(payment_id, 0)
                    + share
                    + (remainder if index == 0 else 0)
                )
                items_per_payment.setdefault(payment_id, []).append(
                    classification.item_id
                )

        for payment_id, recognised in recognised_per_payment.items():
            available = payment_retained.get(payment_id)
            if available is None:
                continue
            if recognised > available:
                conflicts.append(
                    {
                        "evidence_id": f"payment:{payment_id}",
                        "items": items_per_payment[payment_id],
                        "reason": (
                            f"Verified revenue of {recognised} minor units is "
                            f"attributed to a payment that only retained "
                            f"{available}. The same cash is counted more than once."
                        ),
                    }
                )

    return conflicts


def _collect_evidence_ids(item: RevenueItemInput) -> list[str]:
    ids: list[str] = []
    if item.customer_id:
        ids.append(f"customer:{item.customer_id}")
    if item.contract_id:
        ids.append(f"contract:{item.contract_id}")
    if item.invoice_id:
        ids.append(f"invoice:{item.invoice_id}")
    ids.extend(f"payment:{p}" for p in item.payment_ids)
    ids.extend(f"bank:{b}" for b in item.bank_ids)
    return ids
