"""Human resolution of the review queue — the working end of Feature 7.

Features 2 through 6 all route their uncertainty to one place: a `ReviewItem` with
the evidence packet needed to settle it. Until now nothing could *work* that queue,
so items accumulated with no way to close them — the product's stated safe answer,
`HUMAN_REVIEW`, had no human attached.

Three rules shape this module, and all three come from idea_features.md §7:

* **A decision carries a reason, always.** An override with no reason is how a
  figure becomes unauditable — a later reader sees the number moved and cannot ask
  why. The reason is required by the signature, not by a validator that can be
  skipped.
* **Only a resolver may resolve.** External reviewers read and comment; they do not
  move material figures. That is enforced at the API boundary by `require_resolver`.
* **A resolution is remembered.** Confirming that two records are the same customer,
  or that a flag was noise, is knowledge this workspace should not have to
  rediscover on the next run — so it is written to correction memory, which is
  workspace-scoped and never pooled across tenants.

This is not the whole of Feature 7. The adversarial critic that argues against a
classification before a human ever sees it is still to come; what exists here is the
queue, the packet, and the decision that closes it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.features.identity.identifiers import normalize_name
from app.models import Anomaly, CorrectionMemory, ReviewItem
from app.models.enums import ReviewStatus
from app.services.audit import record_audit_event

#: What a reviewer may decide. Deliberately small: anything richer becomes a
#: free-text field nobody can aggregate, and the reason carries the nuance.
DECISIONS = ("approved", "rejected", "corrected")

#: Which feature a category belongs to, so the queue can say who is asking.
CATEGORY_SOURCE = {
    "ambiguous_match": "Feature 2 — identity resolution",
    "unreadable_contract": "Feature 3 — contract intelligence",
    "clause_conflict": "Feature 3 — contract intelligence",
    "partial_payment": "Feature 4 — reconciliation",
    "missing_bank_evidence": "Feature 4 — reconciliation",
    "agent_disagreement": "Feature 5 — revenue classification",
    "related_party": "Feature 6 — anomaly detection",
}


# ---------------------------------------------------------------------------
# Grouping — one item per decision, not one per record
#
# The queue is a list of decisions a human has to make, and that is not the same
# list as the records those decisions affect. Feature 2 asked "is Blue Harbor the
# same company as Blue Harbour?" seventeen times, once per pair of spellings it had
# seen; Feature 4 asked "is this receipt confirmed by the bank?" once per invoice.
# A reviewer answering seventeen identical questions is not doing seventeen times the
# work — they are doing it once and then clicking sixteen times, and a queue built
# that way trains people to click without reading.
#
# So equivalent items are collapsed into one decision carrying its members, and
# resolving it resolves all of them with the same reason. Nothing is hidden: the
# member count is shown, and every underlying record keeps its own row and its own
# audit entry.
# ---------------------------------------------------------------------------

#: Trailing record identifiers that vary *within* one question.
_RECORD_SUFFIX = re.compile(
    r"[:\-–—]?\s*(invoice|payment|receipt|item|order|inv|txn)?\s*"
    r"[A-Z]{0,4}[-_ ]?\d[\w./-]*\s*$",
    re.IGNORECASE,
)


def _question(item: ReviewItem) -> str:
    """What is actually being asked, with the specific record stripped out."""
    title = item.title or ""

    if item.category == "ambiguous_match" and "↔" in title:
        # "BLUE HARBOUR LOGISTICS LLP ↔ Blue Harbor" and "BLUE HARBOR ↔ Blue Harbour
        # Logistics" are the same pair of companies asked from opposite ends.
        left, _, right = title.partition("↔")
        # Strip the record marker from each side as well: "IRONBRIDGE MFG INSTALMENT
        # 1 ↔ IRONBRIDGE MFG H2" and the same with instalment 2 and 3 are one
        # question about one customer, asked once per payment that mentioned it.
        pair = sorted(
            normalize_name(_RECORD_SUFFIX.sub("", side.strip()))
            or normalize_name(side)
            or side.strip().lower()
            for side in (left, right)
        )
        return f"{item.category}|{pair[0]}|{pair[1]}"

    packet = item.evidence_packet or {}
    codes = packet.get("issue_codes")
    if codes:
        # Critic disputes group by what is wrong, not by which invoice it happened to.
        return f"{item.category}|{'+'.join(sorted(str(c) for c in codes))}"
    rule_id = packet.get("rule_id")
    if rule_id:
        return f"{item.category}|{rule_id}"

    return f"{item.category}|{_RECORD_SUFFIX.sub('', title).strip().lower()}"


@dataclass
class QueueSummary:
    open: int = 0
    in_progress: int = 0
    resolved: int = 0
    dismissed: int = 0
    by_category: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    oldest_open_days: int | None = None
    #: Distinct questions behind the open records. This is the number that describes
    #: a reviewer's actual workload; `open` is how many records those answers touch.
    open_decisions: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "open": self.open,
            "open_decisions": self.open_decisions,
            "in_progress": self.in_progress,
            "resolved": self.resolved,
            "dismissed": self.dismissed,
            "by_category": self.by_category,
            "by_severity": self.by_severity,
            "oldest_open_days": self.oldest_open_days,
            "total": self.open + self.in_progress + self.resolved + self.dismissed,
        }


async def summarise(session: AsyncSession, *, workspace_id: uuid.UUID) -> QueueSummary:
    """Counts a reviewer needs before opening anything: how much, how old, how bad."""
    rows = list(
        (
            await session.execute(
                select(ReviewItem).where(ReviewItem.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    summary = QueueSummary()
    now = datetime.now(UTC)
    oldest: datetime | None = None

    for row in rows:
        status = str(row.status)
        if status == ReviewStatus.OPEN:
            summary.open += 1
            created = row.created_at
            if created is not None:
                created = created if created.tzinfo else created.replace(tzinfo=UTC)
                oldest = created if oldest is None or created < oldest else oldest
        elif status == ReviewStatus.IN_PROGRESS:
            summary.in_progress += 1
        elif status == ReviewStatus.RESOLVED:
            summary.resolved += 1
        elif status == ReviewStatus.DISMISSED:
            summary.dismissed += 1

        if status in (ReviewStatus.OPEN, ReviewStatus.IN_PROGRESS):
            summary.by_category[row.category] = summary.by_category.get(row.category, 0) + 1
            key = str(row.severity)
            summary.by_severity[key] = summary.by_severity.get(key, 0) + 1

    if oldest is not None:
        summary.oldest_open_days = max(0, (now - oldest).days)
    summary.open_decisions = len(
        {
            _question(row)
            for row in rows
            if str(row.status) in (ReviewStatus.OPEN, ReviewStatus.IN_PROGRESS)
        }
    )
    return summary


async def list_items(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    status: str | None = "open",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """The queue, worst and oldest first.

    Ordering is severity then age, not insertion order: a reviewer with an hour
    should spend it on the most material unresolved thing, and the item that has
    been waiting longest is the one most likely to be blocking a report.
    """
    query = select(ReviewItem).where(ReviewItem.workspace_id == workspace_id)
    if status and status != "all":
        query = query.where(ReviewItem.status == status)

    rows = list((await session.execute(query)).scalars().all())
    rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    rows.sort(
        key=lambda r: (
            rank.get(str(r.severity), 9),
            r.created_at or datetime.now(UTC),
            str(r.id),
        )
    )

    # Collapse equivalent questions. The first member — already the worst and
    # oldest — represents the group, and the rest ride along as `members`.
    grouped: dict[str, list[ReviewItem]] = {}
    for row in rows:
        grouped.setdefault(_question(row), []).append(row)

    out: list[dict[str, Any]] = []
    for members in grouped.values():
        row = members[0]
        others = members[1:]
        out.append(
            {
                "id": str(row.id),
                "category": row.category,
                "raised_by": CATEGORY_SOURCE.get(row.category, "unknown"),
                "title": row.title,
                "detail": row.detail,
                "severity": str(row.severity),
                "status": str(row.status),
                "evidence_packet": row.evidence_packet,
                "anomaly_id": str(row.anomaly_id) if row.anomaly_id else None,
                "revenue_item_id": (
                    str(row.revenue_item_id) if row.revenue_item_id else None
                ),
                "contract_id": str(row.contract_id) if row.contract_id else None,
                "resolution": row.resolution,
                "resolution_reason": row.resolution_reason,
                "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                # One decision, and every record it covers. Nothing is hidden: the
                # count is shown and each record keeps its own row and audit entry.
                "member_ids": [str(m.id) for m in members],
                "member_count": len(members),
                "also_affects": [m.title for m in others[:8]],
            }
        )
    return out[:limit]


async def resolve(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    item_id: uuid.UUID,
    decision: str,
    reason: str,
    user_id: uuid.UUID,
    remember: bool = True,
) -> ReviewItem | None:
    """Close one review item with a decision and a reason.

    The reason is not optional and is not allowed to be empty. §7 requires a human
    override to state why, and the audit chain stores the before and after state so
    the decision itself is as reviewable as the thing it decided.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a resolution must carry a reason")

    row = await session.get(ReviewItem, item_id)
    if row is None or row.workspace_id != workspace_id:
        # 404 rather than 403 upstream: a reviewer must not learn that an item
        # exists in a workspace they cannot see.
        return None

    before = {
        "status": str(row.status),
        "resolution": row.resolution,
        "resolution_reason": row.resolution_reason,
    }

    row.status = (
        ReviewStatus.DISMISSED if decision == "rejected" else ReviewStatus.RESOLVED
    )
    row.resolution = decision
    row.resolution_reason = reason[:4000]
    row.resolved_by_user_id = user_id
    row.resolved_at = datetime.now(UTC)

    # An anomaly resolved here is also a precision label: a reviewer who rejects a
    # flag is saying it was not worth their time, which is exactly what Feature 6
    # measures its detectors on. Recording it in one place keeps the two screens
    # from disagreeing about the same finding.
    if row.anomaly_id:
        anomaly = await session.get(Anomaly, row.anomaly_id)
        if anomaly is not None and anomaly.workspace_id == workspace_id:
            anomaly.is_false_positive = decision == "rejected"
            anomaly.status = row.status

    if remember:
        session.add(
            CorrectionMemory(
                workspace_id=workspace_id,
                correction_type=_memory_type(row.category),
                subject=row.title[:300],
                corrected_value={"decision": decision, "category": row.category},
                reason=reason[:2000],
                created_by_user_id=user_id,
            )
        )

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="human",
        actor_id=str(user_id),
        action="review.resolved",
        object_type="review_item",
        object_id=str(item_id),
        before_state=before,
        after_state={
            "status": str(row.status),
            "resolution": decision,
            "resolution_reason": reason[:500],
        },
        reason=reason[:500],
    )
    await session.flush()

    emit(
        EventKind.RULE,
        f"Review resolved: {row.title[:80]} → {decision}",
        workspace_id=str(workspace_id),
        feature=7,
        severity=Severity.SUCCESS,
    )
    return row


def _memory_type(category: str) -> str:
    """Which kind of learning this decision represents."""
    return {
        "ambiguous_match": "match_rule",
        "related_party": "related_party",
        "agent_disagreement": "classification_override",
    }.get(category, "classification_override")


async def resolve_group(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    decision: str,
    reason: str,
    user_id: uuid.UUID,
) -> int:
    """Apply one decision to every record it covers.

    The reviewer answered a question once; the system applies that answer to each
    record the question was about. Every record still gets its own audit entry —
    the shortcut is in the asking, never in the recording.
    """
    resolved = 0
    for item_id in item_ids:
        row = await resolve(
            session,
            workspace_id=workspace_id,
            item_id=item_id,
            decision=decision,
            reason=reason,
            user_id=user_id,
            # Correction memory is written once for the group, not once per record:
            # sixteen identical memories would drown the retrieval that reads them.
            remember=resolved == 0,
        )
        if row is not None:
            resolved += 1
    return resolved


async def claim(
    session: AsyncSession, *, workspace_id: uuid.UUID, item_id: uuid.UUID
) -> ReviewItem | None:
    """Mark an item as being worked, so two reviewers do not duplicate the effort."""
    row = await session.get(ReviewItem, item_id)
    if row is None or row.workspace_id != workspace_id:
        return None
    if str(row.status) == ReviewStatus.OPEN:
        row.status = ReviewStatus.IN_PROGRESS
        await session.flush()
    return row


async def open_count(session: AsyncSession, *, workspace_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(ReviewItem)
                .where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.status.in_([ReviewStatus.OPEN, ReviewStatus.IN_PROGRESS]),
                )
            )
        ).scalar_one()
    )
