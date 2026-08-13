"""Revenue truth endpoints — Feature 5."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DbSession, Workspace_
from app.features.revenue import service
from app.features.revenue.policy import get_policy
from app.models import RevenueItem
from app.schemas.workspace import MoneyOut

router = APIRouter(tags=["revenue"])


class VerifyRequest(BaseModel):
    policy_version: str | None = None


@router.post("/workspaces/{workspace_id}/revenue/verify")
async def verify(payload: VerifyRequest, ctx: Workspace_, session: DbSession):
    """Classify every revenue item and produce the claimed-versus-verified totals."""
    ctx.require_resolver()
    result = await service.verify_revenue(
        session, workspace_id=ctx.workspace_id, policy_version=payload.policy_version
    )
    await session.commit()
    return _format_run(result, ctx.workspace.base_currency, payload.policy_version)


def _format_run(result, currency: str, policy_version: str | None) -> dict:
    """Money is formatted server-side so the UI never does arithmetic on a figure."""
    body = result.as_dict()
    body["money"] = {
        key: MoneyOut.build(value, currency).model_dump()
        for key, value in body["totals"].items()
        if isinstance(value, int)
    }
    body["waterfall"] = [
        {
            **step,
            "money": MoneyOut.build(abs(step["amount_minor"]), currency).model_dump(),
        }
        for step in body["waterfall"]
    ]
    body["policy"] = get_policy(policy_version).as_dict()
    return body


@router.get("/workspaces/{workspace_id}/revenue/summary")
async def revenue_summary(ctx: Workspace_, session: DbSession):
    """The claimed-versus-verified position, recomputed and stored nowhere.

    The classified items persist; the totals, waterfall and concentration built from
    them do not. So a reopened page listed 62 items with no statement of what they
    added up to against the claim — which is the only question the page exists to
    answer. Recomputed through the same function the button calls, for the same
    reason Feature 4's read is: a second implementation of the arithmetic could
    disagree with the figures it is displaying.
    """
    stored = int(
        (
            await session.execute(
                select(func.count())
                .select_from(RevenueItem)
                .where(RevenueItem.workspace_id == ctx.workspace_id)
            )
        ).scalar_one()
    )
    if not stored:
        return {"verified": False}

    # Read before the rollback: it expires every ORM instance in this session,
    # including the request context's workspace, and a lazy refresh afterwards fails
    # outside the async greenlet.
    currency = ctx.workspace.base_currency

    result = await service.verify_revenue(
        session, workspace_id=ctx.workspace_id, persist=False
    )
    await session.rollback()
    body = _format_run(result, currency, result.policy_version)
    body["verified"] = True
    return body


@router.get("/workspaces/{workspace_id}/revenue/items")
async def list_items(ctx: Workspace_, session: DbSession):
    """Every classified item with the rule, evidence and missing evidence behind it."""
    rows = (
        (
            await session.execute(
                select(RevenueItem)
                .where(RevenueItem.workspace_id == ctx.workspace_id)
                .order_by(RevenueItem.recognized_amount.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "description": row.description,
                "classification": str(row.classification),
                "is_recurring": row.is_recurring,
                "evidence_strength": str(row.evidence_strength),
                "gross": MoneyOut.build(row.gross_amount, row.currency),
                "recognized": MoneyOut.build(row.recognized_amount, row.currency),
                "rule_id": row.rule_id,
                "rule_explanation": row.rule_explanation,
                "evidence_ids": row.evidence_ids,
                "missing_evidence": row.missing_evidence,
                "calculation_detail": row.calculation_detail,
                "is_material": row.is_material,
                "is_published": row.is_published,
                "policy_version": row.policy_version,
            }
            for row in rows
        ]
    }
