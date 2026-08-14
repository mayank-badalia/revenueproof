"""Feature 8 — living evidence graph and diligence room.

The product's headline claim is that every supported rupee is traceable. These tests
are about that claim being *checkable*: the chain reaches the bank credit, a break in
it is reported rather than hidden, and the published position counts only what
survived review.

Covers Step 2a categories 1 functional, 2 edge, 4 adversarial, 6 persistence and
11 goal-fidelity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.features.room import evidence, versions
from app.models import (
    Allocation,
    BankTransaction,
    Contract,
    CriticDecision,
    CustomerEntity,
    Invoice,
    Payment,
    ReportVersion,
    RevenueItem,
    Workspace,
)
from app.models.enums import CriticVerdict, RevenueClass

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)


@pytest.fixture
async def traceable():
    """A published item with a complete chain, and one with a broken chain."""
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema

    await create_schema()
    async with get_sessionmaker()() as session:
        workspace = Workspace(
            company_name="F8 Room",
            reporting_period_start=PERIOD_START,
            reporting_period_end=PERIOD_END,
            base_currency="INR",
            claimed_revenue=10_000_000,
            claimed_arr=10_000_000,
        )
        session.add(workspace)
        await session.flush()
        ws = workspace.id

        customer = CustomerEntity(
            workspace_id=ws,
            canonical_name="Alder Systems",
            normalized_name="alder systems",
            known_aliases=["ALDER SYS"],
            domains=["alder.example"],
        )
        contract = Contract(
            workspace_id=ws,
            document_name="Alder_Systems_Agreement.pdf",
            currency="INR",
            recurring_amount=900_000,
            one_time_amount=0,
            start_date=PERIOD_START,
            end_date=PERIOD_END,
        )
        session.add_all([customer, contract])
        await session.flush()

        invoice = Invoice(
            workspace_id=ws,
            source_system="zoho_books",
            source_id="inv-1",
            invoice_number="INV-9001",
            customer_entity_id=customer.id,
            currency="INR",
            total=900_000,
            amount_due=0,
            status="paid",
            issue_date=date(2026, 5, 1),
        )
        payment = Payment(
            workspace_id=ws,
            source_system="razorpay",
            source_id="pay-1",
            customer_entity_id=customer.id,
            currency="INR",
            amount=900_000,
            status="captured",
            payment_time=datetime(2026, 5, 2, tzinfo=UTC),
            reference="pay_9001",
        )
        bank = BankTransaction(
            workspace_id=ws,
            source_system="bank_csv",
            source_id="bank-1",
            account_fingerprint="acct",
            transaction_date=date(2026, 5, 4),
            currency="INR",
            amount=882_000,
            direction="credit",
            counterparty="ALDER SYSTEMS",
            narration="NEFT CR ALDER SYSTEMS",
        )
        session.add_all([invoice, payment, bank])
        await session.flush()

        session.add(
            Allocation(
                workspace_id=ws,
                invoice_id=invoice.id,
                payment_id=payment.id,
                bank_transaction_id=bank.id,
                currency="INR",
                allocated_amount=900_000,
                method="solver",
                confidence=1.0,
            )
        )

        complete = RevenueItem(
            workspace_id=ws,
            customer_entity_id=customer.id,
            contract_id=contract.id,
            invoice_id=invoice.id,
            payment_id=payment.id,
            description="INV-9001",
            currency="INR",
            gross_amount=900_000,
            recognized_amount=900_000,
            classification=RevenueClass.VERIFIED_RECURRING,
            rule_id="R01",
            rule_explanation="Paid, retained and bank confirmed under a recurring contract.",
            is_published=True,
        )
        broken = RevenueItem(
            workspace_id=ws,
            description="Unapplied receipt",
            currency="INR",
            gross_amount=250_000,
            recognized_amount=0,
            classification=RevenueClass.PAYMENT_WITHOUT_SUPPORT,
            rule_id="R06",
            rule_explanation="Cash with no invoice or contract behind it.",
            missing_evidence=["invoice", "contract"],
            is_published=False,
        )
        session.add_all([complete, broken])
        await session.flush()
        session.add(
            CriticDecision(
                workspace_id=ws,
                revenue_item_id=complete.id,
                verdict=CriticVerdict.APPROVED,
                issue_codes=[],
                reasoning="Evidence supports the classification.",
                critic_model="test-critic",
            )
        )
        await session.commit()
        ids = (ws, complete.id, broken.id)

    yield ids
    await dispose_engine()


async def test_the_chain_reaches_the_bank_credit(traceable):
    """The product's headline claim, made checkable."""
    ws, complete_id, _ = traceable
    async with get_sessionmaker()() as session:
        trace = await evidence.trace_item(session, workspace_id=ws, item_id=complete_id)

    kinds = [node.kind for node in trace.nodes]
    assert kinds == ["customer", "contract", "invoice", "payment", "bank"]
    assert trace.complete is True
    assert trace.breaks == []
    assert trace.critic["verdict"] == "APPROVED"


async def test_every_step_is_linked_to_the_one_before(traceable):
    ws, complete_id, _ = traceable
    async with get_sessionmaker()() as session:
        trace = await evidence.trace_item(session, workspace_id=ws, item_id=complete_id)
    ids = {node.id for node in trace.nodes}
    assert trace.edges, "a chain with no edges is a list, not a trace"
    for edge in trace.edges:
        assert edge["source"] in ids and edge["target"] in ids


async def test_a_broken_chain_is_reported_not_hidden(traceable):
    """A gap a reviewer can see is worth more than a total they cannot check."""
    ws, _, broken_id = traceable
    async with get_sessionmaker()() as session:
        trace = await evidence.trace_item(session, workspace_id=ws, item_id=broken_id)
    assert trace.complete is False
    assert trace.breaks
    assert any("customer" in reason.lower() for reason in trace.breaks)
    assert trace.is_published is False


async def test_a_trace_cannot_reach_another_workspace(traceable):
    """A valid item id under the wrong workspace must not resolve (OWASP API1)."""
    _, complete_id, _ = traceable
    async with get_sessionmaker()() as session:
        trace = await evidence.trace_item(
            session, workspace_id=uuid.uuid4(), item_id=complete_id
        )
    assert trace is None


async def test_the_position_counts_only_published_figures(traceable):
    """A position built from proposals would move on every re-run and mean nothing."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        position = await versions.current_position(session, workspace_id=ws)
    assert position["verified_recurring"] == 900_000
    assert position["items_published"] == 1
    assert position["items_total"] == 2
    # The unpublished receipt is still visible as an unsupported amount.
    assert position["unsupported"] == 250_000


async def test_the_first_version_does_not_claim_a_comparison(traceable):
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        result = await versions.publish_version(session, workspace_id=ws)
        await session.commit()
    assert result.created is True
    assert result.version == 1
    assert result.changes == []
    assert "baseline" in result.explanation


async def test_an_unchanged_position_does_not_create_a_version(traceable):
    """A history whose job is to show movement must not fill with identical rows."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        await versions.publish_version(session, workspace_id=ws)
        await session.commit()
    async with get_sessionmaker()() as session:
        second = await versions.publish_version(session, workspace_id=ws)
        await session.commit()
    assert second.created is False

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(ReportVersion).where(ReportVersion.workspace_id == ws)
            )
        ).scalars().all()
    assert len(rows) == 1


async def test_a_moved_figure_produces_a_diff_computed_in_code(traceable):
    """Direction and magnitude are arithmetic; no model decides that a number moved."""
    ws, complete_id, _ = traceable
    async with get_sessionmaker()() as session:
        await versions.publish_version(session, workspace_id=ws)
        await session.commit()

    async with get_sessionmaker()() as session:
        item = await session.get(RevenueItem, complete_id)
        item.recognized_amount = 450_000  # a refund landed
        await session.commit()

    async with get_sessionmaker()() as session:
        result = await versions.publish_version(session, workspace_id=ws)
        await session.commit()

    assert result.created is True
    assert result.version == 2
    moved = {c["field"]: c for c in result.changes}
    assert "verified_recurring" in moved
    assert moved["verified_recurring"]["direction"] == "decreased"
    assert moved["verified_recurring"]["delta_minor"] == -450_000
    assert "decreased" in result.explanation


async def test_earlier_versions_stay_readable(traceable):
    """"The report said X last Tuesday" has to remain a checkable statement."""
    ws, complete_id, _ = traceable
    async with get_sessionmaker()() as session:
        await versions.publish_version(session, workspace_id=ws)
        await session.commit()
    async with get_sessionmaker()() as session:
        item = await session.get(RevenueItem, complete_id)
        item.recognized_amount = 100_000
        await session.commit()
    async with get_sessionmaker()() as session:
        await versions.publish_version(session, workspace_id=ws)
        await session.commit()

    async with get_sessionmaker()() as session:
        history = await versions.list_versions(session, workspace_id=ws)
    assert [v["version"] for v in history] == [2, 1]
    # Version 1 still states what it stated, not what is true now.
    assert "9,000.00" in history[1]["verified_recurring"]


async def test_the_diff_never_invents_a_change():
    """`diff` is pure: no previous version means no changes, never a guess."""
    assert versions.diff(None, {"verified_recurring": 5}) == []


# ---------------------------------------------------------------------------
# Continuous monitoring and impact analysis — sub-features 6-8
#
# The half that notices a report has stopped being true. What matters is that a
# change is *confirmed* rather than taken on trust, that silence is reported as
# silence, and that only the affected work is redone — re-running everything is
# correct and useless.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from app.features.room import monitor  # noqa: E402
from app.models import RawRecord  # noqa: E402


async def _vault(session, *, workspace_id, record_type, source_id, version, payload):
    record = RawRecord(
        workspace_id=workspace_id,
        source_system="zoho_books",
        record_type=record_type,
        source_id=source_id,
        payload=payload,
        content_hash=f"hash-{source_id}-v{version}",
        retrieved_at=datetime.now(UTC),
        version=version,
    )
    session.add(record)
    await session.flush()
    return record


async def test_silence_is_reported_as_silence(traceable):
    """A check that ran and found nothing must not look like a check that never ran."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        impact = await monitor.detect_changes(session, workspace_id=ws)
    assert impact.unchanged is True
    assert impact.changes == []
    assert "No source record has changed" in impact.summary


async def test_a_first_version_is_not_a_change(traceable):
    """Collecting a record for the first time is not the report moving."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        await _vault(
            session, workspace_id=ws, record_type="invoice",
            source_id="inv-fresh", version=1, payload={"customer_name": "Alder Systems"},
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        impact = await monitor.detect_changes(session, workspace_id=ws)
    assert impact.unchanged is True


async def test_a_superseding_version_is_a_confirmed_change(traceable):
    """The vault only writes v2 when the content hash actually differs."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        await _vault(
            session, workspace_id=ws, record_type="invoice",
            source_id="inv-moved", version=2,
            payload={"customer_name": "Alder Systems", "total": "900000.00"},
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        impact = await monitor.detect_changes(session, workspace_id=ws)

    assert impact.unchanged is False
    assert len(impact.changes) == 1
    change = impact.changes[0]
    assert change.record_type == "invoice"
    assert change.version == 2
    assert "Alder Systems" in change.customer_names


async def test_a_change_invalidates_only_what_it_touches(traceable):
    """Re-running everything is correct and useless; naming the owner is the point."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        await _vault(
            session, workspace_id=ws, record_type="contract",
            source_id="contract-amended", version=2,
            payload={"customer_name": "Alder Systems"},
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        impact = await monitor.detect_changes(session, workspace_id=ws)

    # A contract amendment does not invalidate identity resolution.
    assert 2 not in impact.features_to_rerun
    assert 3 in impact.features_to_rerun  # contracts must be re-read
    assert 5 in impact.features_to_rerun  # and the classification redone
    assert 7 in impact.features_to_rerun  # and re-challenged before publishing


async def test_a_payment_change_does_not_reopen_contract_reading(traceable):
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        await _vault(
            session, workspace_id=ws, record_type="payment",
            source_id="pay-refunded", version=2,
            payload={"customer_name": "Alder Systems"},
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        impact = await monitor.detect_changes(session, workspace_id=ws)
    assert 3 not in impact.features_to_rerun
    assert 4 in impact.features_to_rerun


async def test_changes_outside_the_window_are_not_reported(traceable):
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        await _vault(
            session, workspace_id=ws, record_type="invoice",
            source_id="inv-old", version=2, payload={"customer_name": "Alder Systems"},
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        impact = await monitor.detect_changes(
            session,
            workspace_id=ws,
            since=datetime.now(UTC) + timedelta(days=1),  # a window that excludes it
        )
    assert impact.unchanged is True


async def test_a_rerun_with_nothing_changed_does_no_work(traceable):
    """The expensive path must not run because someone pressed a button."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        result = await monitor.rerun_affected(
            session, workspace_id=ws, use_llm=False
        )
        await session.commit()
    assert result.ran == []
    assert "No source record has changed" in result.skipped


async def test_a_forced_rerun_versions_the_result(traceable):
    """"We checked and it holds" is a fact worth dating."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        result = await monitor.rerun_affected(
            session, workspace_id=ws, force=True, use_llm=False
        )
        await session.commit()

    assert result.ran, "a forced rerun should have done something"
    assert result.version.get("created") is True

    async with get_sessionmaker()() as session:
        history = await versions.list_versions(session, workspace_id=ws)
    assert history, "a rerun must leave a dated version behind"


async def test_monitoring_status_says_how_a_change_is_confirmed(traceable):
    """A notification is a hint; the refetched hash is the evidence."""
    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        status = await monitor.monitoring_status(session, workspace_id=ws)
    assert "content hash" in status["note"]
    assert "hint" in status["note"]
    assert status["records_with_newer_versions"] == 0


# ---------------------------------------------------------------------------
# One question, one number
# ---------------------------------------------------------------------------


async def test_the_room_and_the_report_quote_the_same_figure(traceable):
    """Two screens answering "how much is proven" must not disagree.

    Regression for the defect that produced INR 0.00 in the diligence room and
    INR 4,50,000 in the downloaded report at the same instant on the same evidence:
    the room counted published items, the report counted every classified item, and
    a workspace whose verified revenue was all withheld fell straight into the gap.
    """
    from app.features.review import report as report_builder

    ws, _, _ = traceable

    # A verified item carrying real money that the critic has *not* cleared. This is
    # the shape that separated the two screens; with none present they agreed by
    # accident and the disagreement never showed up in a test.
    async with get_sessionmaker()() as session:
        session.add(
            RevenueItem(
                workspace_id=ws,
                description="INV-9002",
                currency="INR",
                gross_amount=450_000,
                recognized_amount=450_000,
                classification=RevenueClass.VERIFIED_RECURRING,
                rule_id="R02",
                rule_explanation="Verified by the processor; no bank credit confirms it.",
                is_published=False,
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        snapshot = await versions._snapshot(session, workspace_id=ws)
        _, body = await report_builder.build_report(session, workspace_id=ws)

    proven = snapshot["cash_received"]
    assert proven == 900_000, "only the published item is proven"

    # The report must print that same figure, and must not print the withheld one
    # as though it were proven.
    assert "INR 9,000.00" in body
    assert "INR 13,500.00" not in body, (
        "the report added a withheld item into the headline; the room did not"
    )


async def test_withheld_revenue_is_reported_rather_than_dropped(traceable):
    """Agreeing by hiding the difference would be its own failure."""
    from app.features.review import report as report_builder

    ws, _, _ = traceable
    async with get_sessionmaker()() as session:
        session.add(
            RevenueItem(
                workspace_id=ws,
                description="INV-9003",
                currency="INR",
                gross_amount=450_000,
                recognized_amount=450_000,
                classification=RevenueClass.VERIFIED_RECURRING,
                rule_id="R02",
                rule_explanation="Verified by the processor; no bank credit confirms it.",
                is_published=False,
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        _, body = await report_builder.build_report(session, workspace_id=ws)

    assert "withheld pending review" in body
    assert "INR 4,500.00" in body, "the withheld amount must still be stated"
