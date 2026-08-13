"""Review queue and downloads — Feature 7's working end, plus report/dataset export.

Two things live here because they share an audience: the person who has to *act* on
what the pipeline found. The queue is where uncertainty gets settled; the report is
what they send to whoever asked the question in the first place.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import DbSession, Workspace_
from app.features.review import exports
from app.features.review import report as report_builder
from app.features.review import service, verify

router = APIRouter(tags=["review"])


def _attachment(filename: str) -> dict[str, str]:
    """Force a save, and name the file after what is in it.

    `filename*=` carries the UTF-8 form for browsers that read RFC 5987; the plain
    `filename=` stays ASCII so older clients still get something sensible rather
    than falling back to the URL's last path segment, which is how a download ends
    up named after a random identifier.
    """
    ascii_name = filename.encode("ascii", "ignore").decode() or "revenueproof-export"
    quoted = quote(filename)
    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'
        ),
        # The browser must not sniff a CSV into something it thinks it can render.
        "X-Content-Type-Options": "nosniff",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }


class ResolveRequest(BaseModel):
    decision: str = Field(description="approved | rejected | corrected")
    #: Apply the decision to every record this question covers, not just the row
    #: that happens to represent it.
    apply_to_group: bool = True
    #: Required, and required to be non-empty. §7: an override carries a reason or
    #: it is not auditable.
    reason: str = Field(min_length=3, max_length=4000)
    remember: bool = True


class CriticRequest(BaseModel):
    #: The deterministic half always runs. This only decides whether material items
    #: that pass it are also argued with by the model.
    use_llm: bool = True


@router.post("/workspaces/{workspace_id}/critic/run")
async def run_critic(payload: CriticRequest, ctx: Workspace_, session: DbSession):
    """Challenge every classification, route disputes, publish what survives."""
    ctx.require_resolver()
    result = await verify.run_maker_checker(
        session, workspace_id=ctx.workspace_id, use_llm=payload.use_llm
    )
    await session.commit()
    return result.as_dict()


@router.get("/workspaces/{workspace_id}/critic")
async def list_critic_decisions(ctx: Workspace_, session: DbSession):
    """Every verdict, with the checks and the route behind it."""
    from sqlalchemy import select as _select

    from app.models import CriticDecision, RevenueItem

    rows = (
        await session.execute(
            _select(CriticDecision, RevenueItem)
            .join(RevenueItem, CriticDecision.revenue_item_id == RevenueItem.id)
            .where(CriticDecision.workspace_id == ctx.workspace_id)
        )
    ).all()
    order = {"DISPUTED": 0, "MORE_EVIDENCE_REQUIRED": 1, "APPROVED": 2}
    rows.sort(key=lambda pair: (order.get(str(pair[0].verdict), 9), -pair[1].recognized_amount))
    return {
        "decisions": [
            {
                "id": str(decision.id),
                "revenue_item_id": str(decision.revenue_item_id),
                "description": item.description,
                "classification": str(item.classification),
                "recognized_minor": item.recognized_amount,
                "is_published": item.is_published,
                "verdict": str(decision.verdict),
                "issue_codes": decision.issue_codes,
                "reasoning": decision.reasoning,
                "requested_evidence": decision.requested_evidence,
                "deterministic_findings": decision.deterministic_findings,
                "routed_to_feature": decision.routed_to_feature,
                "critic_model": decision.critic_model,
            }
            for decision, item in rows
        ],
        "note": (
            "Only an APPROVED item is published. A disputed item keeps the "
            "classification Feature 5 gave it and stops being publishable — the "
            "critic never rewrites a financial figure."
        ),
    }


@router.get("/workspaces/{workspace_id}/review")
async def list_review(
    ctx: Workspace_,
    session: DbSession,
    status: str = Query("open", pattern="^(open|in_progress|resolved|dismissed|all)$"),
):
    """The queue every upstream feature routes its uncertainty into."""
    return {
        "summary": (
            await service.summarise(session, workspace_id=ctx.workspace_id)
        ).as_dict(),
        "items": await service.list_items(
            session, workspace_id=ctx.workspace_id, status=status
        ),
        "decisions": list(service.DECISIONS),
        "can_resolve": ctx.can_resolve,
    }


@router.post("/workspaces/{workspace_id}/review/{item_id}/claim")
async def claim_item(item_id: uuid.UUID, ctx: Workspace_, session: DbSession):
    """Mark an item as being worked so two reviewers do not duplicate the effort."""
    ctx.require_resolver()
    row = await service.claim(session, workspace_id=ctx.workspace_id, item_id=item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="review item not found")
    await session.commit()
    return {"id": str(row.id), "status": str(row.status)}


@router.post("/workspaces/{workspace_id}/review/{item_id}/resolve")
async def resolve_item(
    item_id: uuid.UUID,
    payload: ResolveRequest,
    ctx: Workspace_,
    session: DbSession,
):
    """Close one item with a decision and a reason."""
    ctx.require_resolver()
    members: list[uuid.UUID] = [item_id]
    if payload.apply_to_group:
        for group in await service.list_items(
            session, workspace_id=ctx.workspace_id, status="all"
        ):
            if str(item_id) in group["member_ids"]:
                members = [uuid.UUID(m) for m in group["member_ids"]]
                break

    try:
        resolved = await service.resolve_group(
            session,
            workspace_id=ctx.workspace_id,
            item_ids=members,
            decision=payload.decision,
            reason=payload.reason,
            user_id=ctx.user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not resolved:
        raise HTTPException(status_code=404, detail="review item not found")
    row = await session.get(__import__("app.models", fromlist=["ReviewItem"]).ReviewItem, item_id)
    await session.commit()
    return {
        "id": str(item_id),
        "resolved_count": resolved,
        "status": str(row.status) if row else "resolved",
        "resolution": row.resolution if row else payload.decision,
        "summary": (
            await service.summarise(session, workspace_id=ctx.workspace_id)
        ).as_dict(),
    }


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/report")
async def download_report(ctx: Workspace_, session: DbSession):
    """The evidence position as a self-contained file that survives being emailed."""
    try:
        filename, body = await report_builder.build_report(
            session, workspace_id=ctx.workspace_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers=_attachment(filename),
    )


@router.get("/workspaces/{workspace_id}/downloads")
async def list_downloads(ctx: Workspace_, session: DbSession):
    """What can be downloaded, so the UI renders buttons rather than guessing."""
    return {"artifacts": exports.catalogue()}


@router.get("/workspaces/{workspace_id}/downloads/bundle")
async def download_bundle(ctx: Workspace_, session: DbSession):
    """Everything at once: the report, every table as CSV, and a README."""
    try:
        artifact = await exports.build_bundle(session, workspace_id=ctx.workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=artifact.body,
        media_type=artifact.media_type,
        headers=_attachment(artifact.filename),
    )


@router.get("/workspaces/{workspace_id}/downloads/{key}")
async def download_artifact(key: str, ctx: Workspace_, session: DbSession):
    """One named table or the report on its own."""
    try:
        artifact = await exports.build_artifact(
            session, workspace_id=ctx.workspace_id, key=key
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=artifact.body,
        media_type=artifact.media_type,
        headers=_attachment(artifact.filename),
    )


@router.get("/demo-dataset")
async def download_demo_dataset(
    seed: str | None = Query(
        None,
        description="Omit for the built-in §15 dataset; supply anything for a "
        "generated one with the same cases and different companies.",
    ),
):
    """The demonstration evidence, exactly as the connectors would serve it.

    Downloadable so a reader can check that the input is ordinary business data
    rather than something shaped to make the product look good — and so the bank CSV
    can be re-uploaded through the normal upload path, which is the same code a real
    statement goes through.

    Deliberately unauthenticated: it is synthetic data about companies that do not
    exist, and requiring a login to inspect the demo's inputs would defeat the point
    of publishing them.
    """
    from app.connectors.synthetic import customers as roster
    from app.connectors.synthetic import transactions as tx
    from app.connectors.synthetic.generator import describe, generate_roster
    from app.connectors.synthetic.transactions import _translate

    customers = generate_roster(seed) if seed else None
    label = f"generated-{seed}" if seed else "template"

    with roster.use_roster(customers):
        active = roster.CUSTOMERS
        bank_rows = tx.bank_csv_rows()
        payload = {
            "readme": (
                "RevenueProof demonstration dataset. Every company here is invented. "
                "The data deliberately contains the awkward cases a real book "
                "contains: one customer spelled four ways across four systems, two "
                "different companies with near-identical names, a one-time fee "
                "described as an annual subscription, a payment refunded days later, "
                "an agent settling for two customers, money that arrives and leaves "
                "again, and cash with no invoice behind it."
            ),
            "variant": label,
            "seed": seed,
            "generated_at": datetime.now(UTC).isoformat(),
            "cases_planted": describe(list(active)),
            "expected_totals": tx.expected_totals(),
            "customers": [
                {
                    "legal_name": c.legal_name,
                    # An absent spelling is stated as absent rather than as an empty
                    # string: the unexplained-cash customer deliberately has no
                    # accounting record, and "" reads like a missing field.
                    "accounting_name": c.zoho_name or None,
                    "crm_name": c.crm_name,
                    "bank_narration_name": c.bank_narration_name,
                    "domain": c.domain,
                    "email": c.email,
                    "gstin": c.gstin,
                    "address": c.address,
                    # Case notes are written about the built-in roster and name its
                    # companies; rewritten so a generated dataset describes itself.
                    "notes": _translate(c.notes),
                    "tags": c.tags,
                }
                for c in active
            ],
            "invoices": tx.zoho_invoices(),
            "payments": tx.razorpay_payments(),
            "refunds": tx.razorpay_refunds(),
            "disputes": tx.razorpay_disputes(),
            "credit_notes": tx.zoho_credit_notes(),
            "crm_companies": tx.hubspot_companies(),
        }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "README.txt",
            f"{payload['readme']}\n\nVariant: {label}\nSeed: {seed or '(built-in)'}\n"
            f"Generated: {payload['generated_at']}\n\n"
            "bank_statement.csv can be uploaded straight back into a workspace — it "
            "goes through the same parser a real statement does.\n",
        )
        archive.writestr("dataset.json", json.dumps(payload, indent=2, default=str))
        if bank_rows:
            header = list(bank_rows[0].keys())
            lines = [",".join(header)]
            lines += [
                ",".join(f'"{str(row.get(col, "")).replace(chr(34), chr(34) * 2)}"'
                         for col in header)
                for row in bank_rows
            ]
            archive.writestr("bank_statement.csv", "\n".join(lines))

    buffer.seek(0)
    return Response(
        content=buffer.read(),
        media_type="application/zip",
        headers={
            **_attachment(f"revenueproof-demo-{label}.zip")
        },
    )
