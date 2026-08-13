"""Contract intelligence endpoints — Feature 3."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession, Workspace_
from app.features.contracts import service
from app.models import Citation, Contract
from app.schemas.workspace import MoneyOut

router = APIRouter(tags=["contracts"])


class ProcessRequest(BaseModel):
    limit: int | None = None


@router.post("/workspaces/{workspace_id}/contracts/process")
async def process(payload: ProcessRequest, ctx: Workspace_, session: DbSession):
    """Read every vaulted contract and extract its commercial terms."""
    ctx.require_resolver()
    result = await service.process_contracts(
        session, workspace_id=ctx.workspace_id, limit=payload.limit
    )
    await session.commit()
    return result.as_dict()


@router.get("/workspaces/{workspace_id}/contracts")
async def list_contracts(ctx: Workspace_, session: DbSession):
    """Extracted contracts with their amounts and citation coverage."""
    rows = (
        (
            await session.execute(
                select(Contract)
                .where(Contract.workspace_id == ctx.workspace_id)
                .order_by(Contract.document_name)
            )
        )
        .scalars()
        .all()
    )
    return {
        "contracts": [
            {
                "id": str(row.id),
                "document_name": row.document_name,
                "stated_customer_name": row.stated_customer_name,
                "start_date": row.start_date.isoformat() if row.start_date else None,
                "end_date": row.end_date.isoformat() if row.end_date else None,
                "billing_frequency": row.billing_frequency,
                "recurring_amount": MoneyOut.build(row.recurring_amount, row.currency),
                "one_time_amount": MoneyOut.build(row.one_time_amount, row.currency),
                "future_period_amount": MoneyOut.build(
                    row.future_period_amount, row.currency
                ),
                "auto_renewal": row.auto_renewal,
                "termination_notice_days": row.termination_notice_days,
                "is_scanned": row.is_scanned,
                "ocr_applied": row.ocr_applied,
                "is_amendment": row.is_amendment,
                "supersedes_contract_id": (
                    str(row.supersedes_contract_id)
                    if row.supersedes_contract_id
                    else None
                ),
                "extraction_confidence": row.extraction_confidence,
                "unknown_fields": row.unknown_fields,
                "needs_human_review": row.needs_human_review,
                "review_reasons": row.review_reasons,
            }
            for row in rows
        ]
    }


@router.get("/workspaces/{workspace_id}/contracts/{contract_id}/citations")
async def contract_citations(contract_id: str, ctx: Workspace_, session: DbSession):
    """Page-level citations behind each extracted value.

    Every row records whether the verifier re-fetched the cited span and found the
    quote — an unverified citation is shown, not hidden, because a value resting on
    one is a value that was discarded.
    """
    import uuid as _uuid

    try:
        target = _uuid.UUID(contract_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid contract id") from exc

    contract = await session.get(Contract, target)
    if contract is None or contract.workspace_id != ctx.workspace_id:
        raise HTTPException(status_code=404, detail="contract not found")

    rows = (
        (
            await session.execute(
                select(Citation)
                .where(Citation.contract_id == target)
                .order_by(Citation.field_name)
            )
        )
        .scalars()
        .all()
    )
    return {
        "document_name": contract.document_name,
        "citations": [
            {
                "field_name": row.field_name,
                "field_value": row.field_value,
                "page_number": row.page_number,
                "quote": row.quote,
                "quote_hash": row.quote_hash,
                "span": [row.span_start, row.span_end],
                "bbox": row.bbox,
                "verified": row.verified,
                "verification_note": row.verification_note,
            }
            for row in rows
        ],
    }
