"""Conditional statistical anomaly scoring — F6 sub-features 2 and 7.

An Isolation Forest ranks records that are unlike the workspace's own history. Two
constraints shape everything here, and both come straight from core_resoruces.md:

* **Below a data-volume threshold the model stays off.** "Disable ML for small
  datasets rather than presenting an unstable score." A forest fitted on thirty
  payments will happily rank one of them anomalous, and that ranking is noise
  wearing a number. Reporting "not enough history" is the honest output.
* **Validation is time-aware.** `TimeSeriesSplit` exists to stop future periods
  leaking into the assessment of past ones. A model validated on shuffled folds
  looks better than it is, precisely because financial behaviour drifts.

The score is never a finding by itself. It is a reason to look, attached to records
a reviewer can open — which is why every scored record carries the features that
produced it, and why the model version and contamination setting are pinned to each
finding so a figure can be re-derived under the exact model that produced it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.models.enums import AnomalySeverity

from .rules import Finding, PaymentRecord, _finding, _money

#: Features are named here rather than inferred, so an explanation can say which
#: input made a record unusual instead of pointing at a column index.
FEATURE_NAMES = (
    "amount_minor",
    "refund_ratio",
    "hour_of_day",
    "day_of_month",
    "days_since_previous_payment",
    "customer_payment_count",
)

#: Expected share of anomalies. Deliberately low: this ranks the few worth a
#: reviewer's limited time, and a higher setting simply manufactures more work.
CONTAMINATION = 0.05

#: Fixed so two runs over the same evidence produce the same ranking. An audit
#: trail whose findings move between identical runs is not an audit trail.
RANDOM_STATE = 20260401


@dataclass
class ScoringResult:
    """What the scorer did, including the case where it declined to run."""

    enabled: bool = False
    reason: str = ""
    model_version: str | None = None
    records_scored: int = 0
    findings: list[Finding] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    feature_names: tuple[str, ...] = FEATURE_NAMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "model_version": self.model_version,
            "records_scored": self.records_scored,
            "findings": [f.as_dict() for f in self.findings],
            "validation": self.validation,
            "feature_names": list(self.feature_names),
        }


def _features(payments: list[PaymentRecord]) -> tuple[list[list[float]], list[PaymentRecord]]:
    """Build the feature matrix, in payment-time order so time-aware splits mean something."""
    ordered = sorted(
        [p for p in payments if p.captured_at is not None and p.amount_minor > 0],
        key=lambda p: (p.captured_at, p.id),
    )
    counts: dict[str, int] = {}
    previous: dict[str, datetime] = {}
    rows: list[list[float]] = []

    for payment in ordered:
        key = payment.customer_id or payment.customer_name or payment.id
        counts[key] = counts.get(key, 0) + 1
        last = previous.get(key)
        gap_days = (payment.captured_at - last).total_seconds() / 86400 if last else -1.0
        previous[key] = payment.captured_at

        rows.append(
            [
                float(payment.amount_minor),
                float(payment.refunded_minor) / float(payment.amount_minor),
                float(payment.captured_at.hour),
                float(payment.captured_at.day),
                float(gap_days),
                float(counts[key]),
            ]
        )
    return rows, ordered


def model_version(rows: list[list[float]]) -> str:
    """Pin the exact model: algorithm, settings and the data it was fitted on.

    A finding that cites "IsolationForest" alone cannot be reproduced — refit on
    different evidence and the same record scores differently. Hashing the fitted
    matrix makes the version change whenever the inputs do, which is the property a
    model registry exists to provide.
    """
    digest = hashlib.sha256(
        repr([[round(v, 4) for v in row] for row in rows]).encode()
    ).hexdigest()[:12]
    return f"isolation-forest/c{CONTAMINATION}/s{RANDOM_STATE}/{digest}"


def score_payments(payments: list[PaymentRecord]) -> ScoringResult:
    rows, ordered = _features(payments)
    minimum = settings.ml_minimum_records

    if len(rows) < minimum:
        return ScoringResult(
            enabled=False,
            reason=(
                f"{len(rows)} scoreable payments is below the {minimum}-record "
                f"threshold — a model fitted on this little history produces an "
                f"unstable score, so none is reported"
            ),
            records_scored=len(rows),
        )

    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
        from sklearn.model_selection import TimeSeriesSplit
    except ImportError as exc:  # pragma: no cover - dependency is declared
        return ScoringResult(enabled=False, reason=f"scikit-learn unavailable: {exc}")

    matrix = np.asarray(rows, dtype=float)
    version = model_version(rows)

    # Time-aware validation before the scoring fit. Each fold trains only on
    # earlier payments and scores later ones, so the reported stability cannot be
    # inflated by having already seen the period it is judging.
    validation: dict[str, Any] = {"method": "TimeSeriesSplit", "folds": []}
    splits = min(5, max(2, len(rows) // 20))
    try:
        for index, (train_idx, test_idx) in enumerate(
            TimeSeriesSplit(n_splits=splits).split(matrix)
        ):
            fold = IsolationForest(
                contamination=CONTAMINATION, random_state=RANDOM_STATE, n_estimators=200
            ).fit(matrix[train_idx])
            flagged = int((fold.predict(matrix[test_idx]) == -1).sum())
            validation["folds"].append(
                {
                    "fold": index,
                    "train": len(train_idx),
                    "test": len(test_idx),
                    "flagged": flagged,
                    "flagged_rate": round(flagged / max(1, len(test_idx)), 4),
                }
            )
        rates = [f["flagged_rate"] for f in validation["folds"]]
        validation["mean_flagged_rate"] = round(sum(rates) / len(rates), 4) if rates else None
        # Drift signal: a flag rate that swings wildly between folds means the
        # workspace's behaviour changed, not that the model found more wrongdoing.
        validation["rate_spread"] = round(max(rates) - min(rates), 4) if rates else None
        validation["drift_suspected"] = bool(
            rates and (max(rates) - min(rates)) > 2 * CONTAMINATION
        )
    except ValueError as exc:
        validation["error"] = str(exc)[:200]

    forest = IsolationForest(
        contamination=CONTAMINATION, random_state=RANDOM_STATE, n_estimators=200
    ).fit(matrix)
    labels = forest.predict(matrix)
    scores = forest.score_samples(matrix)

    findings: list[Finding] = []
    for payment, label, score, row in zip(ordered, labels, scores, rows, strict=True):
        if label != -1:
            continue
        # Name the feature furthest from the median — the reviewer needs to know
        # *what* was unusual, not merely that something was.
        medians = [
            sorted(col)[len(col) // 2] for col in zip(*rows, strict=True)
        ]
        spreads = [
            (abs(row[i] - medians[i]), FEATURE_NAMES[i]) for i in range(len(FEATURE_NAMES))
        ]
        _, driver = max(spreads)

        findings.append(
            _finding(
                "A12_STATISTICAL_OUTLIER",
                AnomalySeverity.LOW,
                explanation=(
                    f"{_money(payment.amount_minor, payment.currency)} from "
                    f"{payment.customer_name or 'an unnamed customer'} ranked unlike "
                    f"this workspace's other payments, most notably on {driver}."
                ),
                observed_value=f"isolation score {score:.3f}",
                baseline_value=f"top {CONTAMINATION:.0%} of {len(rows)} payments",
                customer_id=payment.customer_id,
                related_records=[{"type": "payment", "id": payment.id}],
                caveats=[
                    ("A statistical score is a reason to look, never a finding. It "
                    "says a record is unusual for this company, not that it is wrong."),
                    f"Model {version} fitted on this workspace only.",
                ],
                model_version=version,
                model_score=float(score),
            )
        )

    return ScoringResult(
        enabled=True,
        reason=f"fitted on {len(rows)} payments",
        model_version=version,
        records_scored=len(rows),
        findings=findings,
        validation=validation,
    )
