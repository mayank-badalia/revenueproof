"""Diligence room endpoints — Feature 8.

The room is where an outside reviewer actually reads the result: the current
position, the version history that shows how it moved, and the evidence chain behind
any single figure.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession, Workspace_
from app.features.room import evidence, monitor, versions
from app.models import CriticDecision, RevenueItem
from app.models.enums import RevenueClass

router = APIRouter(tags=["room"])


class RerunRequest(BaseModel):
    #: Look this far back for changed source records.
    days: int = 7
    #: Rerun even when nothing changed — useful after a policy change.
    force: bool = False
    use_llm: bool = True


@router.get("/workspaces/{workspace_id}/room/changes")
async def detect_changes(
    ctx: Workspace_,
    session: DbSession,
    days: int = Query(7, ge=1, le=365),
):
    """What has moved since the figures were built, and what that invalidates."""
    from datetime import UTC, datetime, timedelta

    impact = await monitor.detect_changes(
        session,
        workspace_id=ctx.workspace_id,
        since=datetime.now(UTC) - timedelta(days=days),
    )
    return {
        **impact.as_dict(),
        "monitoring": await monitor.monitoring_status(
            session, workspace_id=ctx.workspace_id
        ),
    }


@router.post("/workspaces/{workspace_id}/room/rerun")
async def rerun(payload: RerunRequest, ctx: Workspace_, session: DbSession):
    """Redo only the work the changes invalidated, then publish a version."""
    ctx.require_resolver()
    from datetime import UTC, datetime, timedelta

    result = await monitor.rerun_affected(
        session,
        workspace_id=ctx.workspace_id,
        since=datetime.now(UTC) - timedelta(days=payload.days),
        force=payload.force,
        use_llm=payload.use_llm,
    )
    await session.commit()
    return result.as_dict()


class PublishRequest(BaseModel):
    #: Write a version even when nothing moved. Off by default: a history whose job
    #: is to show movement should not fill up with identical entries.
    force: bool = False


@router.get("/workspaces/{workspace_id}/room")
async def diligence_room(ctx: Workspace_, session: DbSession):
    """The current position, the history, and what is still unproven."""
    position = await versions.current_position(session, workspace_id=ctx.workspace_id)
    history = await versions.list_versions(session, workspace_id=ctx.workspace_id)

    rows = (
        await session.execute(
            select(RevenueItem)
            .where(RevenueItem.workspace_id == ctx.workspace_id)
            .order_by(RevenueItem.recognized_amount.desc())
        )
    ).scalars().all()
    verdicts = {
        str(d.revenue_item_id): d
        for d in (
            await session.execute(
                select(CriticDecision).where(
                    CriticDecision.workspace_id == ctx.workspace_id
                )
            )
        ).scalars().all()
    }

    currency = position["currency"]
    items = [
        {
            "id": str(row.id),
            "description": row.description,
            "classification": str(row.classification),
            "counts_as_verified": RevenueClass(row.classification).counts_as_verified,
            "recognized": evidence._money(row.recognized_amount, currency),
            "gross": evidence._money(row.gross_amount, currency),
            "is_published": row.is_published,
            "rule_id": row.rule_id,
            "missing_evidence": row.missing_evidence,
            # Why an amount is *not* in the headline is the question a reviewer asks
            # most, so the answer travels with the row rather than being looked up.
            "withheld_because": (
                None
                if row.is_published
                else (
                    verdicts[str(row.id)].reasoning[:300]
                    if str(row.id) in verdicts
                    else "not yet reviewed by the critic"
                )
            ),
            "verdict": (
                str(verdicts[str(row.id)].verdict) if str(row.id) in verdicts else None
            ),
        }
        for row in rows
    ]

    return {
        "position": position,
        "history": history,
        "items": items,
        "why_the_gap": _explain_gap(items, position),
        "published_count": sum(1 for i in items if i["is_published"]),
        "withheld_count": sum(1 for i in items if not i["is_published"]),
        "caveat": (
            "Only figures that survived the critic are published. Everything else is "
            "listed with the reason it is not — a gap you can see is worth more than "
            "a total you cannot check. This is not investment advice and does not "
            "certify revenue."
        ),
    }


@router.post("/workspaces/{workspace_id}/room/publish")
async def publish(payload: PublishRequest, ctx: Workspace_, session: DbSession):
    """Freeze the current position as an immutable version, with its diff."""
    ctx.require_resolver()
    result = await versions.publish_version(
        session, workspace_id=ctx.workspace_id, force=payload.force
    )
    await session.commit()
    return result.as_dict()


@router.get("/workspaces/{workspace_id}/room/trace/{item_id}")
async def trace(item_id: uuid.UUID, ctx: Workspace_, session: DbSession):
    """The full chain behind one amount: customer → contract → invoice → payment → bank → refund."""
    result = await evidence.trace_item(
        session, workspace_id=ctx.workspace_id, item_id=item_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="revenue item not found")
    return result.as_dict()


@router.get("/workspaces/{workspace_id}/room/versions")
async def version_history(
    ctx: Workspace_,
    session: DbSession,
    limit: int = Query(20, ge=1, le=100),
):
    """Every published version, newest first."""
    history = await versions.list_versions(session, workspace_id=ctx.workspace_id)
    return {"versions": history[:limit], "total": len(history)}


#: What each classification means for the *reader*, when it is the reason a large
#: share of the claim is not in the headline figure. Written as the sentence a
#: reviewer would say out loud, not the enum name.
_GAP_CAUSE: dict[str, str] = {
    "INVOICED_UNPAID": (
        "invoiced, with no payment against them yet. An invoice is a claim on cash, "
        "not proof of it"
    ),
    "CONTRACTED_UNPAID": (
        "under contract but never invoiced. Contracted value is not cash received"
    ),
    "REFUNDED_OR_REVERSED": "paid and then returned, so the cash did not stay",
    "PAYMENT_WITHOUT_SUPPORT": (
        "money that arrived with no invoice or contract explaining it"
    ),
    "HUMAN_REVIEW": (
        "contradictory enough that the pipeline refused to decide on its own"
    ),
    "UNSUPPORTED_CLAIM": "claimed with no evidence found at all",
}

#: The deterministic checks that most often withhold an otherwise verified figure,
#: and what a person can actually do about each.
_WITHHELD_REMEDY: tuple[tuple[str, str, str], ...] = (
    ("no independent bank credit",
     "Verified against the processor, but no bank line corroborates it",
     "Upload the bank statement covering these dates, or confirm in review that the "
     "processor record is sufficient"),
    ("anomaly",
     "An unresolved high-severity anomaly indicator touches these",
     "Answer the indicator in the anomaly panel — confirming or dismissing it "
     "releases every figure it blocks"),
    ("unresolved customer",
     "The customer behind these could not be resolved with confidence",
     "Settle the identity question in the review queue"),
    ("citation",
     "A contract amount could not be traced back to its page",
     "Re-read the contract, or confirm the amount in review"),
)


def _explain_gap(items: list[dict], position: dict) -> dict:
    """Why the published figure is below the claim, in causes a person can act on.

    "Proven ₹0.00 against a claim of ₹1.5 crore" is a true statement that tells a
    reader nothing about whether the product failed, the evidence is missing, or the
    claim was wrong. Each is a different next action, and the difference is knowable
    from the data — so it is stated rather than left to be inferred from a table of
    fifty rows.
    """
    claimed = position.get("claimed_revenue") or 0
    proven = position.get("verified_recurring", 0) + position.get("verified_one_time", 0)
    if claimed <= 0 or proven >= claimed:
        return {"material": False, "causes": [], "actions": []}

    # Where the unproven value sits, by classification, largest first.
    by_class: dict[str, dict] = {}
    for item in items:
        if item["counts_as_verified"] and item["is_published"]:
            continue
        cls = item["classification"]
        entry = by_class.setdefault(cls, {"count": 0, "minor": 0})
        entry["count"] += 1
        entry["minor"] += item["gross"]["minor"]

    causes = [
        {
            "classification": cls,
            "count": data["count"],
            "amount": data["minor"],
            "why": _GAP_CAUSE.get(cls, "not counted as verified revenue"),
        }
        for cls, data in sorted(by_class.items(), key=lambda kv: -kv[1]["minor"])
        if cls in _GAP_CAUSE
    ][:4]

    # Verified figures held back by a check a person can clear.
    withheld = [i for i in items if i["counts_as_verified"] and not i["is_published"]]
    actions = []
    for needle, summary, remedy in _WITHHELD_REMEDY:
        matched = [
            i for i in withheld
            if needle in (i.get("withheld_because") or "").lower()
        ]
        if matched:
            actions.append({
                "summary": summary,
                "remedy": remedy,
                "count": len(matched),
                "amount": sum(i["recognized"]["minor"] for i in matched),
            })
    actions.sort(key=lambda a: -a["amount"])

    return {
        "material": True,
        "shortfall": claimed - proven,
        "causes": causes,
        "actions": actions,
        # The claim is an input, not a finding. When almost none of it is evidenced,
        # the most likely explanations include the claim itself being wrong — and a
        # tool that never says so is implying the evidence must be at fault.
        "claim_may_be_wrong": proven < claimed // 2,
    }
