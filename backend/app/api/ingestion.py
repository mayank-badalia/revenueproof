"""Ingestion and connection endpoints — Feature 1.

Includes the Razorpay webhook receiver. Its contract, per core_resoruces.md, is that
a webhook is a *hint*: the signature is verified against the raw body, the event ID
is claimed for idempotency, and then the authoritative record is refetched from the
API. A replayed or out-of-order delivery therefore cannot change stored evidence.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DbSession, Workspace_
from app.connectors.bank_csv import csv_template
from app.core.cache import claim_idempotency_key
from app.core.crypto import get_cipher
from app.core.events import EventKind, Severity, emit
from app.models import ProviderConnection, QuarantinedRecord, RawRecord
from app.models.enums import SourceSystem
from app.services import ingestion, quarantine, vault

router = APIRouter(tags=["ingestion"])


class SyncRequest(BaseModel):
    sources: list[SourceSystem] | None = None
    include_bank_sample: bool = True
    #: Serve the §15 demonstration dataset even where live credentials exist.
    #: Without this, configuring a provider key silently makes every workspace —
    #: including a sales demo — reach into the founder's real accounting system.
    use_demo_data: bool = False
    #: Seed a *generated* demonstration roster instead of the built-in §15 one.
    #: Same adversarial cases, entirely different companies — which is how a viewer
    #: can tell the product is not tuned to the names in the fixture.
    dataset_seed: str | None = None
    #: Clear this workspace's evidence before loading. Defaults on for a
    #: demonstration dataset, because "generate demonstration data" reads as a
    #: replacement to anyone pressing it, and off for a live connector, where the
    #: whole point of a sync is to add what has happened since the last one.
    replace_existing: bool | None = None


class ConnectRequest(BaseModel):
    source_system: SourceSystem
    access_token: str | None = None
    refresh_token: str | None = None
    external_account_id: str | None = None
    is_test_mode: bool = True


@router.post("/workspaces/{workspace_id}/connections")
async def connect_source(payload: ConnectRequest, ctx: Workspace_, session: DbSession):
    """Register or update a provider connection. Tokens are encrypted at rest."""
    ctx.require_resolver()
    cipher = get_cipher()

    connection = (
        await session.execute(
            select(ProviderConnection).where(
                ProviderConnection.workspace_id == ctx.workspace_id,
                ProviderConnection.source_system == payload.source_system,
            )
        )
    ).scalar_one_or_none()

    if connection is None:
        connection = ProviderConnection(
            workspace_id=ctx.workspace_id,
            source_system=payload.source_system,
            display_name=str(payload.source_system),
        )
        session.add(connection)

    connection.encrypted_access_token = cipher.encrypt(payload.access_token)
    connection.encrypted_refresh_token = cipher.encrypt(payload.refresh_token)
    connection.external_account_id = payload.external_account_id
    connection.is_test_mode = payload.is_test_mode
    connection.is_active = True
    connection.is_synthetic = payload.access_token is None

    await session.commit()
    emit(
        EventKind.SYSTEM,
        f"Connected {payload.source_system}"
        f"{' (no token — synthetic mode)' if payload.access_token is None else ''}",
        workspace_id=str(ctx.workspace_id),
        severity=Severity.SUCCESS,
        feature=1,
    )
    return {
        "source_system": payload.source_system,
        "is_active": True,
        "is_synthetic": connection.is_synthetic,
    }


@router.post("/workspaces/{workspace_id}/ingest")
async def run_ingestion(payload: SyncRequest, ctx: Workspace_, session: DbSession):
    """Collect evidence from every configured source (Feature 1 end to end)."""
    ctx.require_resolver()
    return await ingestion.ingest_all(
        session,
        workspace_id=ctx.workspace_id,
        sources=payload.sources,
        include_bank_sample=payload.include_bank_sample,
        force_synthetic=payload.use_demo_data or payload.dataset_seed is not None,
        dataset_seed=payload.dataset_seed,
        replace_existing=(
            payload.replace_existing
            if payload.replace_existing is not None
            else (payload.use_demo_data or payload.dataset_seed is not None)
        ),
    )


@router.post("/workspaces/{workspace_id}/bank-csv")
async def upload_bank_csv(
    ctx: Workspace_,
    session: DbSession,
    file: Annotated[UploadFile, File()],
):
    """Import a bank statement. Safety checks run before any parsing."""
    ctx.require_resolver()
    content = await file.read()

    stats = await ingestion.ingest_bank_csv(
        session,
        workspace_id=ctx.workspace_id,
        content=content,
        filename=file.filename or "statement.csv",
        currency=ctx.workspace.base_currency,
    )
    await session.commit()

    if stats.errors and stats.canonical_written == 0:
        # The file was rejected outright — a 422 makes that unmistakable rather
        # than returning 200 with zero rows.
        raise HTTPException(status_code=422, detail=stats.errors[0])
    return stats.as_dict()


@router.post("/workspaces/{workspace_id}/contracts/upload")
async def upload_contracts(
    ctx: Workspace_,
    session: DbSession,
    files: Annotated[list[UploadFile], File()],
):
    """Vault contract PDFs so Feature 3 reads them like any other contract.

    The evidence-source screen offered "a bank statement as CSV and contracts as
    PDFs" and only the statement had anywhere to go, so a workspace built from a
    founder's own records had every contract unread — an ARR figure with nothing
    behind it, which is the outcome this product exists to prevent.
    """
    ctx.require_resolver()
    payloads = [(f.filename or "contract.pdf", await f.read()) for f in files]
    if not payloads:
        raise HTTPException(status_code=422, detail="no files were uploaded")

    result = await ingestion.ingest_contract_files(
        session, workspace_id=ctx.workspace_id, files=payloads
    )
    await session.commit()
    if not result["accepted"] and result["rejected"]:
        # Every file was refused: a 422 says so rather than a 200 with zero rows.
        raise HTTPException(
            status_code=422,
            detail="; ".join(
                f"{r['filename']}: {r['reason']}" for r in result["rejected"][:5]
            ),
        )
    return result


@router.get("/bank-csv/template")
async def download_template():
    """Blank CSV showing the expected columns."""
    return Response(
        content=csv_template(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="bank_statement_template.csv"'},
    )


@router.get("/workspaces/{workspace_id}/evidence")
async def list_evidence(
    ctx: Workspace_,
    session: DbSession,
    limit: int = Query(50, ge=1, le=200),
    record_type: str | None = None,
):
    """Vaulted evidence with its provenance hashes, for the evidence browser."""
    query = (
        select(RawRecord)
        .where(
            RawRecord.workspace_id == ctx.workspace_id,
            RawRecord.superseded_by_id.is_(None),
        )
        .order_by(RawRecord.created_at.desc())
        .limit(limit)
    )
    if record_type:
        query = query.where(RawRecord.record_type == record_type)

    rows = (await session.execute(query)).scalars().all()
    counts = (
        await session.execute(
            select(RawRecord.record_type, RawRecord.source_system, func.count())
            .where(
                RawRecord.workspace_id == ctx.workspace_id,
                RawRecord.superseded_by_id.is_(None),
            )
            .group_by(RawRecord.record_type, RawRecord.source_system)
        )
    ).all()

    return {
        "counts": [
            {"record_type": str(rt), "source_system": str(ss), "count": count}
            for rt, ss, count in counts
        ],
        "records": [
            {
                "id": str(row.id),
                "source_system": str(row.source_system),
                "record_type": str(row.record_type),
                "source_id": row.source_id,
                "content_hash": row.content_hash,
                "file_hash": row.file_hash,
                "version": row.version,
                "retrieved_at": row.retrieved_at.isoformat(),
                "has_file": row.storage_key is not None,
                "file_size_bytes": row.file_size_bytes,
            }
            for row in rows
        ],
    }


@router.get("/workspaces/{workspace_id}/evidence/{source_id}/lineage")
async def evidence_lineage(source_id: str, ctx: Workspace_, session: DbSession):
    """Full version history for one source record (W3C PROV derivation chain)."""
    history = await vault.lineage_for(
        session, workspace_id=ctx.workspace_id, source_id=source_id
    )
    if not history:
        raise HTTPException(status_code=404, detail="no evidence with that source id")
    return {"source_id": source_id, "versions": history}


@router.get("/workspaces/{workspace_id}/quarantine")
async def list_quarantine(
    ctx: Workspace_, session: DbSession, limit: int = Query(100, ge=1, le=500)
):
    """Evidence that failed validation and never reached the canonical tables."""
    rows = (
        (
            await session.execute(
                select(QuarantinedRecord)
                .where(
                    QuarantinedRecord.workspace_id == ctx.workspace_id,
                    QuarantinedRecord.resolved_at.is_(None),
                )
                .order_by(QuarantinedRecord.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    summary = await quarantine.quarantine_summary(session, ctx.workspace_id)
    return {
        "summary": summary,
        "records": [
            {
                "id": str(row.id),
                "source_system": str(row.source_system),
                "record_type": str(row.record_type),
                "source_id": row.source_id,
                "reason": str(row.reason),
                "detail": row.detail,
                "validation_errors": row.validation_errors,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# Webhooks — hints that trigger an authoritative refetch
# ---------------------------------------------------------------------------


@router.post("/webhooks/razorpay/{workspace_id}")
async def razorpay_webhook(
    workspace_id: uuid.UUID,
    request: Request,
    session: DbSession,
    x_razorpay_signature: Annotated[str | None, Header()] = None,
    x_razorpay_event_id: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Receive a Razorpay webhook.

    Deliberately unauthenticated by JWT — the caller is Razorpay, not a user. The
    HMAC signature over the raw body *is* the authentication, which is why the raw
    bytes are read before any JSON parsing: re-serialising the parsed body changes
    whitespace and key order and silently breaks verification.
    """
    from app.connectors.providers import RazorpayConnector

    raw_body = await request.body()

    if not RazorpayConnector.verify_webhook(raw_body, x_razorpay_signature or ""):
        emit(
            EventKind.ERROR,
            "Rejected Razorpay webhook: invalid or missing signature",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=1,
        )
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    # Replay prevention. Razorpay documents that deliveries may repeat and arrive
    # out of order, so the event ID is claimed exactly once.
    event_key = x_razorpay_event_id or f"body:{hash(raw_body)}"
    if not await claim_idempotency_key(f"webhook:razorpay:{workspace_id}:{event_key}"):
        emit(
            EventKind.API_CALL,
            f"Duplicate Razorpay webhook {event_key} ignored",
            workspace_id=str(workspace_id),
            severity=Severity.DEBUG,
            feature=1,
        )
        return {"status": "duplicate_ignored"}

    import json

    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed webhook body") from None

    emit(
        EventKind.API_CALL,
        f"Razorpay webhook received: {event.get('event')} — refetching from API",
        workspace_id=str(workspace_id),
        severity=Severity.INFO,
        feature=1,
        event_id=event_key,
    )

    # The webhook payload itself is never trusted as evidence; it only says
    # "something changed", so current state is pulled from the authoritative API.
    stats = await ingestion.ingest_source(
        session, workspace_id=workspace_id, source_system=SourceSystem.RAZORPAY
    )
    await session.commit()
    return {"status": "processed", "event": event.get("event"), "result": stats.as_dict()}
