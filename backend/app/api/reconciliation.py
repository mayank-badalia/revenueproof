"""Reconciliation endpoints — Feature 4."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import DbSession, Workspace_
from app.features.reconciliation import service
from app.models import Allocation
from app.schemas.workspace import MoneyOut

router = APIRouter(tags=["reconciliation"])


def _format(payload: dict, currency: str) -> dict:
    """Money crosses the boundary formatted, so the UI never does arithmetic."""
    payload["totals"] = {
        key.replace("total_", "").replace("_minor", ""): MoneyOut.build(
            payload[key], currency
        ).model_dump()
        for key in (
            "total_invoiced_minor", "total_allocated_minor", "total_outstanding_minor",
            "total_refunded_minor", "total_retained_minor",
            "total_bank_confirmed_minor",
        )
    }
    payload["unapplied_cash"] = MoneyOut.build(
        payload["unapplied_cash_minor"], currency
    ).model_dump()
    return payload


@router.post("/workspaces/{workspace_id}/reconcile")
async def run_reconciliation(ctx: Workspace_, session: DbSession):
    """Match invoices to payments to bank receipts. Fully deterministic."""
    ctx.require_resolver()
    result = await service.reconcile(session, workspace_id=ctx.workspace_id)
    await session.commit()
    return _format(result.as_dict(), ctx.workspace.base_currency)


@router.get("/workspaces/{workspace_id}/reconciliation")
async def read_reconciliation(ctx: Workspace_, session: DbSession):
    """The current reconciliation position, recomputed and stored nowhere.

    Feature 4 is derived state: the allocations persist but the per-invoice view does
    not, so a reopened page had nothing to render and told the reader to collect
    evidence over a workspace that was already reconciled. Recomputed through the
    same function the POST uses — a second read-path could disagree with the figures
    it is meant to be displaying, which is the one thing this product cannot do.

    Returns `reconciled: false` when nothing has been reconciled yet, so "not run" and
    "run, found nothing" stay distinguishable.
    """
    persisted = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Allocation)
                .where(Allocation.workspace_id == ctx.workspace_id)
            )
        ).scalar_one()
    )
    if not persisted:
        return {"reconciled": False}

    # Read before the rollback. Rolling back expires every ORM instance loaded in
    # this session, including the workspace on the request context, so touching it
    # afterwards triggers a lazy refresh outside the async greenlet and fails the
    # whole request — which reaches the browser as a CORS error, because the 500 is
    # rendered without the CORS headers a normal response carries.
    currency = ctx.workspace.base_currency

    result = await service.reconcile(
        session, workspace_id=ctx.workspace_id, persist=False
    )
    # Nothing above committed; discard the in-session work so a read cannot mutate.
    await session.rollback()
    payload = _format(result.as_dict(), currency)
    payload["reconciled"] = True
    return payload


@router.get("/workspaces/{workspace_id}/allocations")
async def list_allocations(ctx: Workspace_, session: DbSession):
    """Every invoice-payment link, with the rule and evidence behind it."""
    rows = (
        (
            await session.execute(
                select(Allocation)
                .where(Allocation.workspace_id == ctx.workspace_id)
                .order_by(Allocation.allocated_amount.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "allocations": [
            {
                "id": str(row.id),
                "invoice_id": str(row.invoice_id) if row.invoice_id else None,
                "payment_id": str(row.payment_id) if row.payment_id else None,
                "bank_transaction_id": (
                    str(row.bank_transaction_id) if row.bank_transaction_id else None
                ),
                "amount": MoneyOut.build(row.allocated_amount, row.currency),
                "method": row.method,
                "confidence": row.confidence,
                "rule_id": row.rule_id,
                "reasons": row.reasons,
                "reversed_at": row.reversed_at.isoformat() if row.reversed_at else None,
            }
            for row in rows
        ]
    }
