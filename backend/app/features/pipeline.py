"""Running the agents — one at a time, or the whole chain.

The panels each own a button, which is right when you are inspecting a single stage
and wrong when you just want an answer. This module is the other door: name a stage
and it runs, or name nothing and every stage runs in order.

Three things it refuses to do, because each produces a confident wrong answer:

* **Run on an empty workspace.** Every stage would "succeed" over nothing and the
  room would report a claim proven at 0% — indistinguishable from a claim that was
  checked and failed. With no evidence the run stops before the first stage and says
  what to load.
* **Run a stage before the stage it depends on.** Classifying revenue before cash has
  been reconciled is not a faster answer, it is a different and wrong one. A single
  stage names its prerequisites and refuses rather than guessing.
* **Run stages concurrently.** They are ordered by data dependency, not by
  convenience — identity decides who a customer is, and everything above it counts
  per customer. The fan-out that *is* safe already happens inside Feature 6, where
  four independent detectors run together and join.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import claim_idempotency_key, release_idempotency_key
from app.core.events import EventKind, Severity, emit
from app.models import Invoice, Payment, RawRecord

#: Every stage a user can run, in dependency order. `needs` names the stages whose
#: output this one reads — not an ordering preference, a correctness requirement.
STAGES: tuple[dict[str, Any], ...] = (
    {
        "key": "identity",
        "feature": 2,
        "label": "Resolve customer identities",
        "purpose": "Decide which records across five systems are the same customer.",
        "needs": (),
    },
    {
        "key": "contracts",
        "feature": 3,
        "label": "Read contracts",
        "purpose": "Separate recurring subscription value from one-time fees, with a "
                   "verified page citation behind every amount.",
        "needs": (),
    },
    {
        "key": "reconcile",
        "feature": 4,
        "label": "Reconcile cash",
        "purpose": "Match invoices to payments to bank receipts and subtract refunds.",
        "needs": ("identity",),
    },
    {
        "key": "revenue",
        "feature": 5,
        "label": "Verify revenue",
        "purpose": "Classify every amount into one of eight states against the claim.",
        "needs": ("identity", "reconcile"),
    },
    {
        "key": "anomalies",
        "feature": 6,
        "label": "Scan for anomalies",
        "purpose": "Rules, an explainable model and a graph search, run independently "
                   "and joined.",
        "needs": ("revenue",),
    },
    {
        "key": "critic",
        "feature": 7,
        "label": "Run the adversarial critic",
        "purpose": "Argue against every classification before anything is published.",
        "needs": ("revenue",),
    },
    {
        "key": "publish",
        "feature": 8,
        "label": "Publish a version",
        "purpose": "Freeze the position so the next run can be compared against it.",
        "needs": ("critic",),
    },
)

STAGE_BY_KEY = {stage["key"]: stage for stage in STAGES}


@dataclass
class StageOutcome:
    key: str
    label: str
    feature: int
    status: str = "pending"          # ran | skipped | failed | pending
    detail: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "feature": self.feature,
            "status": self.status,
            "detail": self.detail,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class RunResult:
    run_id: str = ""
    ran: list[StageOutcome] = field(default_factory=list)
    blocked: str | None = None
    #: What the caller should do about being blocked, in the caller's terms.
    remedy: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.blocked is None and all(s.status != "failed" for s in self.ran)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "blocked": self.blocked,
            "remedy": self.remedy,
            "seconds": round(self.seconds, 2),
            "stages": [s.as_dict() for s in self.ran],
            "stages_run": sum(1 for s in self.ran if s.status == "ran"),
            "stages_failed": sum(1 for s in self.ran if s.status == "failed"),
        }


async def evidence_state(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Is there anything to run on, and has each stage produced anything yet?

    The UI asks this before offering a run, so "no data" is a question it can answer
    with options rather than an error it discovers afterwards.
    """
    async def count(model) -> int:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.workspace_id == workspace_id)
                )
            ).scalar_one()
        )

    from app.models import Anomaly, Contract, CustomerEntity, ReportVersion, RevenueItem

    raw = await count(RawRecord)
    completed = {
        "identity": await count(CustomerEntity) > 0,
        "contracts": await count(Contract) > 0,
        "reconcile": await count(Payment) > 0
        and await count(__import__("app.models", fromlist=["Allocation"]).Allocation) > 0,
        "revenue": await count(RevenueItem) > 0,
        "anomalies": await count(Anomaly) > 0,
        "critic": await count(
            __import__("app.models", fromlist=["CriticDecision"]).CriticDecision
        ) > 0,
        "publish": await count(ReportVersion) > 0,
    }
    return {
        "has_evidence": raw > 0,
        "raw_records": raw,
        "invoices": await count(Invoice),
        "payments": await count(Payment),
        "completed": completed,
        "stages": [
            {**{k: v for k, v in stage.items() if k != "needs"},
             "needs": list(stage["needs"]),
             "has_run": completed.get(stage["key"], False)}
            for stage in STAGES
        ],
    }


async def run(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    stages: list[str] | None = None,
    use_llm: bool = True,
    run_id: str | None = None,
) -> RunResult:
    """Run the named stages, or every stage in order when none are named."""
    run_id = run_id or uuid.uuid4().hex[:12]
    result = RunResult(run_id=run_id)
    started = time.perf_counter()

    state = await evidence_state(session, workspace_id=workspace_id)
    if not state["has_evidence"]:
        # The single most useful thing to say here is what to press next.
        result.blocked = "There is no evidence in this workspace yet."
        result.remedy = (
            "Load a dataset first: the built-in demonstration data, a generated one "
            "from a seed of your choosing, your own bank CSV and contract PDFs, or a "
            "connected account."
        )
        emit(
            EventKind.RULE,
            "Run requested with no evidence loaded — nothing was run, because every "
            "stage would have succeeded over an empty workspace and reported a claim "
            "proven at 0%, which reads exactly like a claim that was checked and "
            "failed",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            run_id=run_id,
        )
        return result

    requested = list(stages) if stages else [s["key"] for s in STAGES]
    unknown = [key for key in requested if key not in STAGE_BY_KEY]
    if unknown:
        result.blocked = f"Unknown stage(s): {', '.join(unknown)}"
        result.remedy = f"Valid stages are: {', '.join(STAGE_BY_KEY)}"
        return result

    # Keep the declared order however the caller listed them: a request for
    # ["critic", "identity"] is a request for both, not an instruction to invert the
    # dependency graph.
    ordered = [s["key"] for s in STAGES if s["key"] in set(requested)]

    # A single stage whose prerequisite has never run is refused rather than run on
    # absent input. Running the whole chain satisfies its own prerequisites.
    if len(ordered) < len(STAGES):
        for key in ordered:
            missing = [
                need
                for need in STAGE_BY_KEY[key]["needs"]
                if need not in ordered and not state["completed"].get(need)
            ]
            if missing:
                names = ", ".join(STAGE_BY_KEY[n]["label"] for n in missing)
                result.blocked = (
                    f"{STAGE_BY_KEY[key]['label']} reads work that has not been done."
                )
                result.remedy = f"Run {names} first, or run everything."
                return result

    # One run per workspace at a time. Without this, pressing "Run everything" while
    # a stage button was still working started a second reconciliation over the
    # first: both replace the whole allocation set, so PostgreSQL detected a deadlock
    # and killed one. The survivor then classified revenue with no allocations at
    # all — every invoice "invoiced, unpaid", the room reporting the claim proven at
    # ₹0.00, and nothing on screen to say the run had been cut in half.
    claimed = await claim_idempotency_key(f"pipeline:{workspace_id}", ttl=1800)
    if not claimed:
        result.blocked = "A run is already in progress for this workspace."
        result.remedy = (
            "Wait for it to finish — starting a second run would recompute the same "
            "figures from half-written work."
        )
        return result

    try:
        return await _run_stages(
            session,
            workspace_id=workspace_id,
            ordered=ordered,
            use_llm=use_llm,
            run_id=run_id,
            result=result,
            started=started,
        )
    finally:
        await release_idempotency_key(f"pipeline:{workspace_id}")


async def _run_stages(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    ordered: list[str],
    use_llm: bool,
    run_id: str,
    result: RunResult,
    started: float,
) -> RunResult:
    emit(
        EventKind.AGENT_STEP,
        f"Pipeline run {run_id}: {len(ordered)} stage(s) — "
        f"{', '.join(STAGE_BY_KEY[k]['label'] for k in ordered)}",
        workspace_id=str(workspace_id),
        run_id=run_id,
    )

    for key in ordered:
        stage = STAGE_BY_KEY[key]
        outcome = StageOutcome(key=key, label=stage["label"], feature=stage["feature"])
        result.ran.append(outcome)
        stage_started = time.perf_counter()
        emit(
            EventKind.AGENT_STEP,
            f"{stage['label']} — {stage['purpose']}",
            workspace_id=str(workspace_id),
            feature=stage["feature"],
            run_id=run_id,
        )
        try:
            outcome.detail = await _run_one(
                session,
                workspace_id=workspace_id,
                key=key,
                use_llm=use_llm,
                run_id=run_id,
            )
            outcome.status = "ran"
            # Committing per stage means a later failure keeps the earlier work,
            # which is the difference between "the critic timed out" and "the whole
            # evening's run is gone".
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            outcome.status = "failed"
            outcome.detail = f"{type(exc).__name__}: {exc}"[:300]
            emit(
                EventKind.ERROR,
                f"{stage['label']} failed: {outcome.detail}",
                workspace_id=str(workspace_id),
                severity=Severity.ERROR,
                feature=stage["feature"],
                run_id=run_id,
            )
            # Stop rather than run the next stage on the previous one's stale
            # output: a downstream figure computed from work that just failed is
            # worse than no figure, because nothing marks it as suspect.
            break
        finally:
            outcome.seconds = time.perf_counter() - stage_started

    result.seconds = time.perf_counter() - started
    emit(
        EventKind.RESULT,
        f"Pipeline run {run_id} finished in {result.seconds:.1f}s: "
        f"{sum(1 for s in result.ran if s.status == 'ran')} stage(s) completed"
        + (
            f", {sum(1 for s in result.ran if s.status == 'failed')} failed"
            if any(s.status == "failed" for s in result.ran)
            else ""
        ),
        workspace_id=str(workspace_id),
        severity=Severity.SUCCESS if result.ok else Severity.WARNING,
        run_id=run_id,
    )
    return result


async def _run_one(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    key: str,
    use_llm: bool,
    run_id: str,
) -> str:
    """Dispatch to the feature that owns the stage, and describe what it did."""
    if key == "identity":
        from app.features.identity import service as identity

        outcome = await identity.resolve_identities(
            session, workspace_id=workspace_id, use_critic=False
        )
        return (
            f"{outcome.clusters} customers from {outcome.records_considered} records; "
            f"{outcome.accepted} links accepted, {outcome.review} left for review"
        )

    if key == "contracts":
        from app.features.contracts import service as contracts

        outcome = await contracts.process_contracts(session, workspace_id=workspace_id)
        data = outcome if isinstance(outcome, dict) else outcome.as_dict()
        return (
            f"{data.get('extracted', 0)} of {data.get('processed', 0)} contracts read, "
            f"{data.get('needs_review', 0)} needing review, "
            f"{data.get('failed', 0)} failed"
        )

    if key == "reconcile":
        from app.features.reconciliation import service as reconciliation

        outcome = await reconciliation.reconcile(session, workspace_id=workspace_id)
        return (
            f"solver {outcome.solver_status}, {outcome.allocations_written} links "
            f"written, conservation "
            f"{'verified' if outcome.conservation_ok else 'FAILED'}"
        )

    if key == "revenue":
        from app.features.revenue import service as revenue

        outcome = await revenue.verify_revenue(session, workspace_id=workspace_id)
        return (
            f"{outcome.items_classified} items classified across "
            f"{len(outcome.by_class)} states"
        )

    if key == "anomalies":
        from app.features.anomaly import service as anomaly

        outcome = await anomaly.scan(
            session, workspace_id=workspace_id, use_llm=use_llm
        )
        return (
            f"{outcome.findings_total} indicators across {len(outcome.by_rule)} rules"
        )

    if key == "critic":
        from app.features.review import verify

        outcome = await verify.run_maker_checker(
            session, workspace_id=workspace_id, use_llm=use_llm
        )
        data = outcome.as_dict()
        return (
            f"{data.get('approved', 0)} approved and published, "
            f"{data.get('disputed', 0)} disputed, "
            f"{data.get('review_items_created', 0)} routed to a human"
        )

    if key == "publish":
        from app.features.room import versions

        outcome = await versions.publish_version(session, workspace_id=workspace_id)
        if not outcome.created:
            return "position unchanged — no new version was created"
        return f"version {outcome.version} published"

    raise ValueError(f"no runner for stage {key!r}")
