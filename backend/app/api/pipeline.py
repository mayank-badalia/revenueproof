"""Run the agents — one stage, or the whole chain."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import DbSession, Workspace_
from app.features import pipeline

router = APIRouter(tags=["pipeline"])


class RunRequest(BaseModel):
    #: Omit to run everything in order. Name one (or several) to run just those.
    stages: list[str] | None = None
    use_llm: bool = True


@router.get("/workspaces/{workspace_id}/pipeline")
async def pipeline_state(ctx: Workspace_, session: DbSession):
    """What can be run, what has already run, and whether there is data to run on.

    The UI asks before offering the button, so "no evidence yet" is a question it can
    answer with a choice of datasets rather than an error discovered after clicking.
    """
    return await pipeline.evidence_state(session, workspace_id=ctx.workspace_id)


@router.post("/workspaces/{workspace_id}/pipeline/run")
async def run_pipeline(payload: RunRequest, ctx: Workspace_, session: DbSession):
    """Run the named stages, or every stage in dependency order."""
    ctx.require_resolver()
    result = await pipeline.run(
        session,
        workspace_id=ctx.workspace_id,
        stages=payload.stages,
        use_llm=payload.use_llm,
    )
    # Each stage commits its own work; this catches anything the last one left.
    await session.commit()
    return result.as_dict()
