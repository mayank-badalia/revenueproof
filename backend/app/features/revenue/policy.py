"""Versioned revenue/ARR policy and evidence completeness — F5 sub-features 1-2.

**What this file is not.** It is not an accounting standard. core_resoruces.md is
explicit that IFRS 15 "is not an ARR definition and must not be represented as one",
and that a shippable ARR policy needs accountant review that no documentation can
substitute for. What this file provides is a *stated, versioned, testable* policy —
so that a reviewer can read the rules that produced a number, disagree with them, and
have the disagreement be about the policy rather than about hidden code.

Two ideas do the work:

* **A policy version is pinned to every result.** Change the rules and figures change;
  without a version stamped on the output, a report cannot be reproduced or compared.
* **Evidence completeness is a checklist, not a score.** Each revenue state names the
  evidence it requires. An item either has that evidence or it does not, and the
  missing pieces are listed by name rather than folded into a confidence percentage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.enums import EvidenceStrength, RevenueClass


@dataclass(frozen=True, slots=True)
class RevenuePolicy:
    """The rules that turn evidence into a revenue classification.

    Frozen and versioned. Every figure produced under it carries `version`, so a
    reviewer comparing two reports can tell whether the evidence changed or the
    rules did.
    """

    version: str = "v1"
    description: str = (
        "RevenueProof default policy. Cash must be retained after refunds to count "
        "as verified. Recurring status comes from the contract, never from an "
        "invoice description. Bank confirmation strengthens evidence but is not "
        "required, because not every workspace connects a bank feed."
    )

    # --- what counts as verified ---------------------------------------
    # A bank receipt makes evidence STRONG but is not mandatory: requiring it would
    # mark every workspace without a bank import as unsupported, which is a
    # statement about our integrations rather than about their revenue.
    require_bank_confirmation: bool = False
    # A contract is required before revenue can be called *recurring*. spec §14:
    # setup and implementation items are not recurring unless the contract says so,
    # and an invoice description is not a contract.
    require_contract_for_recurring: bool = True
    # An identity link that is still under review cannot support verified revenue
    # (idea_features.md §14).
    require_resolved_customer: bool = False

    # --- materiality -----------------------------------------------------
    # Items above this share of claimed revenue need critic agreement before they
    # can be published as verified (Feature 7).
    materiality_pct: float = 1.0

    # --- ARR ---------------------------------------------------------------
    # ARR counts recurring components only. One-time fees, future periods and
    # unsigned proposals are excluded (idea_features.md §8).
    arr_includes_one_time: bool = False
    arr_includes_future_periods: bool = False
    # A contract ending inside the period still contributes its annualised run-rate
    # up to the end date; it does not get extrapolated past termination.
    arr_extrapolates_past_contract_end: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "description": self.description,
            "require_bank_confirmation": self.require_bank_confirmation,
            "require_contract_for_recurring": self.require_contract_for_recurring,
            "require_resolved_customer": self.require_resolved_customer,
            "materiality_pct": self.materiality_pct,
            "arr_includes_one_time": self.arr_includes_one_time,
            "arr_includes_future_periods": self.arr_includes_future_periods,
            "arr_extrapolates_past_contract_end": self.arr_extrapolates_past_contract_end,
            "caveat": (
                "This is RevenueProof's stated policy, not an accounting standard. "
                "ARR is not defined by IFRS 15 or GAAP; these rules require review "
                "by a qualified accountant before any figure is relied upon "
                "externally."
            ),
        }


DEFAULT_POLICY = RevenuePolicy()

# Policies are addressed by version so a stored result can be re-derived under the
# exact rules that produced it.
POLICY_REGISTRY: dict[str, RevenuePolicy] = {DEFAULT_POLICY.version: DEFAULT_POLICY}


def get_policy(version: str | None = None) -> RevenuePolicy:
    return POLICY_REGISTRY.get(version or DEFAULT_POLICY.version, DEFAULT_POLICY)


# ---------------------------------------------------------------------------
# Evidence completeness — sub-feature 2
# ---------------------------------------------------------------------------


@dataclass
class EvidenceSet:
    """What evidence exists for one revenue item, and what is missing."""

    has_customer: bool = False
    customer_resolved: bool = False
    has_contract: bool = False
    contract_covers_period: bool = False
    contract_states_recurring: bool = False
    has_invoice: bool = False
    invoice_is_live: bool = False          # not void or draft
    has_payment: bool = False
    payment_succeeded: bool = False
    cash_retained: bool = False            # survived refunds
    bank_confirmed: bool = False
    fully_refunded: bool = False
    partially_refunded: bool = False
    # Set when sources contradict each other — routes to review regardless of the rest.
    has_contradiction: bool = False
    contradiction_detail: str = ""

    def missing_for(self, target: RevenueClass, policy: RevenuePolicy) -> list[str]:
        """Evidence this item lacks for a given classification.

        Named pieces rather than a percentage: "no bank receipt" tells a reviewer
        what to go and find, "68% confident" does not.
        """
        missing: list[str] = []

        if target in {RevenueClass.VERIFIED_RECURRING, RevenueClass.VERIFIED_ONE_TIME}:
            if not self.has_invoice:
                missing.append("no invoice")
            if not self.payment_succeeded:
                missing.append("no successful payment")
            if not self.cash_retained:
                missing.append("no cash retained after refunds")
            if policy.require_bank_confirmation and not self.bank_confirmed:
                missing.append("no independent bank confirmation")
            if policy.require_resolved_customer and not self.customer_resolved:
                missing.append("customer identity not resolved")

        if target is RevenueClass.VERIFIED_RECURRING:
            if policy.require_contract_for_recurring and not self.has_contract:
                missing.append("no contract establishing recurring terms")
            elif self.has_contract and not self.contract_states_recurring:
                missing.append("contract does not state a recurring charge")
            if self.has_contract and not self.contract_covers_period:
                missing.append("contract term does not cover the reporting period")

        if target is RevenueClass.CONTRACTED_UNPAID and not self.has_contract:
            missing.append("no contract")

        return missing

    def strength(self, policy: RevenuePolicy) -> EvidenceStrength:
        """Evidence completeness, per idea_features.md §9.

        This is a statement about how much evidence exists — not a probability that
        the company is honest.
        """
        if self.has_contradiction:
            return EvidenceStrength.DISPUTED
        if (
            self.has_contract
            and self.has_invoice
            and self.payment_succeeded
            and self.bank_confirmed
        ):
            return EvidenceStrength.STRONG
        if self.has_contract and self.has_invoice and self.payment_succeeded:
            return EvidenceStrength.MODERATE
        if self.has_invoice or self.has_contract:
            return EvidenceStrength.LIMITED
        return EvidenceStrength.LIMITED

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_customer": self.has_customer,
            "customer_resolved": self.customer_resolved,
            "has_contract": self.has_contract,
            "contract_covers_period": self.contract_covers_period,
            "contract_states_recurring": self.contract_states_recurring,
            "has_invoice": self.has_invoice,
            "invoice_is_live": self.invoice_is_live,
            "has_payment": self.has_payment,
            "payment_succeeded": self.payment_succeeded,
            "cash_retained": self.cash_retained,
            "bank_confirmed": self.bank_confirmed,
            "fully_refunded": self.fully_refunded,
            "partially_refunded": self.partially_refunded,
            "has_contradiction": self.has_contradiction,
            "contradiction_detail": self.contradiction_detail,
        }


# ---------------------------------------------------------------------------
# Rule catalogue
# ---------------------------------------------------------------------------

# Every classification cites one of these. A figure a reviewer cannot trace to a
# named, readable rule is exactly what this product exists to replace.
RULES: dict[str, str] = {
    "R01_FULLY_REFUNDED": (
        "Money was received and then fully returned, so no revenue is supported "
        "for this item."
    ),
    "R02_VERIFIED_RECURRING": (
        "A contract establishes a recurring charge covering the reporting period, "
        "an invoice was raised, payment succeeded, and the cash survived refunds."
    ),
    "R03_VERIFIED_ONE_TIME": (
        "An invoice was raised and paid, and the cash survived refunds, but the "
        "charge is not recurring under the contract."
    ),
    "R04_INVOICED_UNPAID": (
        "An invoice exists but no payment evidence supports it. An invoice is a "
        "claim on cash, not proof of it."
    ),
    "R05_CONTRACTED_UNPAID": (
        "A contract obliges the customer to pay, but nothing has been invoiced or "
        "received. Contracted value is never added to cash received."
    ),
    "R06_PAYMENT_WITHOUT_SUPPORT": (
        "Cash arrived with no invoice or contract to explain it."
    ),
    "R07_UNSUPPORTED_CLAIM": (
        "Claimed revenue with no contract, invoice or payment evidence behind it."
    ),
    "R08_HUMAN_REVIEW_CONTRADICTION": (
        "Sources contradict each other, so no confident classification is possible."
    ),
    "R09_HUMAN_REVIEW_UNRESOLVED_IDENTITY": (
        "The customer identity behind this item is still under review, and an "
        "unresolved match cannot support verified revenue."
    ),
    "R10_OUTSIDE_PERIOD": (
        "The contract term falls entirely outside the reporting period, so it "
        "supports no revenue in this period."
    ),
    "R11_VOID_INVOICE": (
        "The invoice is void or still in draft and is not a claim on cash."
    ),
    "R12_CASH_WITHOUT_INVOICE": (
        "Money arrived against a contract but nothing was ever invoiced for it, so "
        "there is no billing record tying the cash to what was sold."
    ),
}


def explain(rule_id: str, detail: str = "") -> str:
    base = RULES.get(rule_id, "No rule description available.")
    return f"{base} {detail}".strip()
