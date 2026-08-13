"""Customer concentration metrics — F6 sub-feature 5.

Top-1 share, top-N share and the Herfindahl-Hirschman Index over *verified* revenue
only. Measuring concentration on invoiced or contracted amounts would let an
unpaid bill make a company look more diversified than its cash says it is.

**The HHI thresholds are not the antitrust ones.** core_resoruces.md is explicit:
the US DOJ publishes the sum-of-squared-shares formula, and its 1,500/2,500 bands
describe market competition among many firms. A startup with eight customers is
mathematically "highly concentrated" by that scale and entirely normal by the
standards of its stage. The formula is borrowed; the interpretation is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.enums import AnomalySeverity

from .rules import CONCENTRATION_SINGLE_PCT, Finding, _finding, _money

#: Share of verified revenue held by the top customer, above which a reviewer
#: should understand the renewal risk. Chosen as a diligence prompt, not a
#: statistical claim: a quarter of revenue resting on one contract is a fact worth
#: knowing regardless of how normal it is at this stage.
TOP_N = 5


@dataclass
class ConcentrationResult:
    currency: str = "INR"
    total_verified_minor: int = 0
    customer_count: int = 0
    top_customer: str | None = None
    top_share_pct: float = 0.0
    top_n_share_pct: float = 0.0
    hhi: float = 0.0
    per_customer: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "total_verified_minor": self.total_verified_minor,
            "customer_count": self.customer_count,
            "top_customer": self.top_customer,
            "top_share_pct": self.top_share_pct,
            "top_n_share_pct": self.top_n_share_pct,
            "top_n": TOP_N,
            "hhi": self.hhi,
            "hhi_caveat": (
                "HHI uses the DOJ sum-of-squared-shares formula. Its antitrust "
                "thresholds describe competition between firms in a market and must "
                "not be read as startup risk bands."
            ),
            "per_customer": self.per_customer,
            "findings": [f.as_dict() for f in self.findings],
        }


def measure(
    verified_by_customer: dict[str, int],
    *,
    currency: str = "INR",
    customer_ids: dict[str, str] | None = None,
) -> ConcentrationResult:
    """Concentration over verified revenue, keyed by canonical customer name."""
    positive = {name: amount for name, amount in verified_by_customer.items() if amount > 0}
    total = sum(positive.values())
    result = ConcentrationResult(currency=currency, total_verified_minor=total)
    if total <= 0:
        return result

    ranked = sorted(positive.items(), key=lambda kv: (-kv[1], kv[0]))
    result.customer_count = len(ranked)
    result.per_customer = [
        {
            "customer": name,
            "amount_minor": amount,
            "share_pct": round(amount / total * 100, 2),
        }
        for name, amount in ranked
    ]

    result.top_customer, top_amount = ranked[0]
    result.top_share_pct = round(top_amount / total * 100, 2)
    result.top_n_share_pct = round(
        sum(a for _, a in ranked[:TOP_N]) / total * 100, 2
    )
    # HHI on percentage points, per the DOJ formula: sum of squared percentage
    # shares, so a monopoly scores 10,000.
    result.hhi = round(sum((a / total * 100) ** 2 for _, a in ranked), 1)

    if result.top_share_pct >= CONCENTRATION_SINGLE_PCT:
        result.findings.append(
            _finding(
                "A07_CUSTOMER_CONCENTRATION",
                AnomalySeverity.MEDIUM
                if result.top_share_pct < 50
                else AnomalySeverity.HIGH,
                explanation=(
                    f"{result.top_customer} accounts for {result.top_share_pct:.1f}% "
                    f"of verified revenue ({_money(top_amount, currency)} of "
                    f"{_money(total, currency)}) across {result.customer_count} "
                    f"paying customers."
                ),
                observed_value=f"{result.top_share_pct:.1f}% from one customer",
                baseline_value=f"{CONCENTRATION_SINGLE_PCT:.0f}% prompts a check",
                customer_id=(customer_ids or {}).get(result.top_customer),
                related_records=[{"type": "customer", "name": result.top_customer}],
                caveats=[
                    ("Concentration is a risk fact, not a defect — an early company "
                    "with few customers is concentrated by definition."),
                    ("Measured over verified revenue only, so an unpaid invoice "
                    "cannot make the book look more diversified than the cash does."),
                ],
            )
        )
    return result
