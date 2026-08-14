"""Report versions and deterministic change explanation — Feature 8, sub-feature 9.

A diligence report is not a document, it is a position that moves as evidence
arrives. The question a reviewer asks on the second visit is never "what does it
say" — it is **"what changed, and why."**

So a version is a snapshot of every headline figure at a moment, and the diff
between two versions is computed **in code**. core_resoruces.md is explicit that an
agent may *explain* a change and must not invent one: the numbers, the direction and
the magnitude are all arithmetic, and the explanation is assembled from the records
that moved. Nothing here calls a model.

Two things a version is careful about:

* **It counts only published figures.** A version built from proposals would move
  every time the pipeline re-ran, and the movement would mean nothing.
* **A version is immutable once written.** Re-running the pipeline creates version
  n+1; it never edits n. That is what makes "the report said X last Tuesday" a
  checkable statement rather than a memory.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.core.money import from_minor_units
from app.features.revenue.position import published_verified, published_verified_total
from app.models import Anomaly, ReportVersion, RevenueItem, Workspace
from app.models.enums import RevenueClass, ReviewStatus
from app.services.audit import record_audit_event

#: Figures compared between versions, in the order a reader wants them.
TRACKED = (
    ("claimed_revenue", "Claimed revenue"),
    ("verified_recurring", "Verified recurring"),
    ("verified_one_time", "Verified one-time"),
    ("supported_arr", "Supported ARR"),
    ("cash_received", "Cash received"),
    ("contracted_unpaid", "Contracted, unbilled"),
    ("invoiced_unpaid", "Invoiced, unpaid"),
    ("refunded_reversed", "Refunded or reversed"),
    ("unsupported", "Unsupported"),
)


@dataclass
class VersionResult:
    version: int = 0
    created: bool = False
    changes: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "created": self.created,
            "changes": self.changes,
            "explanation": self.explanation,
        }


def _money(minor: int, currency: str) -> str:
    return f"{currency} {from_minor_units(minor, currency):,.2f}"


async def _snapshot(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Every headline figure, from published items only."""
    workspace = await session.get(Workspace, workspace_id)
    items = list(
        (
            await session.execute(
                select(RevenueItem).where(RevenueItem.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    published = [i for i in items if i.is_published]

    def total(*classes: RevenueClass, published_only: bool = True) -> int:
        pool = published if published_only else items
        return sum(
            i.recognized_amount if RevenueClass(i.classification).counts_as_verified
            else i.gross_amount
            for i in pool
            if RevenueClass(i.classification) in classes
        )

    per_customer: dict[str, int] = {}
    for item in published_verified(items):
        key = str(item.customer_entity_id or "unattributed")
        per_customer[key] = per_customer.get(key, 0) + item.recognized_amount
    verified_total = sum(per_customer.values())
    top_pct = (
        round(max(per_customer.values()) / verified_total * 100, 2)
        if verified_total
        else None
    )
    # The count travels with the share. Feature 6 measures concentration over every
    # evidence-supported item; this measures it over the published ones only, so the
    # two legitimately differ — and a page showing 53.8% next to 28.8% with no basis
    # stated reads as one of them being wrong. Whichever number a reviewer is looking
    # at, it now says what it was divided by.
    concentration_customers = len(per_customer)
    hhi = (
        round(sum((v / verified_total * 100) ** 2 for v in per_customer.values()), 1)
        if verified_total
        else None
    )

    # Decisions, not records. The queue collapses equivalent questions, and the room
    # must quote the same number the queue does or the two screens disagree about
    # how much work is outstanding.
    from app.features.review.service import summarise as _summarise

    queue = await _summarise(session, workspace_id=workspace_id)
    open_reviews = queue.open_decisions
    open_review_records = queue.open + queue.in_progress
    open_anomalies = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Anomaly)
                .where(
                    Anomaly.workspace_id == workspace_id,
                    Anomaly.status == ReviewStatus.OPEN,
                )
            )
        ).scalar_one()
    )

    missing: dict[str, int] = {}
    for item in items:
        for gap in item.missing_evidence or []:
            missing[gap] = missing.get(gap, 0) + 1

    return {
        "currency": workspace.base_currency if workspace else "INR",
        "claimed_revenue": workspace.claimed_revenue if workspace else 0,
        "claimed_arr": workspace.claimed_arr if workspace else 0,
        "verified_recurring": total(RevenueClass.VERIFIED_RECURRING),
        "verified_one_time": total(RevenueClass.VERIFIED_ONE_TIME),
        "cash_received": published_verified_total(items),
        "contracted_unpaid": total(RevenueClass.CONTRACTED_UNPAID, published_only=False),
        "invoiced_unpaid": total(RevenueClass.INVOICED_UNPAID, published_only=False),
        "refunded_reversed": total(
            RevenueClass.REFUNDED_OR_REVERSED, published_only=False
        ),
        "unsupported": total(
            RevenueClass.UNSUPPORTED_CLAIM,
            RevenueClass.PAYMENT_WITHOUT_SUPPORT,
            published_only=False,
        ),
        "supported_arr": sum(
            i.recognized_amount
            for i in published
            if RevenueClass(i.classification) is RevenueClass.VERIFIED_RECURRING
        ),
        "items_awaiting_review": open_reviews,
        "review_records": open_review_records,
        "open_anomalies": open_anomalies,
        "items_published": len(published),
        "items_total": len(items),
        "largest_customer_concentration_pct": top_pct,
        "hhi": hhi,
        "concentration_customers": concentration_customers,
        "concentration_basis": "published revenue",
        "missing_evidence": [
            {"gap": gap, "items": count}
            for gap, count in sorted(missing.items(), key=lambda kv: -kv[1])
        ],
        "policy_version": published[0].policy_version if published else "v1",
    }


def diff(previous: ReportVersion | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    """What moved between two versions, in code.

    Direction and magnitude are arithmetic. An agent may later describe *why* a
    figure moved; it may never be the thing that decides whether it moved.
    """
    if previous is None:
        return []
    changes: list[dict[str, Any]] = []
    currency = current.get("currency", "INR")
    for key, label in TRACKED:
        before = getattr(previous, key, 0) or 0
        after = current.get(key, 0) or 0
        if before == after:
            continue
        changes.append(
            {
                "field": key,
                "label": label,
                "before_minor": before,
                "after_minor": after,
                "delta_minor": after - before,
                "before": _money(before, currency),
                "after": _money(after, currency),
                "direction": "increased" if after > before else "decreased",
            }
        )

    for key, label in (
        ("items_awaiting_review", "Items awaiting review"),
        ("items_published", "Items published"),
    ):
        before = getattr(previous, key, None)
        after = current.get(key)
        if before is None or after is None or before == after:
            continue
        changes.append(
            {
                "field": key,
                "label": label,
                "before_minor": before,
                "after_minor": after,
                "delta_minor": after - before,
                "before": str(before),
                "after": str(after),
                "direction": "increased" if after > before else "decreased",
            }
        )
    return changes


def explain(changes: list[dict[str, Any]]) -> str:
    """Describe the movement in plain words, assembled rather than generated."""
    if not changes:
        return "No headline figure moved since the previous version."
    parts = []
    for change in changes[:6]:
        parts.append(
            f"{change['label']} {change['direction']} from {change['before']} to "
            f"{change['after']}"
        )
    tail = "" if len(changes) <= 6 else f", and {len(changes) - 6} further changes"
    return "; ".join(parts) + tail + "."


async def publish_version(
    session: AsyncSession, *, workspace_id: uuid.UUID, force: bool = False
) -> VersionResult:
    """Snapshot the current position as an immutable version, with its diff."""
    current = await _snapshot(session, workspace_id=workspace_id)
    previous = (
        await session.execute(
            select(ReportVersion)
            .where(ReportVersion.workspace_id == workspace_id)
            .order_by(ReportVersion.version.desc())
            .limit(1)
        )
    ).scalars().first()

    changes = diff(previous, current)
    result = VersionResult(version=(previous.version + 1) if previous else 1)
    first = previous is None

    if previous is not None and not changes and not force:
        # A version identical to the last one is noise in a history whose whole job
        # is to show movement.
        result.version = previous.version
        result.explanation = "Nothing moved since the previous version; none created."
        return result

    result.changes = changes
    # The first version has nothing to differ from; saying "nothing moved" there
    # implies a comparison that was never made.
    result.explanation = (
        "First published version — the baseline every later change is measured against."
        if first
        else explain(changes)
    )
    result.created = True

    session.add(
        ReportVersion(
            workspace_id=workspace_id,
            version=result.version,
            currency=current["currency"],
            claimed_revenue=current["claimed_revenue"],
            claimed_arr=current["claimed_arr"],
            cash_received=current["cash_received"],
            verified_recurring=current["verified_recurring"],
            verified_one_time=current["verified_one_time"],
            contracted_unpaid=current["contracted_unpaid"],
            invoiced_unpaid=current["invoiced_unpaid"],
            refunded_reversed=current["refunded_reversed"],
            unsupported=current["unsupported"],
            supported_arr=current["supported_arr"],
            items_awaiting_review=current["items_awaiting_review"],
            largest_customer_concentration_pct=current[
                "largest_customer_concentration_pct"
            ],
            hhi=current["hhi"],
            missing_evidence=current["missing_evidence"],
            changes_from_previous=changes,
            change_explanation=result.explanation,
            policy_version=current["policy_version"],
            published_at=datetime.now(UTC),
        )
    )
    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id="report_versioning",
        action="report.version_published",
        object_type="workspace",
        object_id=str(workspace_id),
        after_state={"version": result.version, "changes": len(changes)},
        reason=result.explanation[:500],
        policy_version=current["policy_version"],
    )
    await session.flush()

    emit(
        EventKind.RESULT,
        f"Report version {result.version} published: {result.explanation}",
        workspace_id=str(workspace_id),
        feature=8,
        severity=Severity.SUCCESS,
    )
    return result


async def list_versions(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Every version, newest first. Earlier ones stay readable forever."""
    rows = (
        await session.execute(
            select(ReportVersion)
            .where(ReportVersion.workspace_id == workspace_id)
            .order_by(ReportVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "version": row.version,
            "published_at": row.published_at.isoformat() if row.published_at else None,
            "currency": row.currency,
            "claimed_revenue": _money(row.claimed_revenue, row.currency),
            "verified_recurring": _money(row.verified_recurring, row.currency),
            "verified_one_time": _money(row.verified_one_time, row.currency),
            "supported_arr": _money(row.supported_arr, row.currency),
            "refunded_reversed": _money(row.refunded_reversed, row.currency),
            "unsupported": _money(row.unsupported, row.currency),
            "items_awaiting_review": row.items_awaiting_review,
            "largest_customer_concentration_pct": row.largest_customer_concentration_pct,
            "hhi": row.hhi,
            "changes_from_previous": row.changes_from_previous,
            "change_explanation": row.change_explanation,
            "policy_version": row.policy_version,
        }
        for row in rows
    ]


async def current_position(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """The live figures, whether or not they have been versioned yet."""
    return await _snapshot(session, workspace_id=workspace_id)
