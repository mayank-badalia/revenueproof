"""Workspace endpoints — Feature 1, sub-feature 1 (workspace and revenue-claim intake)."""

from __future__ import annotations


from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, Workspace_
from app.core.events import EventKind, Severity, emit
from app.models import (
    BankTransaction,
    Contract,
    CustomerEntity,
    Invoice,
    Payment,
    ProviderConnection,
    QuarantinedRecord,
    RawRecord,
    Refund,
    ReviewItem,
    User,
    VerificationRun,
    Workspace,
    WorkspaceMember,
)
from app.models.enums import ReviewStatus, UserRole
from app.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceOut,
    WorkspaceSummary,
    WorkspaceUpdate,
)
from app.services.audit import record_audit_event

router = APIRouter(tags=["workspaces"])


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201)
async def create_workspace(payload: WorkspaceCreate, session: DbSession, user: CurrentUser):
    """Create the tenant every later record must reference."""
    workspace = Workspace(
        company_name=payload.company_name,
        legal_name=payload.legal_name,
        reporting_period_start=payload.reporting_period_start,
        reporting_period_end=payload.reporting_period_end,
        base_currency=payload.base_currency,
        claimed_revenue=payload.claimed_revenue_minor(),
        claimed_arr=payload.claimed_arr_minor(),
        materiality_threshold_pct=payload.materiality_threshold_pct,
        accounting_method=payload.accounting_method,
    )
    session.add(workspace)
    await session.flush()

    # The creator owns the workspace; every other member is invited explicitly.
    session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=UserRole.OWNER)
    )
    await session.flush()

    # Attach whatever this deployment can already reach. The credentials belong to
    # the deployment rather than to a workspace, so a new workspace here is already
    # able to pull from those accounts — and without a record saying so the UI
    # showed every source as demonstration data and offered no way to use the real
    # one short of pasting credentials the server already held.
    from app.services.ingestion import seed_deployment_connections

    connected = await seed_deployment_connections(session, workspace_id=workspace.id)

    await record_audit_event(
        session,
        workspace_id=workspace.id,
        actor_type="human",
        actor_id=str(user.id),
        action="workspace.created",
        object_type="workspace",
        object_id=str(workspace.id),
        after_state={
            "company_name": workspace.company_name,
            "connected_sources": connected,
            "claimed_revenue": workspace.claimed_revenue,
            "claimed_arr": workspace.claimed_arr,
            "period": [
                str(workspace.reporting_period_start),
                str(workspace.reporting_period_end),
            ],
        },
        reason="workspace created via API",
    )
    await session.commit()
    await session.refresh(workspace)

    emit(
        EventKind.RESULT,
        f"Workspace created: {workspace.company_name} "
        f"({workspace.reporting_period_start} → {workspace.reporting_period_end})",
        workspace_id=str(workspace.id),
        severity=Severity.SUCCESS,
        feature=1,
        claimed_revenue_minor=workspace.claimed_revenue,
        claimed_arr_minor=workspace.claimed_arr,
        currency=workspace.base_currency,
    )
    return WorkspaceOut.from_model(workspace)


@router.get("/workspaces", response_model=list[WorkspaceOut])
async def list_workspaces(session: DbSession, user: CurrentUser):
    """Only workspaces the caller is actually a member of."""
    if user.is_platform_admin:
        rows = (await session.execute(select(Workspace).order_by(Workspace.created_at.desc()))).scalars()
    else:
        rows = (
            await session.execute(
                select(Workspace)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .where(WorkspaceMember.user_id == user.id)
                .order_by(Workspace.created_at.desc())
            )
        ).scalars()
    return [WorkspaceOut.from_model(w) for w in rows]


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(ctx: Workspace_):
    return WorkspaceOut.from_model(ctx.workspace)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(payload: WorkspaceUpdate, ctx: Workspace_, session: DbSession):
    """Amend the claim under test. Changes are audited because they move the goalposts."""
    ctx.require_resolver()
    workspace = ctx.workspace
    before = {
        "company_name": workspace.company_name,
        "claimed_revenue": workspace.claimed_revenue,
        "claimed_arr": workspace.claimed_arr,
    }

    if payload.company_name is not None:
        workspace.company_name = payload.company_name
    if payload.legal_name is not None:
        workspace.legal_name = payload.legal_name
    if payload.materiality_threshold_pct is not None:
        workspace.materiality_threshold_pct = payload.materiality_threshold_pct

    from app.core.money import MoneyError, to_minor_units

    try:
        if payload.claimed_revenue is not None:
            if payload.claimed_revenue < 0:
                raise ValueError("claimed_revenue cannot be negative")
            workspace.claimed_revenue = to_minor_units(
                payload.claimed_revenue, workspace.base_currency
            )
        if payload.claimed_arr is not None:
            if payload.claimed_arr < 0:
                raise ValueError("claimed_arr cannot be negative")
            workspace.claimed_arr = to_minor_units(payload.claimed_arr, workspace.base_currency)
    except (MoneyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await record_audit_event(
        session,
        workspace_id=workspace.id,
        actor_type="human",
        actor_id=str(ctx.user.id),
        action="workspace.updated",
        object_type="workspace",
        object_id=str(workspace.id),
        before_state=before,
        after_state={
            "company_name": workspace.company_name,
            "claimed_revenue": workspace.claimed_revenue,
            "claimed_arr": workspace.claimed_arr,
        },
        reason="workspace updated via API",
    )
    await session.commit()
    await session.refresh(workspace)
    return WorkspaceOut.from_model(workspace)


@router.get("/workspaces/{workspace_id}/summary", response_model=WorkspaceSummary)
async def workspace_summary(ctx: Workspace_, session: DbSession):
    """Everything the dashboard header needs in one round trip."""
    workspace_id = ctx.workspace_id

    async def count(model) -> int:
        result = await session.execute(
            select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
        )
        return int(result.scalar_one())

    # Evidence collected, and then what each stage made of it. The second group was
    # missing, so a caller asking "what did the revenue stage produce?" had nothing to
    # read and the canvas showed a completed node with an output of zero — which reads
    # as "it ran and found nothing", the opposite of the truth.
    from app.models import Allocation, Anomaly, CriticDecision, ReportVersion, RevenueItem

    evidence_counts = {
        "raw_records": await count(RawRecord),
        "customers": await count(CustomerEntity),
        "contracts": await count(Contract),
        "invoices": await count(Invoice),
        "payments": await count(Payment),
        "refunds": await count(Refund),
        "bank_transactions": await count(BankTransaction),
        "allocations": await count(Allocation),
        "revenue_items": await count(RevenueItem),
        "anomalies": await count(Anomaly),
        "critic_decisions": await count(CriticDecision),
        "report_versions": await count(ReportVersion),
    }

    connections = [
        {
            "source_system": c.source_system,
            "is_active": c.is_active,
            "is_synthetic": c.is_synthetic,
            "is_test_mode": c.is_test_mode,
            "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
            "last_sync_status": c.last_sync_status,
            "last_sync_error": c.last_sync_error,
            "records_imported": c.records_imported,
        }
        for c in (
            await session.execute(
                select(ProviderConnection).where(ProviderConnection.workspace_id == workspace_id)
            )
        ).scalars()
    ]

    latest = (
        await session.execute(
            select(VerificationRun)
            .where(VerificationRun.workspace_id == workspace_id)
            .order_by(VerificationRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    open_reviews = int(
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

    quarantined = int(
        (
            await session.execute(
                select(func.count())
                .select_from(QuarantinedRecord)
                .where(
                    QuarantinedRecord.workspace_id == workspace_id,
                    QuarantinedRecord.resolved_at.is_(None),
                )
            )
        ).scalar_one()
    )

    from app.core.config import settings as _settings

    return WorkspaceSummary(
        workspace=WorkspaceOut.from_model(ctx.workspace),
        evidence_counts=evidence_counts,
        connections=connections,
        deployment_providers={
            name: ok
            for name, ok in _settings.provider_status().items()
            if name in {"razorpay", "zoho_books", "google_drive", "hubspot"}
        },
        latest_run=(
            {
                "id": str(latest.id),
                "status": latest.status,
                "current_stage": latest.current_stage,
                "progress_pct": latest.progress_pct,
                "started_at": latest.started_at.isoformat() if latest.started_at else None,
                "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
                "error": latest.error,
                "stage_stats": latest.stage_stats,
            }
            if latest
            else None
        ),
        open_review_items=open_reviews,
        quarantined_records=quarantined,
    )


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(ctx: Workspace_, session: DbSession):
    """Hard delete, including the graph subgraph (§17 retention/deletion control)."""
    if ctx.role not in {UserRole.OWNER, UserRole.ADMIN}:
        raise HTTPException(status_code=403, detail="only the owner can delete a workspace")

    from app.core import graph_db

    try:
        deleted = await graph_db.clear_workspace(str(ctx.workspace_id))
        emit(
            EventKind.PERSISTENCE,
            f"Removed {deleted} graph nodes for deleted workspace",
            workspace_id=str(ctx.workspace_id),
            severity=Severity.WARNING,
        )
    except Exception as exc:
        # Postgres deletion still proceeds; an orphaned subgraph is preferable to
        # leaving the authoritative records in place.
        emit(
            EventKind.ERROR,
            f"Graph cleanup failed during workspace deletion: {exc}",
            workspace_id=str(ctx.workspace_id),
            severity=Severity.WARNING,
        )

    await session.delete(ctx.workspace)
    await session.commit()
    emit(
        EventKind.PERSISTENCE,
        f"Workspace deleted: {ctx.workspace.company_name}",
        workspace_id=str(ctx.workspace_id),
        severity=Severity.WARNING,
    )


@router.post("/workspaces/{workspace_id}/members", status_code=201)
async def add_member(
    email: str,
    role: UserRole,
    ctx: Workspace_,
    session: DbSession,
):
    """Grant an existing user access. Used to add external reviewers."""
    if ctx.role not in {UserRole.OWNER, UserRole.ADMIN}:
        raise HTTPException(status_code=403, detail="only the owner can add members")

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="no user with that email")

    existing = (
        await session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == ctx.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.role = role
    else:
        session.add(
            WorkspaceMember(workspace_id=ctx.workspace_id, user_id=user.id, role=role)
        )

    await record_audit_event(
        session,
        workspace_id=ctx.workspace_id,
        actor_type="human",
        actor_id=str(ctx.user.id),
        action="workspace.member_added",
        object_type="workspace_member",
        object_id=str(user.id),
        after_state={"email": user.email, "role": str(role)},
        reason=f"granted {role} access",
    )
    await session.commit()
    return {"email": user.email, "role": role}
