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
