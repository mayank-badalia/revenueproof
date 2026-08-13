"""Deterministic discrepancy rules — F6 sub-feature 1.

Every pattern idea_features.md §6 names, expressed as code that can be read,
argued with and re-run to the same answer. No model is consulted: whether two
payments are near-duplicates, whether a refund followed a payment within a day, or
whether a period-end spike exceeds a robust baseline are all questions arithmetic
settles.

**These are investigation prompts, not accusations.** core_resoruces.md is explicit
that findings must say "anomaly indicator, requires review" and never "fraud" — so
every rule states what was *observed*, what the *baseline* was, and what a human
should go and check. A rule that cannot name its baseline has no business firing.

The costly mistake here is not a missed pattern; it is a detector that fires so
often a reviewer learns to scroll past it. Feature 5 already produced 241 false
double-count flags on clean data for exactly that reason. Each rule below therefore
carries an explicit threshold, and each threshold has a stated justification.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.models.enums import AnomalySeverity

# ---------------------------------------------------------------------------
# Thresholds — every one of them stated, not buried
# ---------------------------------------------------------------------------

#: Two payments from the same customer for the same amount inside this window are
#: near-duplicates worth a look. A day is deliberately tight: legitimate monthly
#: billing repeats at ~30 days, and a genuine retry happens within minutes.
DUPLICATE_WINDOW = timedelta(days=1)

#: A refund this soon after capture suggests the charge should not have been taken.
#: Beyond it, refunds are ordinary customer service and flagging them is noise.
RAPID_REFUND_WINDOW = timedelta(days=3)

#: Days before period end that count as "period end" for spike detection.
PERIOD_END_WINDOW_DAYS = 14

#: How many robust deviations above the median a period-end total must sit before
#: it is unusual. 3.0 on the MAD scale is the conventional outlier threshold and is
#: far less sensitive to a single large invoice than a standard deviation would be.
SPIKE_MAD_THRESHOLD = 3.0

#: Below this many periods there is no baseline worth comparing against, and a
#: "spike" is just the only data point.
SPIKE_MINIMUM_PERIODS = 4

#: One customer above this share of verified revenue is a concentration finding.
#: Not a legal threshold — see concentration.py on why the antitrust HHI bands
#: must not be reused here.
CONCENTRATION_SINGLE_PCT = 25.0

#: Distinct customers settling from one account before it is worth explaining. Two
#: is the threshold because two is the case: a payment agent acting for two
#: unrelated companies, or a parent paying its subsidiary's bill, both change who
#: the economic counterparty is and both show up as exactly two. Severity is graded
#: rather than the rule silenced — two is a question, more is a finding.
SHARED_ACCOUNT_PAYER_THRESHOLD = 2

#: Invoice amendments beyond this count on a single invoice suggest the figure was
#: being worked toward rather than recorded.
INVOICE_CHURN_THRESHOLD = 2


RULES: dict[str, tuple[str, str]] = {
    "A01_DUPLICATE_PAYMENT": (
        "Possible duplicate payment",
        ("Two payments of the same amount from the same customer within a day. "
        "Check whether one was a retry that should have been voided."),
    ),
    "A02_RAPID_REFUND": (
        "Payment refunded almost immediately",
        ("Cash arrived and was returned within days. Check whether the charge was "
        "taken in error, and whether the revenue was reported before the reversal."),
    ),
    "A03_PERIOD_END_SPIKE": (
        "Revenue concentrated at period end",
        ("Invoicing in the closing days of the period is far above this company's "
        "own typical level. Check the shipment or delivery dates behind them."),
    ),
    "A04_ONE_TIME_AS_ARR": (
        "One-time fee described as recurring",
        ("An invoice presents a non-recurring charge in recurring language while the "
        "contract makes it one-time. Check which figure reached the ARR claim."),
    ),
    "A05_FUTURE_PERIOD_IN_CURRENT": (
        "Contract value from a future period",
        ("A contract whose term starts after this reporting period contributes value "
        "to it. Check the service start date."),
    ),
    "A06_SHARED_PAYMENT_ACCOUNT": (
        "Several customers paying from one account",
        ("Distinct customers settle from the same bank account or card. Check whether "
        "they are one economic party, or a payment agent acting for several."),
    ),
    "A07_CUSTOMER_CONCENTRATION": (
        "Revenue concentrated in one customer",
        ("A single customer accounts for an unusually large share of verified "
        "revenue. Check the contract term and renewal risk behind it."),
    ),
    "A08_INVOICE_CHURN": (
        "Invoice amended repeatedly",
        ("The same invoice changed several times after issue. Check what drove each "
        "revision and which version the revenue figure used."),
    ),
    "A09_UNUSUAL_REFUND_RATE": (
        "Refund rate above this company's norm",
        ("This customer's refunds are far above the workspace's own refund rate. "
        "Check for disputed delivery or a cancelled engagement."),
    ),
    "A10_RELATED_PARTY_REVENUE": (
        "Revenue from a potentially related party",
        ("Evidence links the payer to the company or to another customer. Related-"
        "party revenue is not improper, but it is not third-party validation."),
    ),
    "A11_CIRCULAR_FUNDS": (
        "Funds appear to move in a circle",
        ("A directed path returns money to its origin. Check whether the same cash "
        "is being recycled to create the appearance of revenue."),
    ),
    "A12_STATISTICAL_OUTLIER": (
        "Statistically unusual against this company's own history",
        ("An explainable model ranked this record as unlike the workspace's normal "
        "pattern. A score is a reason to look, never a finding on its own."),
    ),
    "A13_SUSPECTED_DUPLICATE_CUSTOMER": (
        "Two customer records may be one customer",
        ("Two records share evidence and read as the same name. Confirm whether "
        "identity resolution should merge them — an unmerged customer understates "
        "concentration and splits its revenue in two."),
    ),
}


@dataclass
class Finding:
    """One investigation prompt, with everything a reviewer needs to resolve it."""

    rule_id: str
    title: str
    severity: AnomalySeverity
    explanation: str
    required_check: str
    observed_value: str | None = None
    baseline_value: str | None = None
    related_records: list[dict[str, str]] = field(default_factory=list)
    customer_id: str | None = None
    caveats: list[str] = field(default_factory=list)
    graph_path: list[dict[str, Any]] = field(default_factory=list)
    model_version: str | None = None
    model_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": str(self.severity),
            "explanation": self.explanation,
            "required_check": self.required_check,
            "observed_value": self.observed_value,
            "baseline_value": self.baseline_value,
            "related_records": self.related_records,
            "customer_id": self.customer_id,
            "caveats": self.caveats,
            "graph_path": self.graph_path,
            "model_version": self.model_version,
            "model_score": self.model_score,
        }


def _finding(rule_id: str, severity: AnomalySeverity, **kwargs: Any) -> Finding:
    title, required_check = RULES[rule_id]
    return Finding(
        rule_id=rule_id,
        title=title,
        severity=severity,
        required_check=required_check,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Inputs — plain records so the rules stay testable without a database
# ---------------------------------------------------------------------------


@dataclass
class PaymentRecord:
    id: str
    customer_id: str | None
    customer_name: str | None
    amount_minor: int
    currency: str
    status: str
    captured_at: datetime | None
    refunded_minor: int = 0
    account_fingerprint: str | None = None
    reference: str | None = None


@dataclass
class RefundRecord:
    id: str
    payment_id: str | None
    amount_minor: int
    refunded_at: datetime | None


@dataclass
class InvoiceRecord:
    id: str
    number: str | None
    customer_id: str | None
    customer_name: str | None
    total_minor: int
    issued_on: date | None
    description: str | None = None
    one_time_hint: bool = False
    version_count: int = 1


@dataclass
class ContractRecord:
    id: str
    document_name: str
    customer_id: str | None
    start_date: date | None
    end_date: date | None
    recurring_minor: int = 0
    one_time_minor: int = 0
    future_period_minor: int = 0
    in_period_minor: int = 0


def _money(minor: int, currency: str = "INR") -> str:
    return f"{currency} {minor / 100:,.2f}"


# ---------------------------------------------------------------------------
# A01 — duplicate and near-duplicate payments
# ---------------------------------------------------------------------------


def detect_duplicate_payments(payments: list[PaymentRecord]) -> list[Finding]:
    """Same customer, same amount, inside a day.

    All three conditions are required. core_resoruces.md is explicit that text
    similarity alone cannot create a finding, and the same logic applies to amount
    alone: a company billing twenty customers ₹75,000 a month is not producing
    twenty duplicates.
    """
    findings: list[Finding] = []
    grouped: dict[tuple[str, int], list[PaymentRecord]] = defaultdict(list)
    for payment in payments:
        if payment.captured_at is None or payment.amount_minor <= 0:
            continue
        key = (payment.customer_id or payment.customer_name or payment.id, payment.amount_minor)
        grouped[key].append(payment)

    for (_, amount), group in sorted(grouped.items(), key=lambda kv: str(kv[0])):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda p: (p.captured_at, p.id))
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            gap = later.captured_at - earlier.captured_at
            if gap > DUPLICATE_WINDOW:
                continue
            findings.append(
                _finding(
                    "A01_DUPLICATE_PAYMENT",
                    AnomalySeverity.HIGH,
                    explanation=(
                        f"{_money(amount, earlier.currency)} was captured twice from "
                        f"{earlier.customer_name or 'the same customer'} "
                        f"{_gap_phrase(gap)} apart."
                    ),
                    observed_value=f"2 payments of {_money(amount, earlier.currency)}",
                    baseline_value=f"one payment per charge within {DUPLICATE_WINDOW.days} day",
                    customer_id=earlier.customer_id,
                    related_records=[
                        {"type": "payment", "id": earlier.id},
                        {"type": "payment", "id": later.id},
                    ],
                    caveats=[
                        ("A legitimate retry after a failed capture looks identical "
                        "here; the processor's own retry metadata settles it.")
                    ],
                )
            )
    return findings


def _gap_phrase(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    return f"{hours} hours" if hours < 48 else f"{hours // 24} days"


# ---------------------------------------------------------------------------
# A02 — payment followed by a rapid refund
# ---------------------------------------------------------------------------


def detect_rapid_refunds(
    payments: list[PaymentRecord], refunds: list[RefundRecord]
) -> list[Finding]:
    by_payment = {p.id: p for p in payments}
    findings: list[Finding] = []
    for refund in sorted(refunds, key=lambda r: r.id):
        payment = by_payment.get(refund.payment_id or "")
        if payment is None or payment.captured_at is None or refund.refunded_at is None:
            continue
        gap = refund.refunded_at - payment.captured_at
        if gap < timedelta(0) or gap > RAPID_REFUND_WINDOW:
            continue
        findings.append(
            _finding(
                "A02_RAPID_REFUND",
                AnomalySeverity.MEDIUM,
                explanation=(
                    f"{_money(refund.amount_minor, payment.currency)} was refunded "
                    f"{_gap_phrase(gap)} after capture."
                ),
                observed_value=f"refunded after {_gap_phrase(gap)}",
                baseline_value=f"refunds beyond {RAPID_REFUND_WINDOW.days} days are routine",
                customer_id=payment.customer_id,
                related_records=[
                    {"type": "payment", "id": payment.id},
                    {"type": "refund", "id": refund.id},
                ],
                caveats=[
                    ("A prompt refund is often good customer service. It matters only "
                    "if the revenue was reported before the reversal.")
                ],
            )
        )
    return findings


# ---------------------------------------------------------------------------
# A03 — end-of-period revenue spikes
# ---------------------------------------------------------------------------


def detect_period_end_spike(
    invoices: list[InvoiceRecord], period_start: date, period_end: date
) -> list[Finding]:
    """Compare the closing fortnight against this company's own monthly pattern.

    The baseline is the median absolute deviation rather than a standard deviation:
    with a dozen periods, one large invoice moves a standard deviation enough to
    hide the very spike being looked for. MAD is the robust equivalent and is what
    core_resoruces.md ranks first for exactly this reason.
    """
    dated = [i for i in invoices if i.issued_on and period_start <= i.issued_on <= period_end]
    if not dated:
        return []

    monthly: dict[tuple[int, int], int] = defaultdict(int)
    for invoice in dated:
        monthly[(invoice.issued_on.year, invoice.issued_on.month)] += invoice.total_minor
    if len(monthly) < SPIKE_MINIMUM_PERIODS:
        return []

    window_start = period_end - timedelta(days=PERIOD_END_WINDOW_DAYS)
    window_total = sum(i.total_minor for i in dated if i.issued_on >= window_start)
    if window_total <= 0:
        return []

    values = sorted(monthly.values())
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)
    # A perfectly flat history has MAD 0; any excess would then divide by zero.
    # Falling back to the median keeps the comparison meaningful instead of
    # declaring every non-zero closing fortnight a spike.
    scale = mad if mad > 0 else median
    if scale <= 0:
        return []

    ratio = (window_total - median) / scale
    if ratio < SPIKE_MAD_THRESHOLD:
        return []

    return [
        _finding(
            "A03_PERIOD_END_SPIKE",
            AnomalySeverity.HIGH,
            explanation=(
                f"{_money(window_total)} was invoiced in the final "
                f"{PERIOD_END_WINDOW_DAYS} days, against a typical month of "
                f"{_money(int(median))} — {ratio:.1f} robust deviations above it."
            ),
            observed_value=_money(window_total),
            baseline_value=f"median month {_money(int(median))}",
            related_records=[
                {"type": "invoice", "id": i.id}
                for i in dated
                if i.issued_on >= window_start
            ],
            caveats=[
                ("Seasonal businesses and annual renewal dates produce genuine "
                "period-end concentration."),
                f"Baseline drawn from {len(monthly)} months of this workspace only.",
            ],
        )
    ]


# ---------------------------------------------------------------------------
# A04 / A05 — one-time fees as ARR, future periods in current revenue
# ---------------------------------------------------------------------------


def detect_one_time_as_arr(
    invoices: list[InvoiceRecord], contracts: list[ContractRecord]
) -> list[Finding]:
    """The §19 case: an implementation fee invoiced as "Annual subscription".

    The invoice's own wording is only a hint — the contract decides. A finding is
    raised where the two disagree, because that disagreement is precisely how a
    one-time fee reaches an ARR claim.
    """
    # A customer often has several contracts — a subscription agreement and a
    # separate statement of work, say. The one this rule is about is whichever
    # establishes a non-recurring obligation, so index on that rather than on
    # whichever happens to sort last. Keying blindly let a ₹0 subscription contract
    # shadow the SOW carrying the one-time fee, and the rule then found nothing to
    # disagree with.
    by_customer: dict[str, ContractRecord] = {}
    for candidate in contracts:
        if not candidate.customer_id:
            continue
        held = by_customer.get(candidate.customer_id)
        if held is None or candidate.one_time_minor > held.one_time_minor:
            by_customer[candidate.customer_id] = candidate

    findings: list[Finding] = []
    for invoice in sorted(invoices, key=lambda i: i.id):
        if not invoice.one_time_hint:
            continue
        contract = by_customer.get(invoice.customer_id or "")
        if contract is None or contract.one_time_minor <= 0:
            continue
        findings.append(
            _finding(
                "A04_ONE_TIME_AS_ARR",
                AnomalySeverity.HIGH,
                explanation=(
                    f"Invoice {invoice.number or invoice.id} reads "
                    f"{invoice.description or 'as recurring'}, while "
                    f"{contract.document_name} makes "
                    f"{_money(contract.one_time_minor)} of it non-recurring."
                ),
                observed_value=f"invoice total {_money(invoice.total_minor)}",
                baseline_value=(
                    f"contract: {_money(contract.recurring_minor)} recurring, "
                    f"{_money(contract.one_time_minor)} one-time"
                ),
                customer_id=invoice.customer_id,
                related_records=[
                    {"type": "invoice", "id": invoice.id},
                    {"type": "contract", "id": contract.id},
                ],
                caveats=[
                    ("Wording alone proves nothing; the contract clause is the "
                    "authority and is cited on the contract record.")
                ],
            )
        )
    return findings


def detect_future_period_value(contracts: list[ContractRecord]) -> list[Finding]:
    findings: list[Finding] = []
    for contract in sorted(contracts, key=lambda c: c.id):
        if contract.future_period_minor <= 0:
            continue
        findings.append(
            _finding(
                "A05_FUTURE_PERIOD_IN_CURRENT",
                AnomalySeverity.MEDIUM,
                explanation=(
                    f"{contract.document_name} carries "
                    f"{_money(contract.future_period_minor)} of value dated after "
                    f"this reporting period"
                    + (f", starting {contract.start_date}." if contract.start_date else ".")
                ),
                observed_value=_money(contract.future_period_minor),
                baseline_value="value belonging to the reporting period only",
                customer_id=contract.customer_id,
                related_records=[{"type": "contract", "id": contract.id}],
                caveats=[
                    ("RevenueProof already excludes this from supported ARR; the "
                    "finding exists in case the founder's own figure did not.")
                ],
            )
        )
    return findings


# ---------------------------------------------------------------------------
# A06 — several customers paying from one account
# ---------------------------------------------------------------------------


def detect_shared_payment_accounts(payments: list[PaymentRecord]) -> list[Finding]:
    by_account: dict[str, set[str]] = defaultdict(set)
    examples: dict[str, list[PaymentRecord]] = defaultdict(list)
    for payment in payments:
        if not payment.account_fingerprint:
            continue
        name = payment.customer_name or payment.customer_id
        if not name:
            continue
        by_account[payment.account_fingerprint].add(name)
        examples[payment.account_fingerprint].append(payment)

    findings: list[Finding] = []
    for fingerprint, customers in sorted(by_account.items()):
        # Two *is* the case worth reporting. idea_features.md §Feature 6 asks for
        # "multiple customers paying from one account" and §19 encodes exactly two —
        # one agent settling for Crestview and Pinnacle — so a rule that waits for a
        # third cannot see the case the dataset was built around. The earlier
        # reasoning (a parent paying for a subsidiary is ordinary) is right about
        # *noise* and wrong about the remedy: the answer is to grade the severity,
        # not to be blind. Two customers is a question; five is a finding.
        if len(customers) < SHARED_ACCOUNT_PAYER_THRESHOLD:
            continue
        findings.append(
            _finding(
                "A06_SHARED_PAYMENT_ACCOUNT",
                AnomalySeverity.MEDIUM
                if len(customers) == SHARED_ACCOUNT_PAYER_THRESHOLD
                else AnomalySeverity.HIGH,
                explanation=(
                    f"{len(customers)} distinct customers settle from one account: "
                    f"{', '.join(sorted(customers)[:6])}."
                ),
                observed_value=f"{len(customers)} customers, 1 account",
                baseline_value="a customer normally settles from its own account",
                related_records=[
                    {"type": "payment", "id": p.id} for p in examples[fingerprint][:10]
                ],
                caveats=[
                    ("A payment agent or a group treasury function explains this "
                    "legitimately, and the account is stored only as a salted "
                    "fingerprint — never the account number itself.")
                ],
            )
        )
    return findings


# ---------------------------------------------------------------------------
# A08 / A09 — invoice churn and refund behaviour
# ---------------------------------------------------------------------------


def detect_invoice_churn(invoices: list[InvoiceRecord]) -> list[Finding]:
    findings: list[Finding] = []
    for invoice in sorted(invoices, key=lambda i: i.id):
        if invoice.version_count <= INVOICE_CHURN_THRESHOLD:
            continue
        findings.append(
            _finding(
                "A08_INVOICE_CHURN",
                AnomalySeverity.MEDIUM,
                explanation=(
                    f"Invoice {invoice.number or invoice.id} has "
                    f"{invoice.version_count} recorded versions."
                ),
                observed_value=f"{invoice.version_count} versions",
                baseline_value=f"{INVOICE_CHURN_THRESHOLD} or fewer",
                customer_id=invoice.customer_id,
                related_records=[{"type": "invoice", "id": invoice.id}],
                caveats=[
                    ("Version count comes from the provenance vault, so a corrected "
                    "typo counts the same as a repriced line.")
                ],
            )
        )
    return findings


def detect_unusual_refund_rate(payments: list[PaymentRecord]) -> list[Finding]:
    """Compare each customer's refund rate against the workspace's own rate.

    Deliberately relative. An absolute threshold would flag every customer of a
    business with a generous returns policy, and none of a business where a single
    reversal is extraordinary.
    """
    total_captured = sum(p.amount_minor for p in payments if p.amount_minor > 0)
    total_refunded = sum(p.refunded_minor for p in payments)
    if total_captured <= 0:
        return []
    workspace_rate = total_refunded / total_captured

    per_customer: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    names: dict[str, str] = {}
    for payment in payments:
        key = payment.customer_id or payment.customer_name or payment.id
        names[key] = payment.customer_name or key
        per_customer[key][0] += payment.amount_minor
        per_customer[key][1] += payment.refunded_minor

    findings: list[Finding] = []
    for key, (captured, refunded) in sorted(per_customer.items()):
        if captured <= 0 or refunded <= 0:
            continue
        rate = refunded / captured
        # Twice the workspace rate *and* a materially different absolute level:
        # a customer at 4% against a 2% workspace rate is noise.
        if rate < max(2 * workspace_rate, 0.25):
            continue
        findings.append(
            _finding(
                "A09_UNUSUAL_REFUND_RATE",
                AnomalySeverity.MEDIUM,
                explanation=(
                    f"{names[key]} had {rate * 100:.0f}% of cash refunded "
                    f"({_money(refunded)} of {_money(captured)}), against a "
                    f"workspace rate of {workspace_rate * 100:.0f}%."
                ),
                observed_value=f"{rate * 100:.0f}% refunded",
                baseline_value=f"workspace {workspace_rate * 100:.0f}%",
                customer_id=key if key in names else None,
                related_records=[
                    {"type": "payment", "id": p.id}
                    for p in payments
                    if (p.customer_id or p.customer_name or p.id) == key and p.refunded_minor
                ],
                caveats=[
                    ("A single large reversal dominates this ratio in a small "
                    "workspace.")
                ],
            )
        )
    return findings


def run_all(
    *,
    payments: list[PaymentRecord],
    refunds: list[RefundRecord],
    invoices: list[InvoiceRecord],
    contracts: list[ContractRecord],
    period_start: date,
    period_end: date,
) -> list[Finding]:
    """Every deterministic rule, in a stable order so runs are comparable."""
    findings: list[Finding] = []
    findings += detect_duplicate_payments(payments)
    findings += detect_rapid_refunds(payments, refunds)
    findings += detect_period_end_spike(invoices, period_start, period_end)
    findings += detect_one_time_as_arr(invoices, contracts)
    findings += detect_future_period_value(contracts)
    findings += detect_shared_payment_accounts(payments)
    findings += detect_invoice_churn(invoices)
    findings += detect_unusual_refund_rate(payments)
    return findings
