"""Identity resolution endpoints — Feature 2."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DbSession, Workspace_
from app.core.events import EventKind, Severity, emit
from app.features.identity import graph as identity_graph
from app.features.identity import service
from app.models import CustomerEntity, EntityMatchProposal, ReviewItem
from app.models.enums import MatchDecision, ReviewStatus

router = APIRouter(tags=["identity"])


class ResolveRequest(BaseModel):
    use_critic: bool = True


class MatchDecisionRequest(BaseModel):
    decision: Literal["ACCEPTED", "REJECTED"]
    reason: str = Field(min_length=3, max_length=1000)


@router.post("/workspaces/{workspace_id}/identity/resolve")
async def resolve(payload: ResolveRequest, ctx: Workspace_, session: DbSession):
    """Run entity resolution across every source (Feature 2 end to end)."""
    ctx.require_resolver()
    result = await service.resolve_identities(
        session, workspace_id=ctx.workspace_id, use_critic=payload.use_critic
    )
    await session.commit()
    return result.as_dict()


@router.get("/workspaces/{workspace_id}/identity/customers")
async def list_customers(ctx: Workspace_, session: DbSession):
    """Resolved canonical customers with their aliases and identifiers."""
    rows = (
        (
            await session.execute(
                select(CustomerEntity)
                .where(CustomerEntity.workspace_id == ctx.workspace_id)
                .order_by(CustomerEntity.canonical_name)
            )
        )
        .scalars()
        .all()
    )
    return {
        "customers": [
            {
                "id": str(row.id),
                "canonical_name": row.canonical_name,
                "normalized_name": row.normalized_name,
                "known_aliases": row.known_aliases,
                "domains": row.domains,
                "tax_identifiers": row.tax_identifiers,
                "email_addresses": row.email_addresses,
                "match_confidence": row.match_confidence,
                "human_confirmed": row.human_confirmed,
                "related_party_status": row.related_party_status,
            }
            for row in rows
        ]
    }


@router.get("/workspaces/{workspace_id}/identity/matches")
async def list_matches(
    ctx: Workspace_,
    session: DbSession,
    decision: str | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    """Scored identity links, including rejections.

    Rejections are returned deliberately: "why were these two *not* merged?" is a
    question reviewers ask constantly, and only a stored negative can answer it.
    """
    query = (
        select(EntityMatchProposal)
        .where(EntityMatchProposal.workspace_id == ctx.workspace_id)
        .order_by(EntityMatchProposal.score.desc())
        .limit(limit)
    )
    if decision:
        query = query.where(EntityMatchProposal.decision == decision.upper())

    rows = (await session.execute(query)).scalars().all()
    counts = (
        await session.execute(
            select(EntityMatchProposal.decision, func.count())
            .where(EntityMatchProposal.workspace_id == ctx.workspace_id)
            .group_by(EntityMatchProposal.decision)
        )
    ).all()

    return {
        "counts": {str(decision): count for decision, count in counts},
        "matches": [
            {
                "id": str(row.id),
                "left": {"id": row.left_id, "label": row.left_label, "type": row.left_type},
                "right": {"id": row.right_id, "label": row.right_label, "type": row.right_type},
                "method": row.method,
                "score": row.score,
                "decision": str(row.decision),
                "signals": row.signals,
                "critic_note": row.critic_note,
            }
            for row in rows
        ],
    }


@router.post("/workspaces/{workspace_id}/identity/matches/{match_id}/decide")
async def decide_match(
    match_id: uuid.UUID,
    payload: MatchDecisionRequest,
    ctx: Workspace_,
    session: DbSession,
):
    """Record a human decision on a link and remember it for future runs."""
    ctx.require_resolver()

    proposal = await session.get(EntityMatchProposal, match_id)
    if proposal is None or proposal.workspace_id != ctx.workspace_id:
        raise HTTPException(status_code=404, detail="match proposal not found")

    proposal.decision = MatchDecision(payload.decision)
    proposal.decided_by = "human"
    proposal.critic_note = f"Human decision: {payload.reason}"

    # Workspace-scoped memory, so the same pair is never re-asked here — and never
    # applied to any other tenant.
    await service.remember_decision(
        session,
        workspace_id=ctx.workspace_id,
        left_id=proposal.left_id,
        right_id=proposal.right_id,
        decision=payload.decision,
        reason=payload.reason,
        user_id=ctx.user.id,
    )

    # Resolve any queued review item for this pair.
    review = (
        await session.execute(
            select(ReviewItem).where(
                ReviewItem.workspace_id == ctx.workspace_id,
                ReviewItem.category == "ambiguous_match",
                ReviewItem.title == f"{proposal.left_label} ↔ {proposal.right_label}",
                ReviewItem.status == ReviewStatus.OPEN,
            )
        )
    ).scalar_one_or_none()
    if review is not None:
        review.status = ReviewStatus.RESOLVED
        review.resolution = payload.decision
        review.resolution_reason = payload.reason
        review.resolved_by_user_id = ctx.user.id
        review.resolved_at = func.now()

    await session.commit()
    emit(
        EventKind.RESULT,
        f"Human resolved identity link: {proposal.left_label} ↔ "
        f"{proposal.right_label} → {payload.decision}",
        workspace_id=str(ctx.workspace_id),
        severity=Severity.SUCCESS,
        feature=2,
        reason=payload.reason,
    )
    return {"id": str(match_id), "decision": payload.decision, "remembered": True}


@router.get("/workspaces/{workspace_id}/identity/relationships")
async def relationships(ctx: Workspace_, max_depth: int = Query(3, ge=1, le=4)):
    """Entities connected by shared domain, address or payment account.

    Reported for investigation only. A shared address may mean a parent and
    subsidiary, two tenants of one business centre, or a related party — the graph
    records what was observed and never asserts a legal relationship.
    """
    try:
        paths = await identity_graph.find_related_entities(
            str(ctx.workspace_id), max_depth=max_depth
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"evidence graph unavailable: {exc}"
        ) from exc
    return {
        "relationships": paths,
        "caveat": (
            "These are observed shared attributes, not proof of a legal relationship "
            "or of improper conduct. They indicate records worth investigating."
        ),
    }


@router.get("/workspaces/{workspace_id}/identity/graph/{node_id}")
async def graph_neighbourhood(
    node_id: str, ctx: Workspace_, max_depth: int = Query(2, ge=1, le=3)
):
    """Bounded subgraph around one node, for the evidence-graph view."""
    try:
        return await identity_graph.neighbourhood(
            str(ctx.workspace_id), node_id, max_depth=max_depth
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"evidence graph unavailable: {exc}"
        ) from exc
