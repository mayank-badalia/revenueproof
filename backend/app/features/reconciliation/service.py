"""Reconciliation pipeline — F4 sub-features 3-6 plus orchestration.

Answers the question the whole product exists for: **did the money actually arrive,
and did the company keep it?**

    invoices + payments + bank receipts + refunds
      → candidate links → constrained allocation → settlement verification
      → refund and chargeback subtraction → retained cash per invoice

Two rules from idea_features.md §14 shape everything here:

* **A failed payment contributes zero.** Not "a little", not "pending" — zero.
* **A refund reduces the supported amount proportionally.** A ₹1,00,000 invoice paid
  in full and then 40% refunded supports ₹60,000, not ₹1,00,000 with a footnote.

Refunds are applied *after* allocation rather than by netting them off payments
first. The order matters: netting first would hide which invoice the returned money
was originally applied to, and that link is what a reviewer needs to see.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.core.money import Money, allocate_proportionally
from app.features.reconciliation import candidates as cand
from app.features.reconciliation.allocation import (
    AllocationEdge,
    AllocationInput,
    allocate,
)
from app.models import (
    Allocation,
    BankTransaction,
    CreditNote,
    Invoice,
    Payment,
    Refund,
    ReviewItem,
    Workspace,
)
from app.models.enums import AnomalySeverity, PaymentStatus
from app.services.audit import record_audit_event


@dataclass
class InvoiceOutcome:
    """What the evidence says about one invoice."""

    invoice_id: str
    invoice_number: str | None
    customer: str | None
    currency: str
    total_minor: int
    allocated_minor: int = 0
    outstanding_minor: int = 0
    refunded_minor: int = 0
    # Cash applied, less anything later returned. The figure Feature 5 classifies.
    retained_minor: int = 0
    bank_confirmed_minor: int = 0
    payment_ids: list[str] = field(default_factory=list)
    bank_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_fully_settled(self) -> bool:
        return self.outstanding_minor == 0 and self.total_minor > 0

    @property
    def has_bank_confirmation(self) -> bool:
        return self.bank_confirmed_minor > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "invoice_number": self.invoice_number,
            "customer": self.customer,
            "currency": self.currency,
            "total_minor": self.total_minor,
            "allocated_minor": self.allocated_minor,
            "outstanding_minor": self.outstanding_minor,
            "refunded_minor": self.refunded_minor,
            "retained_minor": self.retained_minor,
            "bank_confirmed_minor": self.bank_confirmed_minor,
            "fully_settled": self.is_fully_settled,
            "bank_confirmed": self.has_bank_confirmation,
            "payment_ids": self.payment_ids,
            "notes": self.notes,
        }


@dataclass
class ReconciliationResult:
    invoices_considered: int = 0
    payments_considered: int = 0
    bank_rows_considered: int = 0
    candidate_links: int = 0
    allocations_written: int = 0
    solver_status: str = ""
    conservation_ok: bool = True
    conservation_error: str | None = None

    total_invoiced_minor: int = 0
    total_allocated_minor: int = 0
    total_outstanding_minor: int = 0
    total_refunded_minor: int = 0
    total_retained_minor: int = 0
    total_bank_confirmed_minor: int = 0
    unapplied_cash_minor: int = 0

    failed_payments: int = 0
    unsupported_receipts: int = 0
    invoices_unpaid: int = 0
    review_items_created: int = 0
    outcomes: list[InvoiceOutcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "invoices_considered": self.invoices_considered,
            "payments_considered": self.payments_considered,
            "bank_rows_considered": self.bank_rows_considered,
            "candidate_links": self.candidate_links,
            "allocations_written": self.allocations_written,
            "solver_status": self.solver_status,
            "conservation_ok": self.conservation_ok,
            "conservation_error": self.conservation_error,
            "total_invoiced_minor": self.total_invoiced_minor,
            "total_allocated_minor": self.total_allocated_minor,
            "total_outstanding_minor": self.total_outstanding_minor,
            "total_refunded_minor": self.total_refunded_minor,
            "total_retained_minor": self.total_retained_minor,
            "total_bank_confirmed_minor": self.total_bank_confirmed_minor,
            "unapplied_cash_minor": self.unapplied_cash_minor,
            "failed_payments": self.failed_payments,
            "unsupported_receipts": self.unsupported_receipts,
            "invoices_unpaid": self.invoices_unpaid,
            "review_items_created": self.review_items_created,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


# ---------------------------------------------------------------------------
# Refunds — sub-feature 4
# ---------------------------------------------------------------------------


def distribute_refunds(
    allocations: list[dict[str, Any]],
    refunds_by_payment: dict[str, int],
    currency: str,
) -> dict[str, int]:
    """Push each payment's refunds back onto the invoices that payment settled.

    A refund is recorded against a *payment*, but revenue is recognised against an
    *invoice*. When one payment settled four invoices, a partial refund of that
    payment must reduce all four proportionally — assigning it to whichever invoice
    happens to be first would misstate every one of them.

    `allocate_proportionally` guarantees the split conserves to the exact minor unit.
    """
    refund_by_invoice: dict[str, int] = {}

    for payment_id, refunded in refunds_by_payment.items():
        if refunded <= 0:
            continue
        applied = [a for a in allocations if a["payment_id"] == payment_id]
        if not applied:
            # Refund of money that was never applied to an invoice. It still reduces
            # cash, but there is no invoice to reduce — Feature 5 sees it as a
            # refunded receipt without support.
            continue

        total_applied = sum(a["amount_minor"] for a in applied)
        # Never return more than was applied, whatever the processor reports.
        distributable = min(refunded, total_applied)
        shares = allocate_proportionally(
            Money(distributable, currency),
            [a["amount_minor"] for a in applied],
        )
        for allocation, share in zip(applied, shares, strict=True):
            invoice_id = allocation["invoice_id"]
            refund_by_invoice[invoice_id] = (
                refund_by_invoice.get(invoice_id, 0) + share.minor
            )

    return refund_by_invoice


def total_refunded_for_payment(
    payment: Payment, refunds: list[Refund], credit_notes_minor: int = 0
) -> int:
    """Everything returned against one payment: refunds, chargebacks, credit notes.

    Uses the larger of the processor's own `amount_refunded` field and the sum of
    linked refund records. They disagree in practice — a chargeback may appear as a
    dispute record before the payment's counter updates — and taking the larger is
    the conservative reading for revenue.
    """
    linked = sum(
        r.amount for r in refunds if r.payment_id == payment.id or
        (r.source_payment_id and r.source_payment_id == payment.source_id)
    )
    return min(payment.amount, max(payment.amount_refunded, linked) + credit_notes_minor)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def reconcile(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: str | None = None,
    persist: bool = True,
) -> ReconciliationResult:
    """Run Feature 4 end to end. Entirely deterministic — no model calls.

    `persist=False` computes the same result and writes nothing: no allocations, no
    review items, no audit event, no trace. The reconciliation view is derived state
    rather than stored state, so reopening the page had nothing to show and said
    "collect evidence first" over a workspace that had already been reconciled —
    which reads as the feature never having run. Rebuilding it through *this*
    function rather than a second read-path is the point: a reconstruction that can
    drift from the real calculation is worse than no reconstruction at all.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    result = ReconciliationResult()

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return result
    currency = workspace.base_currency

    def trace(*args, **kwargs):
        # A read-only recomputation must not narrate itself into the live trace: the
        # panel rebuilding its view on page load is not a run anyone asked for, and
        # a reviewer reading the trace would see Feature 4 apparently firing twice.
        if persist:
            emit(*args, **kwargs)

    trace(
        EventKind.AGENT_STEP,
        "Invoice Matching Agent starting",
        workspace_id=str(workspace_id),
        feature=4,
        run_id=run_id,
    )

    async def fetch(model, *where):
        query = select(model).where(model.workspace_id == workspace_id, *where)
        return list((await session.execute(query.order_by(model.id))).scalars().all())

    invoices = await fetch(Invoice)
    payments = await fetch(Payment)
    bank_rows = await fetch(BankTransaction)
    refunds = await fetch(Refund)
    credit_notes = await fetch(CreditNote)

    # Void and draft invoices are not claims on cash and must never attract an
    # allocation or appear in a total.
    live_invoices = [i for i in invoices if i.status not in {"void", "draft"}]
    successful = [p for p in payments if PaymentStatus(p.status).is_successful]

    result.invoices_considered = len(live_invoices)
    result.payments_considered = len(payments)
    result.bank_rows_considered = len(bank_rows)
    result.failed_payments = len(payments) - len(successful)
    result.total_invoiced_minor = sum(i.total for i in live_invoices)

    if not live_invoices or not successful:
        trace(
            EventKind.RESULT,
            "Reconciliation skipped: no live invoices or no successful payments",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=4,
            run_id=run_id,
        )
        return result

    # --- 1. candidate links --------------------------------------------
    invoice_payment = cand.generate_invoice_payment_candidates(live_invoices, successful)
    payment_bank = cand.generate_payment_bank_candidates(successful, bank_rows)
    result.candidate_links = len(invoice_payment) + len(payment_bank)

    trace(
        EventKind.RULE,
        f"{len(invoice_payment)} invoice-payment and {len(payment_bank)} "
        f"payment-bank candidate links generated",
        workspace_id=str(workspace_id),
        feature=4,
        run_id=run_id,
    )

    # --- 2. constrained allocation --------------------------------------
    # Payments enter net of refunds: money already returned cannot settle a bill.
    refund_totals: dict[str, int] = {}
    credit_by_invoice: dict[str, int] = {}
    for note in credit_notes:
        if note.invoice_id:
            credit_by_invoice[str(note.invoice_id)] = (
                credit_by_invoice.get(str(note.invoice_id), 0) + note.total
            )

    for payment in successful:
        refund_totals[str(payment.id)] = total_refunded_for_payment(payment, refunds)

    allocation_result = allocate(
        [
            AllocationInput(
                id=str(i.id), amount_minor=i.total, currency=i.currency,
                customer_id=str(i.customer_entity_id) if i.customer_entity_id else None,
                label=i.invoice_number or i.source_id,
            )
            for i in live_invoices
        ],
        [
            AllocationInput(
                id=str(p.id), amount_minor=p.amount, currency=p.currency,
                customer_id=str(p.customer_entity_id) if p.customer_entity_id else None,
                label=p.source_id,
            )
            for p in successful
        ],
        [
            AllocationEdge(
                invoice_id=c.left_id, payment_id=c.right_id,
                confidence=c.score, method=c.method, reasons=c.reasons,
            )
            for c in invoice_payment
        ],
        workspace_id=str(workspace_id),
    )

    result.solver_status = allocation_result.status
    result.conservation_ok = allocation_result.conservation_ok
    result.conservation_error = allocation_result.conservation_error
    result.total_allocated_minor = allocation_result.total_allocated_minor
    result.unapplied_cash_minor = sum(allocation_result.unapplied_by_payment.values())

    trace(
        EventKind.RESULT,
        f"Allocation {allocation_result.status}: "
        f"{len(allocation_result.allocations)} links, "
        f"{allocation_result.total_allocated_minor} minor units applied",
        workspace_id=str(workspace_id),
        feature=4,
        severity=Severity.SUCCESS if allocation_result.conservation_ok else Severity.ERROR,
        run_id=run_id,
        conservation_ok=allocation_result.conservation_ok,
    )

    # --- 3. settlement verification (Bank Receipt Agent) -----------------
    bank_by_payment: dict[str, list[tuple[str, int]]] = {}
    matched_bank_ids: set[str] = set()
    claimed_bank: set[str] = set()
    # Sorted by score, then by identifiers. Without the identifier tiebreak, two
    # equally-scored candidates for the same bank credit resolve in whatever order
    # the database returned them — and row order comes from UUIDs that change every
    # ingestion, so the bank-confirmed total drifted between otherwise identical
    # runs. A figure that moves without the evidence moving is not auditable.
    for link in sorted(
        payment_bank, key=lambda c: (-c.score, c.right_id, c.left_id)
    ):
        # One bank credit settles one processor payment; the highest-scoring
        # claim wins so a single receipt cannot confirm two payments.
        if link.right_id in claimed_bank:
            continue
        claimed_bank.add(link.right_id)
        matched_bank_ids.add(link.right_id)
        bank_row = next(b for b in bank_rows if str(b.id) == link.right_id)
        bank_by_payment.setdefault(link.left_id, []).append(
            (link.right_id, bank_row.amount)
        )

    # --- 4. refunds pushed back onto invoices ---------------------------
    refund_by_invoice = distribute_refunds(
        allocation_result.allocations, refund_totals, currency
    )

    # --- 5. per-invoice outcome ------------------------------------------
    for invoice in live_invoices:
        invoice_id = str(invoice.id)
        applied = [
            a for a in allocation_result.allocations if a["invoice_id"] == invoice_id
        ]
        allocated = sum(a["amount_minor"] for a in applied)
        refunded = refund_by_invoice.get(invoice_id, 0) + credit_by_invoice.get(
            invoice_id, 0
        )
        refunded = min(refunded, allocated)

        confirmed = 0
        bank_ids: list[str] = []
        for allocation in applied:
            for bank_id, _amount in bank_by_payment.get(allocation["payment_id"], []):
                bank_ids.append(bank_id)
                # The invoice's share of a bank-confirmed payment.
                confirmed += allocation["amount_minor"]

        outcome = InvoiceOutcome(
            invoice_id=invoice_id,
            invoice_number=invoice.invoice_number,
            customer=invoice.stated_customer_name,
            currency=invoice.currency,
            total_minor=invoice.total,
            allocated_minor=allocated,
            outstanding_minor=allocation_result.outstanding_by_invoice.get(
                invoice_id, invoice.total
            ),
            refunded_minor=refunded,
            retained_minor=max(0, allocated - refunded),
            bank_confirmed_minor=min(confirmed, allocated),
            payment_ids=[a["payment_id"] for a in applied],
            bank_ids=sorted(set(bank_ids)),
        )

        if not applied:
            outcome.notes.append("No payment evidence found for this invoice.")
            result.invoices_unpaid += 1
        elif outcome.outstanding_minor > 0:
            outcome.notes.append(
                f"Partially settled: {outcome.outstanding_minor} minor units outstanding."
            )
        if refunded > 0:
            outcome.notes.append(
                f"{refunded} minor units were later refunded or credited."
            )
        if applied and not bank_ids:
            outcome.notes.append(
                "Processor reports payment, but no matching bank receipt was found."
            )

        result.outcomes.append(outcome)
        result.total_outstanding_minor += outcome.outstanding_minor
        result.total_refunded_minor += outcome.refunded_minor
        result.total_retained_minor += outcome.retained_minor
        result.total_bank_confirmed_minor += outcome.bank_confirmed_minor

    # --- 6. persist -------------------------------------------------------
    unsupported = cand.unmatched_bank_credits(bank_rows, matched_bank_ids)
    # Operating inflows that are plainly not customer receipts would drown the
    # queue; only credits of material size and with a counterparty are raised.
    material = [
        row for row in unsupported
        if row.amount >= 10_000_00 and row.counterparty
    ]
    result.unsupported_receipts = len(material)

    if not persist:
        return result

    result.allocations_written = await _persist_allocations(
        session,
        workspace_id=workspace_id,
        allocations=allocation_result.allocations,
        bank_by_payment=bank_by_payment,
    )

    # --- 7. exceptions to review ------------------------------------------
    result.review_items_created = await _queue_exceptions(
        session,
        workspace_id=workspace_id,
        result=result,
        unsupported=material,
        allocation_status=allocation_result.status,
    )

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id="reconciliation",
        action="cash.reconciled",
        object_type="workspace",
        object_id=str(workspace_id),
        after_state={k: v for k, v in result.as_dict().items() if k != "outcomes"},
        reason=f"reconciliation run {run_id}",
    )

    emit(
        EventKind.RESULT,
        f"Feature 4 complete: {result.total_retained_minor} minor units retained "
        f"across {len(result.outcomes)} invoices "
        f"({result.total_bank_confirmed_minor} bank-confirmed), "
        f"{result.total_outstanding_minor} outstanding, "
        f"{result.total_refunded_minor} refunded",
        workspace_id=str(workspace_id),
        feature=4,
        severity=Severity.SUCCESS,
        run_id=run_id,
    )
    return result


async def _persist_allocations(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    allocations: list[dict[str, Any]],
    bank_by_payment: dict[str, list[tuple[str, int]]],
) -> int:
    """Replace this workspace's allocations with the current solution.

    Replacement rather than merge: allocation is a global optimisation, so a re-run
    with new evidence can legitimately move money between invoices. Merging would
    leave a stale link alongside its replacement and double-count.

    **Serialised per workspace.** Delete-all-then-insert is a deadlock waiting for a
    second caller: two overlapping reconciliations each hold rows the other needs,
    PostgreSQL kills one, and the survivor's revenue classification then runs with no
    allocations at all — so every invoice reads "invoiced, unpaid" and the room
    reports the claim proven at ₹0.00. That is the worst possible failure mode,
    because it looks exactly like a truthful answer. An advisory lock held for the
    transaction makes the second caller wait rather than die; it is released when the
    transaction ends, including on rollback, and it is scoped to this workspace so
    two different workspaces still reconcile concurrently.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"reconcile:{workspace_id}"},
    )

    existing = (
        (
            await session.execute(
                select(Allocation).where(Allocation.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await session.delete(row)
    await session.flush()

    written = 0
    for allocation in allocations:
        bank_links = bank_by_payment.get(allocation["payment_id"], [])
        session.add(
            Allocation(
                workspace_id=workspace_id,
                invoice_id=uuid.UUID(allocation["invoice_id"]),
                payment_id=uuid.UUID(allocation["payment_id"]),
                bank_transaction_id=(
                    uuid.UUID(bank_links[0][0]) if bank_links else None
                ),
                currency=allocation["currency"],
                allocated_amount=allocation["amount_minor"],
                method=allocation["method"],
                confidence=allocation["confidence"],
                rule_id=f"ALLOC_{allocation['method'].upper()}",
                reasons=allocation["reasons"],
            )
        )
        written += 1

    await session.flush()
    return written


async def _queue_exceptions(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    result: ReconciliationResult,
    unsupported: list[BankTransaction],
    allocation_status: str,
) -> int:
    """Route unresolved records to Feature 7's queue."""
    created = 0

    async def add(category: str, title: str, detail: str,
                  severity: AnomalySeverity, packet: dict[str, Any]) -> None:
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

    for row in unsupported:
        await add(
            "missing_bank_evidence",
            f"Bank receipt with no matching payment: {row.counterparty}",
            f"A credit of {row.amount} minor units on {row.transaction_date} from "
            f"{row.counterparty} has no processor payment behind it. Cash arrived "
            f"without an invoice or contract to explain it.",
            AnomalySeverity.HIGH,
            {
                "bank_transaction_id": str(row.id),
                "amount_minor": row.amount,
                "date": row.transaction_date.isoformat(),
                "counterparty": row.counterparty,
                "narration": row.narration,
            },
        )

    for outcome in result.outcomes:
        if outcome.allocated_minor > 0 and not outcome.has_bank_confirmation:
            await add(
                "missing_bank_evidence",
                f"No bank confirmation: invoice {outcome.invoice_number}",
                "The processor reports this payment as successful, but no bank "
                "receipt matches it. Captured is not the same as settled.",
                AnomalySeverity.MEDIUM,
                outcome.as_dict(),
            )
        elif 0 < outcome.allocated_minor < outcome.total_minor:
            await add(
                "partial_payment",
                f"Partially settled: invoice {outcome.invoice_number}",
                f"{outcome.outstanding_minor} minor units of "
                f"{outcome.total_minor} remain outstanding.",
                AnomalySeverity.LOW,
                outcome.as_dict(),
            )

    if not result.conservation_ok:
        await add(
            "agent_disagreement",
            "Allocation failed its conservation check",
            f"The reconciliation engine produced allocations that do not conserve "
            f"value: {result.conservation_error}. No figure from this run should be "
            f"relied on until it is resolved.",
            AnomalySeverity.HIGH,
            {"error": result.conservation_error, "status": allocation_status},
        )

    await session.flush()
    return created
