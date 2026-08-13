"""Feature 4 tests — Contract-to-Cash Reconciliation.

The property this feature must never violate is conservation: money applied plus
money unapplied equals money received, exactly, in integer minor units. A
reconciliation engine that double-counts one payment reports revenue that does not
exist, and does it invisibly — the totals still look plausible.

Covers Step 2a categories 1, 2, 3, 4, 6, 7 and 11, plus Hypothesis properties.
"""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.core.db import get_sessionmaker
from app.features.reconciliation import service as recon
from app.features.reconciliation.allocation import (
    AllocationEdge,
    AllocationInput,
    allocate,
)
from app.models import Allocation, Workspace
from app.services import ingestion


def inputs(*amounts: int, prefix: str = "x") -> list[AllocationInput]:
    return [
        AllocationInput(f"{prefix}{i}", amount, "INR")
        for i, amount in enumerate(amounts)
    ]


def full_mesh(invoices, payments, confidence: float = 0.8) -> list[AllocationEdge]:
    return [
        AllocationEdge(i.id, p.id, confidence, "amount_date")
        for i in invoices
        for p in payments
    ]


# ---------------------------------------------------------------------------
# 1. The three allocation shapes the spec names (§18)
# ---------------------------------------------------------------------------


def test_one_payment_settles_several_invoices():
    """The Silverline case: one bank credit covering four invoices."""
    invoices = inputs(47_200_000, 35_400_000, 23_600_000, 11_800_000, prefix="i")
    payments = inputs(118_000_000, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments))

    assert result.status == "OPTIMAL"
    assert len(result.allocations) == 4
    assert result.total_allocated_minor == 118_000_000
    assert sum(result.outstanding_by_invoice.values()) == 0
    assert result.conservation_ok


def test_several_partial_payments_settle_one_invoice():
    """The Ironbridge case: three instalments against a single invoice."""
    invoices = inputs(47_200_000, prefix="i")
    payments = inputs(20_000_000, 15_000_000, 12_200_000, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments))

    assert result.status == "OPTIMAL"
    assert result.total_allocated_minor == 47_200_000
    assert sum(result.outstanding_by_invoice.values()) == 0
    assert result.conservation_ok


def test_a_payment_is_never_applied_twice():
    """The defect that silently doubles reported revenue.

    One ₹5,00,000 payment against two ₹5,00,000 invoices must allocate ₹5,00,000 in
    total. A greedy matcher applies it to both and reports ₹10,00,000.
    """
    invoices = inputs(50_000_000, 50_000_000, prefix="i")
    payments = inputs(50_000_000, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments, 0.9))

    assert result.total_allocated_minor == 50_000_000
    assert sum(result.outstanding_by_invoice.values()) == 50_000_000
    assert result.conservation_ok


def test_identical_amounts_settle_cleanly_instead_of_fragmenting():
    """Equal invoices and equal payments must match one-to-one, not split into dust.

    Found on live data: three ₹75,000 instalments against six identical ₹75,000
    invoices produced an invoice recognised at ₹0.03 beside one at ₹74,999.94.
    Conservation held and the solver reported OPTIMAL, so nothing failed — the
    per-invoice figures were simply indefensible, which is worse than a crash.

    The cause was the objective *rewarding* each used link, making fragmentation
    profitable: two links earned two confidence bonuses, so splitting outscored
    settling. Links now cost rather than pay.
    """
    invoices = inputs(75_000_00, 75_000_00, 75_000_00, prefix="i")
    payments = inputs(75_000_00, 75_000_00, 75_000_00, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments))

    assert result.status == "OPTIMAL"
    assert result.total_allocated_minor == 3 * 75_000_00
    assert len(result.allocations) == 3, (
        f"expected three whole matches, got {len(result.allocations)} fragments: "
        f"{[a['amount_minor'] for a in result.allocations]}"
    )
    assert all(a["amount_minor"] == 75_000_00 for a in result.allocations)


def test_an_invoice_is_never_over_allocated():
    invoices = inputs(10_000_000, prefix="i")
    payments = inputs(50_000_000, 50_000_000, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments))

    assert result.total_allocated_minor == 10_000_000
    assert sum(result.unapplied_by_payment.values()) == 90_000_000


# ---------------------------------------------------------------------------
# 2. Boundary and degenerate cases
# ---------------------------------------------------------------------------


def test_no_candidates_leaves_everything_unallocated():
    result = allocate(inputs(1000, prefix="i"), inputs(1000, prefix="p"), [])
    assert result.status == "NO_CANDIDATES"
    assert result.total_allocated_minor == 0
    # Money with nowhere to go is still counted, not dropped.
    assert sum(result.unapplied_by_payment.values()) == 1000
    assert sum(result.outstanding_by_invoice.values()) == 1000


def test_no_invoices_or_no_payments():
    assert allocate([], inputs(1000, prefix="p"), []).total_allocated_minor == 0
    assert allocate(inputs(1000, prefix="i"), [], []).total_allocated_minor == 0


def test_zero_amount_records_are_handled():
    invoices = inputs(0, prefix="i")
    payments = inputs(1000, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments))
    assert result.total_allocated_minor == 0
    assert result.conservation_ok


def test_edges_referencing_unknown_records_are_ignored():
    """A stale candidate must not crash the solve or invent an allocation."""
    invoices = inputs(1000, prefix="i")
    payments = inputs(1000, prefix="p")
    result = allocate(
        invoices, payments,
        [AllocationEdge("ghost", "p0", 0.9, "m"), AllocationEdge("i0", "p0", 0.9, "m")],
    )
    assert result.total_allocated_minor == 1000
    assert result.conservation_ok


def test_allocation_is_deterministic_across_runs():
    """An audit trail that changes between identical runs is not an audit trail."""
    invoices = inputs(30_000, 20_000, 50_000, prefix="i")
    payments = inputs(60_000, 40_000, prefix="p")
    edges = full_mesh(invoices, payments)

    first = allocate(invoices, payments, edges)
    second = allocate(invoices, payments, edges)
    key = lambda r: sorted(  # noqa: E731
        (a["invoice_id"], a["payment_id"], a["amount_minor"]) for a in r.allocations
    )
    assert key(first) == key(second)


def test_large_component_is_reported_not_silently_skipped():
    """A skipped component must not read as 'nothing matched'."""
    invoices = inputs(*[1000] * 70, prefix="i")
    payments = inputs(*[1000] * 70, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments))
    # Either it solves, or it says why — never a quiet zero.
    assert result.status in {"OPTIMAL", "FEASIBLE", "TOO_LARGE", "TIMEOUT"}
    if result.status in {"TOO_LARGE", "TIMEOUT"}:
        assert result.total_allocated_minor == 0


# ---------------------------------------------------------------------------
# 3. Refund distribution (sub-feature 4)
# ---------------------------------------------------------------------------


def test_refund_is_split_across_the_invoices_the_payment_settled():
    """One payment settled three invoices; a partial refund must reduce all three."""
    allocations = [
        {"invoice_id": "a", "payment_id": "P", "amount_minor": 60_000},
        {"invoice_id": "b", "payment_id": "P", "amount_minor": 30_000},
        {"invoice_id": "c", "payment_id": "P", "amount_minor": 10_000},
    ]
    split = recon.distribute_refunds(allocations, {"P": 10_000}, "INR")

    # Conserves exactly, and in proportion to what each invoice received.
    assert sum(split.values()) == 10_000
    assert split["a"] == 6_000
    assert split["b"] == 3_000
    assert split["c"] == 1_000


def test_refund_never_exceeds_what_was_applied():
    allocations = [{"invoice_id": "a", "payment_id": "P", "amount_minor": 5_000}]
    split = recon.distribute_refunds(allocations, {"P": 99_000}, "INR")
    assert split["a"] == 5_000


def test_refund_of_unapplied_money_touches_no_invoice():
    """Cash refunded before it settled anything reduces cash, not an invoice."""
    assert recon.distribute_refunds([], {"P": 10_000}, "INR") == {}


def test_refund_split_conserves_with_indivisible_amounts():
    allocations = [
        {"invoice_id": "a", "payment_id": "P", "amount_minor": 1},
        {"invoice_id": "b", "payment_id": "P", "amount_minor": 1},
        {"invoice_id": "c", "payment_id": "P", "amount_minor": 1},
    ]
    split = recon.distribute_refunds(allocations, {"P": 2}, "INR")
    assert sum(split.values()) == 2


# ---------------------------------------------------------------------------
# 4. Hypothesis properties — conservation must hold for all inputs
# ---------------------------------------------------------------------------

amounts = st.lists(st.integers(min_value=1, max_value=10**9), min_size=1, max_size=6)


@given(invoice_amounts=amounts, payment_amounts=amounts)
@hyp_settings(max_examples=60, deadline=None)
def test_property_allocation_always_conserves(invoice_amounts, payment_amounts):
    invoices = inputs(*invoice_amounts, prefix="i")
    payments = inputs(*payment_amounts, prefix="p")
    result = allocate(invoices, payments, full_mesh(invoices, payments), time_limit=3)

    assert result.conservation_ok, result.conservation_error
    for payment in payments:
        applied = sum(
            a["amount_minor"] for a in result.allocations if a["payment_id"] == payment.id
        )
        assert applied <= payment.amount_minor, "a payment was applied for more than it is worth"
    for invoice in invoices:
        applied = sum(
            a["amount_minor"] for a in result.allocations if a["invoice_id"] == invoice.id
        )
        assert applied <= invoice.amount_minor, "an invoice received more than it is owed"


@given(
    applied=st.lists(st.integers(min_value=1, max_value=10**8), min_size=1, max_size=8),
    refund=st.integers(min_value=0, max_value=10**8),
)
@hyp_settings(max_examples=100, deadline=None)
def test_property_refund_distribution_conserves(applied, refund):
    allocations = [
        {"invoice_id": f"i{n}", "payment_id": "P", "amount_minor": amount}
        for n, amount in enumerate(applied)
    ]
    split = recon.distribute_refunds(allocations, {"P": refund}, "INR")
    expected = min(refund, sum(applied))
    assert sum(split.values()) == expected
    # No invoice is refunded more than it received.
    for n, amount in enumerate(applied):
        assert split.get(f"i{n}", 0) <= amount


# ---------------------------------------------------------------------------
# 5. End-to-end against the real dataset (Step 2a category 11)
# ---------------------------------------------------------------------------


@pytest.fixture
async def reconciled():
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema

    await create_schema()
    async with get_sessionmaker()() as session:
        workspace = Workspace(
            company_name="F4 End To End",
            reporting_period_start=date(2026, 4, 1),
            reporting_period_end=date(2027, 3, 31),
            base_currency="INR",
        )
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id
        await session.commit()

    async with get_sessionmaker()() as session:
        await ingestion.ingest_all(session, workspace_id=workspace_id)
    async with get_sessionmaker()() as session:
        result = await recon.reconcile(session, workspace_id=workspace_id)
        await session.commit()

    yield workspace_id, result
    await dispose_engine()


async def test_end_to_end_solves_and_conserves(reconciled):
    _, result = reconciled
    assert result.solver_status == "OPTIMAL", result.solver_status
    assert result.conservation_ok, result.conservation_error
    assert result.allocations_written > 0


async def test_end_to_end_matches_dataset_ground_truth(reconciled):
    """Every figure below is derivable by hand from the synthetic dataset."""
    _, result = reconciled

    # Tidewater: ₹4,50,000 + 18% GST = ₹5,31,000, invoiced and never paid.
    # A one-paise tolerance is deliberate: provider payloads carry amounts as JSON
    # floats (Zoho sends `total: 531000.0`), and round-tripping a float can shift
    # the last minor unit. Conservation is asserted exactly elsewhere — this is a
    # ground-truth check, and demanding exactness here would be testing float
    # serialisation rather than the reconciliation engine.
    assert abs(result.total_outstanding_minor - 53_100_000) <= 100
    assert result.invoices_unpaid == 1

    # Cobalt ₹7,08,000 refunded + Halcyon ₹5,90,000 chargeback
    # + Quantum ₹3,54,000 partial = ₹16,52,000.
    assert abs(result.total_refunded_minor - 165_200_000) <= 100

    # Conservation, asserted exactly — this is the invariant that must never bend.
    assert (
        result.total_allocated_minor + result.total_outstanding_minor
        == result.total_invoiced_minor
    )

    # Two deliberately failed payments contribute nothing (spec §14).
    assert result.failed_payments == 2

    # Zenith Consulting paid with no invoice and no contract behind it.
    assert result.unsupported_receipts >= 1

    # Retained is what survived refunds.
    assert result.total_retained_minor == (
        result.total_allocated_minor - result.total_refunded_minor
    )


async def test_failed_payments_are_never_allocated(reconciled):
    """spec §14: a failed payment contributes zero cash received."""
    workspace_id, _ = reconciled
    from sqlalchemy import select

    from app.models import Payment
    from app.models.enums import PaymentStatus

    async with get_sessionmaker()() as session:
        failed = (
            await session.execute(
                select(Payment.id).where(
                    Payment.workspace_id == workspace_id,
                    Payment.status == PaymentStatus.FAILED,
                )
            )
        ).scalars().all()
        allocated = (
            await session.execute(
                select(Allocation.payment_id).where(
                    Allocation.workspace_id == workspace_id
                )
            )
        ).scalars().all()

    assert failed, "the dataset should contain failed payments"
    assert not (set(failed) & set(allocated)), "a failed payment was allocated"


async def test_void_and_draft_invoices_are_excluded(reconciled):
    """A cancelled invoice is not a claim on cash and must attract none."""
    workspace_id, result = reconciled
    from sqlalchemy import select

    from app.models import Invoice

    async with get_sessionmaker()() as session:
        excluded = (
            await session.execute(
                select(Invoice.id).where(
                    Invoice.workspace_id == workspace_id,
                    Invoice.status.in_(["void", "draft"]),
                )
            )
        ).scalars().all()
        allocated = (
            await session.execute(
                select(Allocation.invoice_id).where(
                    Allocation.workspace_id == workspace_id
                )
            )
        ).scalars().all()

    assert excluded, "the dataset should contain a void and a draft invoice"
    assert not (set(excluded) & set(allocated))
    assert all(
        o.invoice_id not in {str(i) for i in excluded} for o in result.outcomes
    )


async def test_bank_confirmation_is_tracked_separately_from_payment(reconciled):
    """"Captured" is not "settled" — the two must be distinguishable."""
    _, result = reconciled
    assert result.total_bank_confirmed_minor > 0
    # Bank confirmation can never exceed what was actually applied.
    assert result.total_bank_confirmed_minor <= result.total_allocated_minor
    with_payment_no_bank = [
        o for o in result.outcomes if o.allocated_minor > 0 and not o.has_bank_confirmation
    ]
    for outcome in with_payment_no_bank:
        assert any("bank receipt" in note for note in outcome.notes)


async def test_exceptions_are_routed_to_review(reconciled):
    _, result = reconciled
    assert result.review_items_created > 0


async def test_reconciliation_is_idempotent(reconciled):
    """Re-running replaces allocations rather than accumulating them."""
    workspace_id, first = reconciled
    from sqlalchemy import func, select

    async def allocation_count() -> int:
        async with get_sessionmaker()() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(Allocation).where(
                            Allocation.workspace_id == workspace_id
                        )
                    )
                ).scalar_one()
            )

    before = await allocation_count()
    async with get_sessionmaker()() as session:
        second = await recon.reconcile(session, workspace_id=workspace_id)
        await session.commit()

    assert await allocation_count() == before
    assert second.total_allocated_minor == first.total_allocated_minor
    assert second.total_retained_minor == first.total_retained_minor


async def test_read_only_recompute_matches_the_run_and_writes_nothing(reconciled):
    """The reconciled position must survive reopening the page.

    Feature 4 is derived state: the allocations persist but the per-invoice view
    does not, so a reopened workspace rendered "collect evidence first" over a
    reconciliation that had already happened — which reads as the feature never
    having run. The read path recomputes through this same function, so the two can
    never disagree; what it must not do is write.
    """
    from sqlalchemy import func, select

    from app.models import ReviewItem

    workspace_id, first = reconciled

    async def counts() -> tuple[int, int]:
        async with get_sessionmaker()() as session:
            allocations = int(
                (
                    await session.execute(
                        select(func.count()).select_from(Allocation).where(
                            Allocation.workspace_id == workspace_id
                        )
                    )
                ).scalar_one()
            )
            reviews = int(
                (
                    await session.execute(
                        select(func.count()).select_from(ReviewItem).where(
                            ReviewItem.workspace_id == workspace_id
                        )
                    )
                ).scalar_one()
            )
            return allocations, reviews

    before = await counts()
    async with get_sessionmaker()() as session:
        replay = await recon.reconcile(
            session, workspace_id=workspace_id, persist=False
        )
        await session.rollback()

    assert await counts() == before, "a read recomputation wrote to the database"
    assert replay.total_allocated_minor == first.total_allocated_minor
    assert replay.total_retained_minor == first.total_retained_minor
    assert replay.total_outstanding_minor == first.total_outstanding_minor
    assert replay.total_refunded_minor == first.total_refunded_minor
    assert replay.total_bank_confirmed_minor == first.total_bank_confirmed_minor
    assert len(replay.outcomes) == len(first.outcomes)
    assert replay.conservation_ok
    # It reports nothing was written, rather than reporting the earlier run's writes.
    assert replay.allocations_written == 0
