"""Revenue truth engine — F5 orchestration, totals and truth-table publication.

Produces the headline the whole product exists for: **the claim beside what the
evidence supports, and why every rupee of the difference moved.**

    founder's claim + F2 identities + F3 contract terms + F4 retained cash
      → evidence checklist → item classification → deterministic totals
      → double-count check → materiality routing → versioned truth table

Every figure here is computed by code, not a model. The only judgement calls in the
system live upstream — reading a contract (F3) and deciding whether two records are
the same customer (F2) — and both arrive here already decided, cited and reviewable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.features.contracts.service import allocate_to_period
from app.features.reconciliation import service as reconciliation
from app.features.revenue.classify import (
    Classification,
    RevenueItemInput,
    classify,
    detect_double_counting,
)
from app.features.revenue.policy import EvidenceSet, RevenuePolicy, get_policy
from app.models import (
    Allocation,
    Contract,
    Invoice,
    Payment,
    RevenueItem,
    ReviewItem,
    Workspace,
)
from app.models.enums import AnomalySeverity, PaymentStatus, RevenueClass
from app.services.audit import record_audit_event


@dataclass
class RevenueTotals:
    """The claimed-versus-verified headline, all in minor units."""

    currency: str = "INR"
    claimed_revenue: int = 0
    claimed_arr: int = 0

    cash_received: int = 0
    verified_recurring: int = 0
    verified_one_time: int = 0
    contracted_unpaid: int = 0
    invoiced_unpaid: int = 0
    refunded_reversed: int = 0
    payment_without_support: int = 0
    unsupported_claim: int = 0
    human_review: int = 0
    supported_arr: int = 0

    @property
    def total_verified(self) -> int:
        return self.verified_recurring + self.verified_one_time

    @property
    def unexplained_claim(self) -> int:
        """Claimed revenue that no evidence supports.

        Never negative: a company verifying *more* than it claimed is a finding, but
        it is not a shortfall, and reporting it as one would be misleading.
        """
        return max(0, self.claimed_revenue - self.total_verified)

    @property
    def arr_gap(self) -> int:
        return max(0, self.claimed_arr - self.supported_arr)

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "claimed_revenue": self.claimed_revenue,
            "claimed_arr": self.claimed_arr,
            "cash_received": self.cash_received,
            "verified_recurring": self.verified_recurring,
            "verified_one_time": self.verified_one_time,
            "total_verified": self.total_verified,
            "contracted_unpaid": self.contracted_unpaid,
            "invoiced_unpaid": self.invoiced_unpaid,
            "refunded_reversed": self.refunded_reversed,
            "payment_without_support": self.payment_without_support,
            "unsupported_claim": self.unsupported_claim,
            "human_review": self.human_review,
            "supported_arr": self.supported_arr,
            "unexplained_claim": self.unexplained_claim,
            "arr_gap": self.arr_gap,
        }


@dataclass
class RevenueRunResult:
    policy_version: str = "v1"
    items_classified: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    totals: RevenueTotals = field(default_factory=RevenueTotals)
    material_items: int = 0
    items_awaiting_review: int = 0
    contracts_unread: int = 0
    double_count_conflicts: list[dict[str, Any]] = field(default_factory=list)
    review_items_created: int = 0
    waterfall: list[dict[str, Any]] = field(default_factory=list)
    concentration: list[dict[str, Any]] = field(default_factory=list)
    classifications: list[Classification] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "items_classified": self.items_classified,
            "by_class": self.by_class,
            "totals": self.totals.as_dict(),
            "material_items": self.material_items,
            "items_awaiting_review": self.items_awaiting_review,
            "contracts_unread": self.contracts_unread,
            "double_count_conflicts": self.double_count_conflicts,
            "review_items_created": self.review_items_created,
            "waterfall": self.waterfall,
            "concentration": self.concentration,
            "classifications": [c.as_dict() for c in self.classifications],
        }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def verify_revenue(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    policy_version: str | None = None,
    run_id: str | None = None,
    persist: bool = True,
    refresh_allocations: bool | None = None,
) -> RevenueRunResult:
    """Run Feature 5 end to end. Deterministic — no model calls.

    `persist=False` computes the same totals and waterfall and writes nothing, so a
    reopened page can show the claimed-versus-verified position it had before
    without a second implementation of the arithmetic drifting away from this one.

    `refresh_allocations` controls the *reconciliation* underneath separately, and
    defaults to following `persist`. The two are separable because what they write
    is not comparable: rewriting an allocation destroys nothing — Feature 4 is
    deterministic and no human decision attaches to a link — while rewriting a
    revenue item discards `is_published` and the critic verdict that earned it.
    Feature 6 needs fresh allocations to see a shared payment account and must not
    touch the revenue items, which is exactly this combination.
    """
    if refresh_allocations is None:
        refresh_allocations = persist
    run_id = run_id or uuid.uuid4().hex[:12]
    policy = get_policy(policy_version)
    result = RevenueRunResult(policy_version=policy.version)

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return result

    result.totals.currency = workspace.base_currency
    result.totals.claimed_revenue = workspace.claimed_revenue
    result.totals.claimed_arr = workspace.claimed_arr

    def trace(*args, **kwargs):
        # A read-only recomputation for the page must not narrate itself into the
        # live trace as though a run had been requested.
        if persist:
            emit(*args, **kwargs)

    trace(
        EventKind.AGENT_STEP,
        f"Revenue Classification Agent starting under policy {policy.version}",
        workspace_id=str(workspace_id),
        feature=5,
        run_id=run_id,
    )

    # Reconciliation is the source of truth for what cash arrived and survived.
    # Re-running it rather than reading stored allocations keeps the two in step:
    # a stale allocation set would produce a confident, wrong headline.
    recon_result = await reconciliation.reconcile(
        session, workspace_id=workspace_id, run_id=run_id, persist=refresh_allocations
    )
    if not recon_result.conservation_ok:
        trace(
            EventKind.ERROR,
            "Reconciliation failed its conservation check; revenue verification "
            "cannot produce a trustworthy figure",
            workspace_id=str(workspace_id),
            severity=Severity.ERROR,
            feature=5,
            run_id=run_id,
        )

    items = await _build_items(
        session, workspace=workspace, recon_result=recon_result, policy=policy
    )

    # Contracts that Feature 3 has not read yet. Worth surfacing loudly: with no
    # contract terms, `require_contract_for_recurring` means nothing can qualify as
    # recurring, so ARR reads as zero. That is the correct answer given the evidence
    # — an unread contract proves nothing — but a reader seeing "supported ARR: 0"
    # deserves to know it reflects unread documents rather than a company with no
    # recurring revenue.
    unread = (
        await session.execute(
            select(Contract).where(
                Contract.workspace_id == workspace_id,
                Contract.recurring_amount == 0,
                Contract.one_time_amount == 0,
            )
        )
    ).scalars().all()
    result.contracts_unread = len(unread)
    total_contracts = (
        await session.execute(
            select(func.count()).select_from(Contract).where(
                Contract.workspace_id == workspace_id
            )
        )
    ).scalar_one()
    if result.contracts_unread:
        # The message has to match the arithmetic. Stating "supported ARR will read
        # as zero" while it reads ₹18,00,000 is a small lie that costs a reader more
        # trust than the omission would: if the narration is wrong about something
        # checkable, the figures beside it stop being worth checking.
        consequence = (
            "so supported ARR will read as zero"
            if result.contracts_unread >= total_contracts
            else "so supported ARR reflects only the contracts already read"
        )
        trace(
            EventKind.RULE,
            f"{result.contracts_unread} of {total_contracts} contracts have not been "
            f"read yet — nothing can be classified as recurring until Feature 3 "
            f"extracts their terms, {consequence}",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=5,
            run_id=run_id,
        )

    classifications = [
        classify(item, policy, claimed_revenue_minor=workspace.claimed_revenue)
        for item in items
    ]
    result.classifications = classifications
    result.items_classified = len(classifications)

    # --- totals ----------------------------------------------------------
    # ARR is a property of a *contract*, not of each invoice raised under it, so it
    # is collected per contract and summed once. Adding it per item counted one
    # customer's run-rate three times when three monthly instalments verified —
    # ₹9,00,000 of real ARR reported as ₹27,00,000. That is precisely the
    # overstatement this product exists to catch, committed by its own engine, and
    # revenue's own double-count detector could not see it: it checks that cash is
    # not counted twice, and no cash was.
    arr_by_contract: dict[str, int] = {}

    for classification in classifications:
        key = str(classification.classification)
        result.by_class[key] = result.by_class.get(key, 0) + 1
        _accumulate(result.totals, classification)
        if (
            classification.classification is RevenueClass.VERIFIED_RECURRING
            and classification.arr_contribution_minor > 0
        ):
            # Keyed on the contract where one exists; an item without one can only
            # speak for itself, so it keys on its own id rather than being merged
            # with unrelated items under a shared `None`.
            contract_key = classification.contract_id or f"item:{classification.item_id}"
            arr_by_contract[contract_key] = max(
                arr_by_contract.get(contract_key, 0),
                classification.arr_contribution_minor,
            )
        if classification.is_material:
            result.material_items += 1
        if classification.classification is RevenueClass.HUMAN_REVIEW:
            result.items_awaiting_review += 1

    result.totals.supported_arr = sum(arr_by_contract.values())
    result.totals.cash_received = recon_result.total_retained_minor

    # --- double-count check ------------------------------------------------
    payment_retained = {
        payment_id: retained
        for payment_id, retained in _payment_retained(recon_result).items()
    }
    result.double_count_conflicts = detect_double_counting(
        classifications, payment_retained
    )

    # --- presentation ------------------------------------------------------
    result.waterfall = _build_waterfall(result.totals)
    result.concentration = _build_concentration(classifications, items)

    # --- persist -----------------------------------------------------------
    if not persist:
        return result

    await _persist(
        session,
        workspace_id=workspace_id,
        items=items,
        classifications=classifications,
        policy=policy,
    )
    result.review_items_created = await _queue_reviews(
        session, workspace_id=workspace_id, result=result, items=items
    )

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id="revenue_classification",
        action="revenue.verified",
        object_type="workspace",
        object_id=str(workspace_id),
        after_state={
            "policy_version": policy.version,
            "items": result.items_classified,
            "by_class": result.by_class,
            "totals": result.totals.as_dict(),
        },
        reason=f"revenue verification run {run_id}",
        policy_version=policy.version,
    )

    totals = result.totals
    emit(
        EventKind.RESULT,
        f"Feature 5 complete: claimed {totals.claimed_revenue}, "
        f"verified {totals.total_verified} "
        f"({totals.verified_recurring} recurring + {totals.verified_one_time} one-time), "
        f"unexplained {totals.unexplained_claim}, "
        f"supported ARR {totals.supported_arr} against claimed {totals.claimed_arr}",
        workspace_id=str(workspace_id),
        feature=5,
        severity=Severity.SUCCESS,
        run_id=run_id,
        by_class=result.by_class,
        material_items=result.material_items,
    )
    return result


def _accumulate(totals: RevenueTotals, classification: Classification) -> None:
    """Add one classification into the right bucket. States are mutually exclusive."""
    amount = classification.recognized_minor
    match classification.classification:
        case RevenueClass.VERIFIED_RECURRING:
            totals.verified_recurring += amount
            # ARR deliberately not added here — see the per-contract pass in
            # verify_revenue. Summing it per item triple-counts a contract billed
            # in instalments.
        case RevenueClass.VERIFIED_ONE_TIME:
            totals.verified_one_time += amount
        case RevenueClass.CONTRACTED_UNPAID:
            totals.contracted_unpaid += classification.gross_minor
        case RevenueClass.INVOICED_UNPAID:
            totals.invoiced_unpaid += classification.gross_minor
        case RevenueClass.REFUNDED_OR_REVERSED:
            # The amount that was returned, not the amount recognised (which is zero).
            totals.refunded_reversed += classification.calculation_detail.get(
                "refunded_minor", classification.gross_minor
            )
        case RevenueClass.PAYMENT_WITHOUT_SUPPORT:
            totals.payment_without_support += classification.calculation_detail.get(
                "retained_minor", classification.gross_minor
            )
        case RevenueClass.UNSUPPORTED_CLAIM:
            totals.unsupported_claim += classification.gross_minor
        case RevenueClass.HUMAN_REVIEW:
            totals.human_review += classification.gross_minor


async def _build_items(
    session: AsyncSession,
    *,
    workspace: Workspace,
    recon_result: reconciliation.ReconciliationResult,
    policy: RevenuePolicy,
) -> list[RevenueItemInput]:
    """Assemble candidate revenue items from every upstream feature.

    An item is anchored on whichever evidence exists — usually an invoice, sometimes
    a contract with nothing invoiced, sometimes cash with no invoice at all. That
    last case is the one reviewers care about most, so it must become an item rather
    than being skipped for lack of a bill to hang it on.
    """
    workspace_id = workspace.id

    async def fetch(model, *where):
        query = select(model).where(model.workspace_id == workspace_id, *where)
        return list((await session.execute(query.order_by(model.id))).scalars().all())

    invoices = {str(i.id): i for i in await fetch(Invoice)}
    contracts = await fetch(Contract)

    # Contracts indexed by resolved customer, and by stated name for the common case
    # where identity resolution has not linked them yet.
    contracts_by_customer: dict[str, Contract] = {}
    contracts_by_name: dict[str, Contract] = {}
    for contract in contracts:
        if contract.customer_entity_id:
            contracts_by_customer.setdefault(str(contract.customer_entity_id), contract)
        if contract.stated_customer_name:
            from app.features.identity.identifiers import normalize_name

            key = normalize_name(contract.stated_customer_name)
            if key:
                contracts_by_name.setdefault(key, contract)

    items: list[RevenueItemInput] = []
    used_contracts: set[str] = set()

    # --- one item per invoice outcome from Feature 4 ---------------------
    for outcome in recon_result.outcomes:
        invoice = invoices.get(outcome.invoice_id)
        if invoice is None:
            continue

        contract = _find_contract(
            invoice, contracts_by_customer, contracts_by_name
        )
        if contract is not None:
            used_contracts.add(str(contract.id))

        allocation = _period_allocation(contract, workspace) if contract else {}

        evidence = EvidenceSet(
            has_customer=invoice.customer_entity_id is not None
            or bool(invoice.stated_customer_name),
            customer_resolved=invoice.customer_entity_id is not None,
            has_contract=contract is not None,
            contract_covers_period=bool(allocation.get("in_period_minor", 0) > 0)
            if contract
            else False,
            contract_states_recurring=bool(
                contract and contract.recurring_amount > 0
            ),
            has_invoice=True,
            invoice_is_live=invoice.status not in {"void", "draft"},
            has_payment=bool(outcome.payment_ids),
            payment_succeeded=outcome.allocated_minor > 0,
            cash_retained=outcome.retained_minor > 0,
            bank_confirmed=outcome.bank_confirmed_minor > 0,
            fully_refunded=(
                outcome.allocated_minor > 0
                and outcome.refunded_minor >= outcome.allocated_minor
            ),
            partially_refunded=0 < outcome.refunded_minor < outcome.allocated_minor,
            has_contradiction=bool(contract and contract.needs_human_review),
            contradiction_detail=(
                "; ".join(contract.review_reasons or []) if contract else ""
            ),
        )

        items.append(
            RevenueItemInput(
                item_id=f"invoice:{outcome.invoice_id}",
                description=invoice.invoice_number or invoice.source_id,
                currency=invoice.currency,
                gross_minor=invoice.total,
                evidence=evidence,
                customer_id=str(invoice.customer_entity_id)
                if invoice.customer_entity_id
                else None,
                customer_name=invoice.stated_customer_name,
                contract_id=str(contract.id) if contract else None,
                invoice_id=outcome.invoice_id,
                payment_ids=outcome.payment_ids,
                bank_ids=outcome.bank_ids,
                allocated_minor=outcome.allocated_minor,
                retained_minor=outcome.retained_minor,
                refunded_minor=outcome.refunded_minor,
                bank_confirmed_minor=outcome.bank_confirmed_minor,
                contract_recurring_minor=contract.recurring_amount if contract else 0,
                contract_one_time_minor=contract.one_time_amount if contract else 0,
                contract_start=contract.start_date if contract else None,
                contract_end=contract.end_date if contract else None,
                billing_frequency=contract.billing_frequency if contract else "unknown",
                in_period_minor=allocation.get("in_period_minor", 0),
                future_period_minor=allocation.get("future_period_minor", 0),
                annualised_recurring_minor=allocation.get(
                    "annualised_recurring_minor", 0
                ),
                allocation_detail=allocation.get("detail", {}),
                invoice_has_one_time_items=invoice.has_one_time_items,
                invoice_status=str(invoice.status),
            )
        )

    # --- contracts with nothing invoiced against them --------------------
    # Without this, a signed contract that was never billed simply disappears —
    # which is the opposite of what a reviewer needs to see.
    for contract in contracts:
        if str(contract.id) in used_contracts:
            continue
        if contract.recurring_amount == 0 and contract.one_time_amount == 0:
            continue  # not yet read by Feature 3

        allocation = _period_allocation(contract, workspace)
        evidence = EvidenceSet(
            has_customer=bool(contract.stated_customer_name),
            customer_resolved=contract.customer_entity_id is not None,
            has_contract=True,
            contract_covers_period=allocation.get("in_period_minor", 0) > 0,
            contract_states_recurring=contract.recurring_amount > 0,
            has_contradiction=bool(contract.needs_human_review),
            contradiction_detail="; ".join(contract.review_reasons or []),
        )
        items.append(
            RevenueItemInput(
                item_id=f"contract:{contract.id}",
                description=contract.document_name,
                currency=contract.currency,
                gross_minor=allocation.get("in_period_minor", 0)
                or contract.recurring_amount,
                evidence=evidence,
                customer_id=str(contract.customer_entity_id)
                if contract.customer_entity_id
                else None,
                customer_name=contract.stated_customer_name,
                contract_id=str(contract.id),
                contract_recurring_minor=contract.recurring_amount,
                contract_one_time_minor=contract.one_time_amount,
                contract_start=contract.start_date,
                contract_end=contract.end_date,
                billing_frequency=contract.billing_frequency,
                in_period_minor=allocation.get("in_period_minor", 0),
                future_period_minor=allocation.get("future_period_minor", 0),
                annualised_recurring_minor=allocation.get(
                    "annualised_recurring_minor", 0
                ),
                allocation_detail=allocation.get("detail", {}),
            )
        )

    # --- cash that no invoice or contract explains ------------------------
    # The case reviewers care about most. Anchoring items only on invoices would
    # drop it entirely: a receipt with nothing billed against it has no invoice row
    # to hang on, so it would leave no trace in the revenue figures at all — money
    # arriving from nowhere, silently omitted rather than reported.
    allocated_payment_ids = {
        str(row)
        for row in (
            await session.execute(
                select(Allocation.payment_id).where(
                    Allocation.workspace_id == workspace_id,
                    Allocation.payment_id.is_not(None),
                    Allocation.invoice_id.is_not(None),
                    Allocation.reversed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    }

    for payment in await fetch(Payment):
        if str(payment.id) in allocated_payment_ids:
            continue
        retained = max(0, payment.amount - payment.amount_refunded)
        succeeded = PaymentStatus(payment.status).is_successful
        if not succeeded:
            # A failed payment is money that never arrived. It is already reported
            # as an exception by Feature 4, and turning it into a revenue item would
            # deduct it from the claim as though someone had counted it — inventing
            # an overstatement rather than measuring one.
            continue

        contract = contracts_by_name.get(
            _normalized(payment.stated_customer_name)
        ) if payment.stated_customer_name else None

        items.append(
            RevenueItemInput(
                item_id=f"payment:{payment.id}",
                description=(
                    f"Unapplied receipt {payment.reference or payment.source_id}"
                ),
                currency=payment.currency,
                gross_minor=payment.amount,
                evidence=EvidenceSet(
                    has_customer=bool(
                        payment.customer_entity_id or payment.stated_customer_name
                    ),
                    customer_resolved=payment.customer_entity_id is not None,
                    has_contract=contract is not None,
                    contract_states_recurring=bool(
                        contract and contract.recurring_amount > 0
                    ),
                    has_invoice=False,
                    has_payment=True,
                    payment_succeeded=succeeded,
                    cash_retained=retained > 0,
                    fully_refunded=payment.amount_refunded >= payment.amount > 0,
                    partially_refunded=0 < payment.amount_refunded < payment.amount,
                ),
                customer_id=str(payment.customer_entity_id)
                if payment.customer_entity_id
                else None,
                customer_name=payment.stated_customer_name,
                contract_id=str(contract.id) if contract else None,
                payment_ids=[str(payment.id)],
                allocated_minor=payment.amount if succeeded else 0,
                retained_minor=retained if succeeded else 0,
                refunded_minor=payment.amount_refunded,
            )
        )

    return items


def _normalized(name: str) -> str:
    from app.features.identity.identifiers import normalize_name

    return normalize_name(name)


def _find_contract(
    invoice: Invoice,
    by_customer: dict[str, Contract],
    by_name: dict[str, Contract],
) -> Contract | None:
    """Locate the contract governing an invoice, by resolved identity then by name."""
    if invoice.customer_entity_id:
        contract = by_customer.get(str(invoice.customer_entity_id))
        if contract is not None:
            return contract
    if invoice.stated_customer_name:
        from app.features.identity.identifiers import normalize_name

        return by_name.get(normalize_name(invoice.stated_customer_name))
    return None


def _period_allocation(contract: Contract, workspace: Workspace) -> dict[str, Any]:
    """Reuse Feature 3's allocation engine rather than re-deriving period maths."""
    return allocate_to_period(
        recurring_minor=contract.recurring_amount,
        one_time_minor=contract.one_time_amount,
        frequency=contract.billing_frequency,
        contract_start=contract.start_date,
        contract_end=contract.end_date,
        period_start=workspace.reporting_period_start,
        period_end=workspace.reporting_period_end,
        currency=contract.currency,
    )


def _build_waterfall(totals: RevenueTotals) -> list[dict[str, Any]]:
    """Claimed → verified, with every deduction named.

    The point of a waterfall here is that each step is a *reason*, not an
    adjustment: a reviewer should be able to ask "why is this bar here?" and get an
    evidence-backed answer.

    The steps must also *reconcile*. The deduction categories are derived from
    evidence while the claim is merely asserted, so the two do not agree by
    construction — and a waterfall that starts at the claim, subtracts five named
    amounts and lands somewhere other than the verified total is worse than no
    waterfall at all, because a reviewer who checks the arithmetic stops trusting
    every other figure on the page. The difference is therefore carried as an
    explicit, named step in whichever direction it falls.
    """
    deductions = [
        ("Refunded or reversed", totals.refunded_reversed,
         "Money received and later returned."),
        ("Invoiced but unpaid", totals.invoiced_unpaid,
         "An invoice is a claim on cash, not proof of it."),
        ("Contracted but unbilled", totals.contracted_unpaid,
         "Contracted value is never added to cash received."),
        ("Cash without support", totals.payment_without_support,
         "Money arrived with no invoice or contract to explain it."),
        ("Awaiting human review", totals.human_review,
         "Evidence contradicts itself; no confident classification."),
        ("Unsupported", totals.unsupported_claim,
         "No contract, invoice or payment behind it."),
    ]

    steps: list[dict[str, Any]] = [
        {"label": "Claimed revenue", "amount_minor": totals.claimed_revenue,
         "kind": "start"},
    ]
    running = totals.claimed_revenue
    for label, amount, note in deductions:
        if amount == 0:
            continue
        steps.append(
            {"label": label, "amount_minor": -amount, "kind": "deduction", "note": note}
        )
        running -= amount

    residual = totals.total_verified - running
    if residual < 0:
        steps.append({
            "label": "Claimed but not evidenced",
            "amount_minor": residual,
            "kind": "deduction",
            "note": (
                "Nothing in the collected sources accounts for this part of the "
                "claim, for or against."
            ),
        })
    elif residual > 0:
        # A positive residual has two quite different causes, and labelling both the
        # same way produces a false statement. It is only "cash beyond the claim"
        # when verified revenue genuinely exceeds the claim. More often it means the
        # deduction categories — which are derived from evidence, not carved out of
        # the claim — add up to more than was claimed in the first place: a company
        # claiming ₹20,00,000 can easily hold ₹1,29,00,000 of unpaid invoices.
        # Saying "retained cash exceeds the figure claimed" there is simply untrue.
        if totals.total_verified > totals.claimed_revenue:
            steps.append({
                "label": "Evidence beyond the claim",
                "amount_minor": residual,
                "kind": "addition",
                "note": (
                    "Retained cash exceeds the figure claimed. Worth asking why the "
                    "claim is lower than the money received."
                ),
            })
        else:
            steps.append({
                "label": "Categories larger than the claim",
                "amount_minor": residual,
                "kind": "addition",
                "note": (
                    "The amounts above are measured from evidence, not carved out of "
                    "the claim, and together they exceed it. This line is the "
                    "arithmetic difference, not additional revenue."
                ),
            })

    # Named for its basis. This waterfall is the classifier's output, produced
    # before the critic has ruled on anything, so it is legitimately larger than the
    # room's published position — but called "Evidence-supported revenue" flat it
    # read as the headline, and a reader comparing it against the room found two
    # totals with no way to tell which question each was answering.
    steps.append({
        "label": "Evidence-supported revenue (before critic review)",
        "amount_minor": totals.total_verified,
        "kind": "total",
    })
    return steps


def _build_concentration(
    classifications: list[Classification], items: list[RevenueItemInput]
) -> list[dict[str, Any]]:
    """Verified revenue by customer, as a share of the verified total (§8)."""
    by_item = {item.item_id: item for item in items}
    per_customer: dict[str, int] = {}

    for classification in classifications:
        if not classification.classification.counts_as_verified:
            continue
        item = by_item.get(classification.item_id)
        name = (item.customer_name if item else None) or "Unattributed"
        per_customer[name] = per_customer.get(name, 0) + classification.recognized_minor

    total = sum(per_customer.values())
    if total == 0:
        return []

    ranked = sorted(per_customer.items(), key=lambda pair: -pair[1])
    return [
        {
            "customer": name,
            "amount_minor": amount,
            "share_pct": round((amount / total) * 100, 2),
        }
        for name, amount in ranked
    ]


async def _persist(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    items: list[RevenueItemInput],
    classifications: list[Classification],
    policy: RevenuePolicy,
) -> None:
    """Replace this workspace's revenue items with the current classification.

    Replacement, not merge: classification is a whole-workspace computation, and a
    stale item alongside a fresh one would double-count in every total.
    """
    existing = (
        (
            await session.execute(
                select(RevenueItem).where(RevenueItem.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await session.delete(row)
    await session.flush()

    by_id = {item.item_id: item for item in items}
    for classification in classifications:
        item = by_id[classification.item_id]
        session.add(
            RevenueItem(
                workspace_id=workspace_id,
                customer_entity_id=uuid.UUID(item.customer_id) if item.customer_id else None,
                contract_id=uuid.UUID(item.contract_id) if item.contract_id else None,
                invoice_id=uuid.UUID(item.invoice_id) if item.invoice_id else None,
                payment_id=(
                    uuid.UUID(item.payment_ids[0]) if item.payment_ids else None
                ),
                description=item.description[:500],
                currency=item.currency,
                gross_amount=classification.gross_minor,
                recognized_amount=classification.recognized_minor,
                period_start=item.contract_start,
                period_end=item.contract_end,
                classification=classification.classification,
                is_recurring=classification.is_recurring,
                evidence_strength=classification.evidence_strength,
                rule_id=classification.rule_id,
                rule_explanation=classification.explanation,
                evidence_ids=classification.evidence_ids,
                missing_evidence=classification.missing_evidence,
                calculation_detail=classification.calculation_detail,
                is_material=classification.is_material,
                policy_version=policy.version,
                # Nothing is published until Feature 7 approves or a human resolves it.
                is_published=False,
            )
        )
    await session.flush()


async def _queue_reviews(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    result: RevenueRunResult,
    items: list[RevenueItemInput],
) -> int:
    """Route contradictions, double-counts and material items to Feature 7."""
    created = 0

    async def add(category, title, detail, severity, packet) -> None:
        nonlocal created
        existing = (
            await session.execute(
                select(ReviewItem)
                .where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.title == title[:300],
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.detail = detail[:4000]
            existing.evidence_packet = packet
            return
        session.add(
            ReviewItem(
                workspace_id=workspace_id, category=category, title=title[:300],
                detail=detail[:4000], severity=severity, evidence_packet=packet,
            )
        )
        created += 1

    # HUMAN_REVIEW items are deliberately *not* queued here. Feature 7's critic
    # reviews every classified item and refuses to approve a HUMAN_REVIEW one, so it
    # raises the case with the evidence packet and the routing already attached.
    # Queueing it here as well put the same revenue item in front of a reviewer
    # twice under two different titles, which is how a queue of 74 forms out of
    # perhaps 30 real questions.

    for conflict in result.double_count_conflicts:
        await add(
            "agent_disagreement",
            f"Evidence used twice: {conflict['evidence_id']}",
            conflict["reason"],
            AnomalySeverity.HIGH,
            conflict,
        )

    await session.flush()
    return created


def _payment_retained(
    recon_result: reconciliation.ReconciliationResult,
) -> dict[str, int]:
    """How much each payment retained, for the double-count check.

    Derived from Feature 4's per-invoice outcomes: a payment's retained total is the
    sum of what it contributed to each invoice, after refunds.
    """
    retained: dict[str, int] = {}
    for outcome in recon_result.outcomes:
        if not outcome.payment_ids or outcome.retained_minor <= 0:
            continue
        # An invoice settled by several payments splits its retained cash across
        # them; charging each the full amount would make the ceiling too generous
        # and let a real over-count pass.
        share = outcome.retained_minor // len(outcome.payment_ids)
        remainder = outcome.retained_minor - share * len(outcome.payment_ids)
        for index, payment_id in enumerate(outcome.payment_ids):
            retained[payment_id] = (
                retained.get(payment_id, 0) + share + (remainder if index == 0 else 0)
            )
    return retained
