"""Maker-checker orchestration — Feature 7, sub-features 2, 3 and 7.

    Feature 5 classification + Feature 6 anomaly packet
      → deterministic completeness and policy checks
      → independent critic over the original evidence
      → APPROVED / DISPUTED / MORE_EVIDENCE_REQUIRED
      → dispute routed back to the feature that owns the failure
      → unresolved cases interrupt for a human
      → only approved items are published

**Publication is the point of this module.** Feature 5 deliberately leaves every
item `is_published = False`, because a classification nothing has argued against is
a proposal, not a result. This is what flips that bit, and it flips it only for
items that survived both the arithmetic and the critic. Everything else either goes
back to the feature that can fix it or waits for a person.

The critic's verdict is *attached* to the item rather than replacing its
classification. A disputed item keeps the class Feature 5 gave it and stops being
publishable; the critic never rewrites a financial figure, because then the figure
would depend on a model and the whole architecture rests on it not doing that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.features.review.critic import (
    CRITIC_CALL_BUDGET,
    CriticResult,
    ItemUnderReview,
    criticise,
    summarise,
)
from app.models import (
    Anomaly,
    Contract,
    CriticDecision,
    Invoice,
    ReviewItem,
    RevenueItem,
    Workspace,
)
from app.models.enums import AnomalySeverity, CriticVerdict, ReviewStatus
from app.services.audit import record_audit_event


@dataclass
class VerificationResult:
    run_id: str = ""
    items_reviewed: int = 0
    approved: int = 0
    disputed: int = 0
    more_evidence: int = 0
    published: int = 0
    unpublished: int = 0
    #: Published because every deterministic check passed, while the model still
    #: objected. Counted separately so the number is visible rather than implied:
    #: these are the items a reviewer should look at first.
    published_over_model_objection: int = 0
    review_items_created: int = 0
    critic_summary: dict[str, Any] = field(default_factory=dict)
    decisions: list[CriticResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "items_reviewed": self.items_reviewed,
            "approved": self.approved,
            "disputed": self.disputed,
            "more_evidence": self.more_evidence,
            "published": self.published,
            "published_over_model_objection": self.published_over_model_objection,
            "unpublished": self.unpublished,
            "review_items_created": self.review_items_created,
            "critic": self.critic_summary,
            "decisions": [d.as_dict() for d in self.decisions],
        }


async def run_maker_checker(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: str | None = None,
    use_llm: bool = True,
) -> VerificationResult:
    """Challenge every classification, route the disputes, publish what survives."""
    run_id = run_id or uuid.uuid4().hex[:12]
    result = VerificationResult(run_id=run_id)

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return result

    items = list(
        (
            await session.execute(
                select(RevenueItem)
                .where(RevenueItem.workspace_id == workspace_id)
                .order_by(RevenueItem.recognized_amount.desc())
            )
        )
        .scalars()
        .all()
    )
    if not items:
        emit(
            EventKind.RULE,
            "Nothing to review: run revenue verification first",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=7,
            run_id=run_id,
        )
        return result

    emit(
        EventKind.AGENT_STEP,
        f"Critic Agent starting on {len(items)} classified items — deterministic "
        f"checks first, model only for material items that pass them",
        workspace_id=str(workspace_id),
        feature=7,
        run_id=run_id,
    )

    invoices = {
        str(row.id): row
        for row in (
            await session.execute(
                select(Invoice).where(Invoice.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    }
    contracts = {
        str(row.id): row
        for row in (
            await session.execute(
                select(Contract).where(Contract.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    }
    anomalies_by_customer = await _open_anomalies(session, workspace_id=workspace_id)

    # Clear this run's previous verdicts: a decision from an earlier evidence state
    # would otherwise sit beside a fresh one and it would be impossible to tell which
    # figure the reviewer was looking at.
    for stale in (
        await session.execute(
            select(CriticDecision).where(CriticDecision.workspace_id == workspace_id)
        )
    ).scalars().all():
        await session.delete(stale)
    await session.flush()

    budget = CRITIC_CALL_BUDGET
    for item in items:
        under_review = _to_review_input(
            item, invoices=invoices, contracts=contracts,
            anomalies_by_customer=anomalies_by_customer,
        )
        # The budget is spent on the largest material items first, which is why the
        # query is ordered by recognised amount.
        allow_model = use_llm and budget > 0
        decision = await criticise(
            under_review,
            workspace_id=str(workspace_id),
            run_id=run_id,
            use_llm=allow_model,
        )
        if decision.used_model:
            budget -= 1

        result.decisions.append(decision)
        result.items_reviewed += 1

        session.add(
            CriticDecision(
                workspace_id=workspace_id,
                revenue_item_id=item.id,
                verdict=decision.verdict,
                issue_codes=decision.issue_codes,
                reasoning=decision.reasoning or "no issues found",
                challenged_evidence_ids=item.evidence_ids or [],
                requested_evidence=decision.requested_evidence,
                routed_to_feature=decision.routed_to_feature,
                critic_model=decision.model,
                deterministic_findings=[
                    f.as_dict() for f in decision.deterministic_findings
                ],
            )
        )

        # Publication is decided by the deterministic half. The model's opinion is
        # recorded, routed to a human and shown on the item — but it cannot withhold
        # a figure on its own.
        #
        # This is not a softening of the maker-checker; it is the maker-checker
        # applied to the critic too. Measured over three runs on byte-identical
        # evidence, the classifier produced 13,681,000 every single time while the
        # published total came out at 75.1%, 72.3% and 35.8% of the claim — because
        # the model returns different verdicts on the same input at temperature 0,
        # and its budget is spent on the largest items, so one changed verdict moved
        # millions. A figure that moves while the evidence does not is exactly what
        # this product says it will never produce, and "the model felt differently
        # this time" is not evidence.
        #
        # The critic keeps everything that made it worth having: it reads original
        # evidence, it argues the other side, it can only ever weaken, and every
        # objection it raises reaches a person. What it no longer has is a silent,
        # irreproducible veto over the headline number.
        blocked_by_checks = bool(decision.deterministic_findings)

        if decision.verdict is CriticVerdict.APPROVED:
            result.approved += 1
            item.is_published = True
            result.published += 1
        elif blocked_by_checks:
            # Arithmetic said no. That is reproducible and it stands.
            item.is_published = False
            result.unpublished += 1
            if decision.verdict is CriticVerdict.DISPUTED:
                result.disputed += 1
            else:
                result.more_evidence += 1
        else:
            # Every check passed and only the model objected. Publish it, carrying
            # the objection, and put the question in front of a person.
            item.is_published = True
            result.published += 1
            result.published_over_model_objection += 1
            if decision.verdict is CriticVerdict.DISPUTED:
                result.disputed += 1
            else:
                result.more_evidence += 1

    result.review_items_created = await _queue_disputes(
        session, workspace_id=workspace_id, items=items, decisions=result.decisions
    )
    result.critic_summary = summarise(result.decisions)

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id="revenue_critic",
        action="critic.reviewed",
        object_type="workspace",
        object_id=str(workspace_id),
        after_state={
            "reviewed": result.items_reviewed,
            "approved": result.approved,
            "disputed": result.disputed,
            "more_evidence": result.more_evidence,
            "published": result.published,
        },
        reason=f"maker-checker run {run_id}",
    )
    await session.flush()

    emit(
        EventKind.RESULT,
        f"Feature 7 complete: {result.approved} approved and published, "
        f"{result.disputed} disputed, {result.more_evidence} need more evidence, "
        f"{result.review_items_created} routed to a human. "
        f"{result.critic_summary.get('model_calls', 0)} model calls; the rest was "
        f"settled by deterministic checks.",
        workspace_id=str(workspace_id),
        feature=7,
        severity=Severity.SUCCESS,
        run_id=run_id,
        by_verdict=result.critic_summary.get("by_verdict", {}),
    )
    return result


async def _open_anomalies(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, list[str]]:
    """Unresolved high-severity anomaly rules, keyed by the record they name.

    Keyed by *record*, not by customer. Attaching them per customer meant one
    flagged invoice withheld every other invoice that customer had ever raised —
    three anomalies blocked twenty items, and the room published ₹9.7 lakh of the
    ₹1.14 crore it had actually verified. An indicator about one invoice is evidence
    about that invoice; treating it as evidence about the customer's whole book is
    over-reach.

    An anomaly naming no specific record — concentration, a related-party cluster —
    still falls back to the customer, because that genuinely is its subject.
    """
    rows = (
        await session.execute(
            select(Anomaly).where(
                Anomaly.workspace_id == workspace_id,
                Anomaly.status == ReviewStatus.OPEN,
                # High severity only. A LOW-severity statistical ranking is, in the
                # anomaly engine's own words, "a reason to look, never a finding" —
                # letting it veto publication would give the model the final say over
                # a figure, which is the one thing this architecture refuses.
                Anomaly.severity == AnomalySeverity.HIGH,
            )
        )
    ).scalars().all()
    by_key: dict[str, list[str]] = {}
    for row in rows:
        named = [
            str(record.get("id"))
            for record in (row.related_records or [])
            if record.get("type") in {"invoice", "payment"} and record.get("id")
        ]
        if named:
            for record_id in named:
                by_key.setdefault(record_id, []).append(row.rule_id)
        elif row.customer_entity_id is not None:
            by_key.setdefault(str(row.customer_entity_id), []).append(row.rule_id)
    return by_key


def _to_review_input(
    item: RevenueItem,
    *,
    invoices: dict[str, Invoice],
    contracts: dict[str, Contract],
    anomalies_by_customer: dict[str, list[str]],
) -> ItemUnderReview:
    """Assemble original evidence about one item from the features that own it."""
    detail = item.calculation_detail or {}
    invoice = invoices.get(str(item.invoice_id)) if item.invoice_id else None
    contract = contracts.get(str(item.contract_id)) if item.contract_id else None
    customer_key = str(item.customer_entity_id) if item.customer_entity_id else ""

    return ItemUnderReview(
        item_id=str(item.id),
        description=item.description,
        currency=item.currency,
        classification=str(item.classification),
        recognized_minor=item.recognized_amount,
        gross_minor=item.gross_amount,
        rule_id=item.rule_id or "",
        rule_explanation=item.rule_explanation or "",
        evidence_ids=list(item.evidence_ids or []),
        missing_evidence=list(item.missing_evidence or []),
        is_material=bool(item.is_material),
        customer_resolved=item.customer_entity_id is not None,
        invoice_status=str(invoice.status) if invoice else None,
        allocated_minor=int(detail.get("allocated_minor", 0) or 0),
        retained_minor=int(detail.get("retained_minor", 0) or 0),
        refunded_minor=int(detail.get("refunded_minor", 0) or 0),
        bank_confirmed_minor=int(detail.get("bank_confirmed_minor", 0) or 0),
        contract_recurring_minor=contract.recurring_amount if contract else 0,
        contract_one_time_minor=contract.one_time_amount if contract else 0,
        # A contract Feature 3 flagged for review has an unverified or contradictory
        # value behind it, which is exactly what the citation check is asking about.
        citations_verified=not (contract.needs_human_review if contract else False),
        # Absent rather than zero when Feature 5 never recorded a period split.
        in_period_minor=(
            int(detail["in_period_minor"]) if "in_period_minor" in detail else None
        ),
        future_period_minor=(
            int(detail["future_period_minor"])
            if "future_period_minor" in detail
            else None
        ),
        # The item's own invoice or payment first; the customer only as a fallback
        # for indicators that name no particular record.
        open_anomaly_rules=sorted(
            set(
                anomalies_by_customer.get(str(item.invoice_id), [])
                + anomalies_by_customer.get(str(item.payment_id), [])
                + anomalies_by_customer.get(customer_key, [])
            )
        ),
    )


async def _queue_disputes(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    items: list[RevenueItem],
    decisions: list[CriticResult],
) -> int:
    """Send everything the critic would not approve to a human, once each."""
    by_id = {str(item.id): item for item in items}
    created = 0

    for decision in decisions:
        if decision.verdict is CriticVerdict.APPROVED:
            continue
        item = by_id.get(decision.item_id)
        if item is None:
            continue

        title = f"Critic {str(decision.verdict).lower()}: {item.description}"[:300]
        existing = (
            await session.execute(
                select(ReviewItem).where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.revenue_item_id == item.id,
                    ReviewItem.status.in_(
                        [ReviewStatus.OPEN, ReviewStatus.IN_PROGRESS]
                    ),
                ).limit(1)
            )
        ).scalar_one_or_none()

        packet = {
            "verdict": str(decision.verdict),
            "issue_codes": decision.issue_codes,
            "reasoning": decision.reasoning,
            "requested_evidence": decision.requested_evidence,
            "deterministic_findings": [
                f.as_dict() for f in decision.deterministic_findings
            ],
            "routed_to_feature": decision.routed_to_feature,
            "classification": str(item.classification),
            "rule_id": item.rule_id,
            "recognized_minor": item.recognized_amount,
            "evidence_ids": item.evidence_ids,
            "settled_by": "model" if decision.used_model else "deterministic checks",
        }

        if existing is not None:
            existing.detail = decision.reasoning[:4000] or existing.detail
            existing.evidence_packet = packet
            continue

        session.add(
            ReviewItem(
                workspace_id=workspace_id,
                category="agent_disagreement",
                title=title,
                detail=decision.reasoning[:4000] or "The critic did not approve this item.",
                severity=(
                    AnomalySeverity.HIGH if item.is_material else AnomalySeverity.MEDIUM
                ),
                revenue_item_id=item.id,
                evidence_packet=packet,
            )
        )
        created += 1

    await session.flush()
    return created
