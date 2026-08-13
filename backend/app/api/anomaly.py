"""Anomaly detection endpoints — Feature 6.

Findings are investigation prompts. Nothing here approves, rejects or publishes a
revenue figure — that is Feature 7's job — so the only state a caller can change is
the reviewer's own verdict on whether a flag was worth their time, which is the
label sub-feature 7 measures precision from.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbSession, Workspace_
from app.features.anomaly import service
from app.models import Anomaly

router = APIRouter(tags=["anomaly"])

#: Order findings by how much of a reviewer's attention they deserve, not by when
#: they were written. A queue sorted by insertion time buries the serious flag under
#: whatever the last scan happened to produce.
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}


class ScanRequest(BaseModel):
    #: Narratives cost model budget and add nothing a reviewer cannot read from the
    #: packet, so a caller running against a rate-limited key can turn them off.
    use_llm: bool = True


class FeedbackRequest(BaseModel):
    is_false_positive: bool
    note: str | None = None


@router.post("/workspaces/{workspace_id}/anomalies/scan")
async def scan(payload: ScanRequest, ctx: Workspace_, session: DbSession):
    """Run every detector, persist the findings and route the material ones."""
    ctx.require_resolver()
    result = await service.scan(
        session, workspace_id=ctx.workspace_id, use_llm=payload.use_llm
    )
    await session.commit()
    return result.as_dict()


@router.get("/workspaces/{workspace_id}/anomalies")
async def list_anomalies(ctx: Workspace_, session: DbSession):
    """Stored findings, worst first, with the reviewer's verdict where one exists."""
    rows = (
        (
            await session.execute(
                select(Anomaly).where(Anomaly.workspace_id == ctx.workspace_id)
            )
        )
        .scalars()
        .all()
    )
    ordered = sorted(
        rows,
        key=lambda row: (SEVERITY_RANK.get(str(row.severity), 9), row.rule_id, str(row.id)),
    )
    return {
        "anomalies": [
            {
                "id": str(row.id),
                "rule_id": row.rule_id,
                "title": row.title,
                "severity": str(row.severity),
                "explanation": row.explanation,
                "required_check": row.required_check,
                "observed_value": row.observed_value,
                "baseline_value": row.baseline_value,
                "related_records": row.related_records,
                "graph_path": row.graph_path,
                "caveats": row.caveats,
                "customer_entity_id": (
                    str(row.customer_entity_id) if row.customer_entity_id else None
                ),
                "model_version": row.model_version,
                "model_score": row.model_score,
                "status": str(row.status),
                "is_false_positive": row.is_false_positive,
            }
            for row in ordered
        ],
        # Repeated on the list endpoint so a client that never calls the scan cannot
        # render findings without the sentence that frames them.
        "disclaimer": (
            "Every item is an anomaly indicator requiring review. None of them is a "
            "finding of wrongdoing, and none asserts that anyone acted improperly."
        ),
    }


@router.get("/workspaces/{workspace_id}/anomalies/precision")
async def precision(ctx: Workspace_, session: DbSession):
    """Measured precision per rule, and whether the model is currently allowed to run."""
    return await service.measure_precision(session, workspace_id=ctx.workspace_id)


@router.post("/workspaces/{workspace_id}/anomalies/{anomaly_id}/feedback")
async def feedback(
    anomaly_id: uuid.UUID,
    payload: FeedbackRequest,
    ctx: Workspace_,
    session: DbSession,
):
    """Record whether a flag was worth the reviewer's time."""
    ctx.require_resolver()
    row = await service.record_feedback(
        session,
        workspace_id=ctx.workspace_id,
        anomaly_id=anomaly_id,
        is_false_positive=payload.is_false_positive,
        actor_id=str(ctx.user.id),
        note=payload.note,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="anomaly not found")
    await session.commit()
    return {
        "id": str(row.id),
        "status": str(row.status),
        "is_false_positive": row.is_false_positive,
        "precision": await service.measure_precision(
            session, workspace_id=ctx.workspace_id
        ),
    }
