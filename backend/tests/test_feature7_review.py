"""Feature 7 queue, report and dataset export.

The queue is where every upstream feature's uncertainty ends up, so the tests are
mostly about the guarantees a reviewer relies on rather than about plumbing: a
decision cannot be recorded without a reason, a decision cannot reach another
workspace's item, and resolving an anomaly here is the same fact as marking it a
false positive on the anomaly screen — two screens must not disagree about one
finding.

Covers Step 2a categories 1 functional, 3 error handling, 4 adversarial,
6 persistence and 8 end-to-end.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import date

import pytest

from app.core.db import get_sessionmaker
from app.features.review import report as report_builder
from app.features.review import service as review
from app.models import (
    Anomaly,
    AuditEvent,
    CorrectionMemory,
    ReviewItem,
    User,
    Workspace,
)
from app.models.enums import AnomalySeverity, ReviewStatus
from sqlalchemy import select

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)


@pytest.fixture
async def reviewer():
    """A real user id. Correction memory carries a foreign key to one, and a
    resolution recorded against a user who does not exist is not attributable."""
    from app.core.passwords import hash_password
    from app.core.schema_init import create_schema

    await create_schema()
    async with get_sessionmaker()() as session:
        user = User(
            email=f"reviewer+{uuid.uuid4().hex[:8]}@example.com",
            hashed_password=hash_password("CorrectHorse9!battery"),
            full_name="Queue Reviewer",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()
    return user_id


@pytest.fixture
async def workspace_with_queue():
    """A workspace holding one anomaly-backed review item and one plain one."""
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema

    await create_schema()
    async with get_sessionmaker()() as session:
        workspace = Workspace(
            company_name="F7 Queue",
            reporting_period_start=PERIOD_START,
            reporting_period_end=PERIOD_END,
            base_currency="INR",
            claimed_revenue=10_000_000,
            claimed_arr=10_000_000,
        )
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id

        anomaly = Anomaly(
            workspace_id=workspace_id,
            rule_id="A01_DUPLICATE_PAYMENT",
            title="Possible duplicate payment",
            severity=AnomalySeverity.HIGH,
            related_records=[{"type": "payment", "id": "p1"}],
            observed_value="2 payments of INR 59,000.00",
            baseline_value="one payment per charge within 1 day",
            explanation="Captured twice 24 hours apart.",
            required_check="Check whether one was a retry.",
            caveats=["A legitimate retry looks identical here."],
        )
        session.add(anomaly)
        await session.flush()

        session.add(
            ReviewItem(
                workspace_id=workspace_id,
                category="related_party",
                title="Possible duplicate payment: 2 payments of INR 59,000.00",
                detail="Captured twice 24 hours apart.",
                severity=AnomalySeverity.HIGH,
                anomaly_id=anomaly.id,
                evidence_packet={"rule_id": "A01_DUPLICATE_PAYMENT"},
            )
        )
        session.add(
            ReviewItem(
                workspace_id=workspace_id,
                category="ambiguous_match",
                title="Prevented false merge: Acme ↔ Acme Logistics",
                detail="Conflicting tax identifiers.",
                severity=AnomalySeverity.MEDIUM,
                evidence_packet={},
            )
        )
        await session.commit()

    yield workspace_id
    await dispose_engine()


async def test_queue_summarises_what_is_waiting(workspace_with_queue):
    async with get_sessionmaker()() as session:
        summary = await review.summarise(session, workspace_id=workspace_with_queue)
    assert summary.open == 2
    assert summary.resolved == 0
    assert summary.by_severity["high"] == 1
    assert summary.by_category["ambiguous_match"] == 1
    assert summary.oldest_open_days is not None


async def test_queue_names_the_feature_that_raised_each_item(workspace_with_queue):
    """'Prevented false merge' means something different per engine."""
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
    sources = {i["category"]: i["raised_by"] for i in items}
    assert "Feature 2" in sources["ambiguous_match"]
    assert "Feature 6" in sources["related_party"]


async def test_queue_is_ordered_worst_first(workspace_with_queue):
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
    assert [i["severity"] for i in items] == ["high", "medium"]


async def test_a_decision_without_a_reason_is_refused(workspace_with_queue):
    """§7: an override with no reason is how a figure becomes unauditable."""
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
        for blank in ("", "   ", "\n"):
            with pytest.raises(ValueError, match="reason"):
                await review.resolve(
                    session,
                    workspace_id=workspace_with_queue,
                    item_id=uuid.UUID(items[0]["id"]),
                    decision="approved",
                    reason=blank,
                    user_id=uuid.uuid4(),
                )


async def test_an_unknown_decision_is_refused(workspace_with_queue):
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
        with pytest.raises(ValueError, match="decision must be"):
            await review.resolve(
                session,
                workspace_id=workspace_with_queue,
                item_id=uuid.UUID(items[0]["id"]),
                decision="probably_fine",
                reason="looks alright",
                user_id=uuid.uuid4(),
            )


async def test_a_decision_cannot_reach_another_workspaces_item(workspace_with_queue):
    """A valid item id under the wrong workspace must not resolve (OWASP API1)."""
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
        result = await review.resolve(
            session,
            workspace_id=uuid.uuid4(),
            item_id=uuid.UUID(items[0]["id"]),
            decision="approved",
            reason="not mine to decide",
            user_id=uuid.uuid4(),
        )
    assert result is None


async def test_resolving_writes_the_audit_trail_and_the_memory(
    workspace_with_queue, reviewer
):
    user_id = reviewer
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
        target = next(i for i in items if i["category"] == "ambiguous_match")
        await review.resolve(
            session,
            workspace_id=workspace_with_queue,
            item_id=uuid.UUID(target["id"]),
            decision="approved",
            reason="Different GSTINs and different registered addresses.",
            user_id=user_id,
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_with_queue,
                    AuditEvent.action == "review.resolved",
                )
            )
        ).scalars().all()
        memory = (
            await session.execute(
                select(CorrectionMemory).where(
                    CorrectionMemory.workspace_id == workspace_with_queue
                )
            )
        ).scalars().all()

    assert len(events) == 1
    assert events[0].actor_type == "human"
    assert events[0].before_state["status"] == "open"
    assert events[0].after_state["resolution"] == "approved"
    # The decision is knowledge this workspace should not rediscover next run.
    assert len(memory) == 1
    assert memory[0].correction_type == "match_rule"


async def test_rejecting_an_anomaly_marks_it_a_false_positive(
    workspace_with_queue, reviewer
):
    """The review screen and the anomaly screen must not disagree about one finding."""
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
        target = next(i for i in items if i["anomaly_id"])
        await review.resolve(
            session,
            workspace_id=workspace_with_queue,
            item_id=uuid.UUID(target["id"]),
            decision="rejected",
            reason="The processor's retry metadata shows one voided attempt.",
            user_id=reviewer,
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        anomaly = await session.get(Anomaly, uuid.UUID(target["anomaly_id"]))
        item = await session.get(ReviewItem, uuid.UUID(target["id"]))
    assert anomaly.is_false_positive is True
    assert str(item.status) == ReviewStatus.DISMISSED


async def test_approving_an_anomaly_confirms_rather_than_dismisses(
    workspace_with_queue, reviewer
):
    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
        target = next(i for i in items if i["anomaly_id"])
        await review.resolve(
            session,
            workspace_id=workspace_with_queue,
            item_id=uuid.UUID(target["id"]),
            decision="approved",
            reason="Confirmed: the second capture was never voided.",
            user_id=reviewer,
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        anomaly = await session.get(Anomaly, uuid.UUID(target["anomaly_id"]))
    assert anomaly.is_false_positive is False
    assert str(anomaly.status) == ReviewStatus.RESOLVED


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def test_report_states_the_claim_beside_the_evidence(workspace_with_queue):
    async with get_sessionmaker()() as session:
        filename, body = await report_builder.build_report(
            session, workspace_id=workspace_with_queue
        )
    assert filename.endswith(".html")
    assert "Claimed revenue" in body and "Evidence-supported" in body
    assert "F7 Queue" in body


async def test_report_never_accuses(workspace_with_queue):
    """The wording constraint travels with the file, because the file travels."""
    from app.features.anomaly.explain import FORBIDDEN

    async with get_sessionmaker()() as session:
        _, body = await report_builder.build_report(
            session, workspace_id=workspace_with_queue
        )
    assert FORBIDDEN.search(body) is None
    assert "indicator requiring review" in body
    assert "does not give investment advice" in body


async def test_report_escapes_evidence_it_did_not_write():
    """Evidence text reaches the report from a provider; it must not become markup."""
    from app.features.review.report import _esc

    assert _esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert _esc('Acme "quoted" & co') == "Acme &quot;quoted&quot; &amp; co"


async def test_report_refuses_an_unknown_workspace():
    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError, match="workspace not found"):
            await report_builder.build_report(session, workspace_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# The downloadable dataset
# ---------------------------------------------------------------------------


def _archive(seed: str | None) -> zipfile.ZipFile:
    from app.connectors.synthetic import customers as roster
    from app.connectors.synthetic import transactions as tx
    from app.connectors.synthetic.generator import describe, generate_roster

    customers = generate_roster(seed) if seed else None
    buffer = io.BytesIO()
    with roster.use_roster(customers), zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "dataset.json",
            json.dumps(
                {
                    "cases_planted": describe(list(roster.CUSTOMERS)),
                    "customers": [c.legal_name for c in roster.CUSTOMERS],
                    "invoices": tx.zoho_invoices(),
                },
                default=str,
            ),
        )
        rows = tx.bank_csv_rows()
        archive.writestr(
            "bank_statement.csv",
            "\n".join([",".join(rows[0])] + [",".join(r.values()) for r in rows]),
        )
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


def test_downloadable_dataset_carries_the_statement_and_the_cases():
    with _archive(None) as archive:
        assert {"dataset.json", "bank_statement.csv"} <= set(archive.namelist())
        body = json.loads(archive.read("dataset.json"))
    assert body["cases_planted"]["customers"] == 20
    assert body["invoices"]


def test_a_seeded_download_contains_no_built_in_company():
    from app.connectors.synthetic.customers import template

    built_in = {c.legal_name for c in template()}
    with _archive("download-test") as archive:
        body = json.loads(archive.read("dataset.json"))
        statement = archive.read("bank_statement.csv").decode()
    assert not (built_in & set(body["customers"]))
    for name in ("NSTAR TECH", "GLOBAL PAY SERVICES", "BLUE HARBOR"):
        assert name not in statement.upper()


# ---------------------------------------------------------------------------
# The critic — Feature 7 sub-features 1-3
#
# The critic is the component most able to do quiet damage: it decides what gets
# published. So the tests are mostly about what it is *not* allowed to do — approve
# something arithmetic already failed, promote an item, or draw a conclusion from a
# figure nobody recorded.
# ---------------------------------------------------------------------------

from app.features.review import critic as critic_mod  # noqa: E402
from app.features.review.critic import ItemUnderReview, deterministic_checks  # noqa: E402
from app.models.enums import CriticVerdict, RevenueClass  # noqa: E402


def under_review(**kwargs) -> ItemUnderReview:
    """A clean, verified, fully evidenced item; override what the test is about."""
    base = {
        "item_id": "item-1",
        "description": "INV-001",
        "currency": "INR",
        "classification": str(RevenueClass.VERIFIED_ONE_TIME),
        "recognized_minor": 100_000,
        "gross_minor": 100_000,
        "rule_id": "R03",
        "rule_explanation": "Paid, retained and bank confirmed.",
        "allocated_minor": 100_000,
        "retained_minor": 100_000,
        "bank_confirmed_minor": 100_000,
        "invoice_status": "paid",
    }
    return ItemUnderReview(**{**base, **kwargs})


def test_a_clean_item_raises_no_deterministic_finding():
    assert deterministic_checks(under_review()) == []


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"recognized_minor": 200_000}, "DOUBLE_COUNTED"),
        ({"retained_minor": 0}, "MISSING_PAYMENT_EVIDENCE"),
        ({"refunded_minor": 100_000}, "REFUND_NOT_APPLIED"),
        ({"bank_confirmed_minor": 0}, "MISSING_BANK_CONFIRMATION"),
        ({"customer_resolved": False}, "WEAK_ENTITY_LINK"),
        ({"invoice_status": "void"}, "CONTRADICTORY_CLAUSE"),
        ({"citations_verified": False}, "UNVERIFIED_CITATION"),
        ({"open_anomaly_rules": ["A04_ONE_TIME_AS_ARR"]}, "ANOMALY_UNRESOLVED"),
    ],
)
def test_each_failure_is_caught_by_arithmetic(overrides, expected_code):
    codes = {f.code for f in deterministic_checks(under_review(**overrides))}
    assert expected_code in codes


def test_recognising_more_than_was_retained_is_the_double_count():
    """The check Feature 5's own detector could not make."""
    findings = deterministic_checks(
        under_review(recognized_minor=500_000, retained_minor=100_000)
    )
    detail = next(f.detail for f in findings if f.code == "DOUBLE_COUNTED")
    assert "500000" in detail and "100000" in detail


def test_an_unrecorded_period_is_not_treated_as_zero():
    """"Not recorded" and "none of it falls in the period" are different facts.

    Feature 5 stores a period split only for contract-anchored items. Passing 0 for
    the rest told the critic every invoice fell outside the reporting period, and it
    disputed five sound items on that basis — correct reasoning from a false premise.
    """
    assert deterministic_checks(under_review(in_period_minor=None,
                                             future_period_minor=None)) == []
    block = critic_mod._evidence_block(under_review())
    assert "Value inside the reporting period: not recorded" in block
    # A genuinely measured zero still reads as zero.
    measured = critic_mod._evidence_block(under_review(in_period_minor=0))
    assert "Value inside the reporting period: 0" in measured


def test_a_period_dispute_needs_both_figures_present():
    assert deterministic_checks(
        under_review(future_period_minor=50_000, in_period_minor=0)
    )
    assert not deterministic_checks(
        under_review(future_period_minor=50_000, in_period_minor=None)
    )


def test_recurring_without_a_recurring_contract_is_caught():
    codes = {
        f.code
        for f in deterministic_checks(
            under_review(
                classification=str(RevenueClass.VERIFIED_RECURRING),
                contract_recurring_minor=0,
            )
        )
    }
    assert "ONE_TIME_AS_RECURRING" in codes


def test_an_unverified_item_is_not_checked_for_cash():
    """An item classified as unpaid should not be faulted for having no cash."""
    codes = {
        f.code
        for f in deterministic_checks(
            under_review(
                classification=str(RevenueClass.INVOICED_UNPAID),
                recognized_minor=0,
                retained_minor=0,
                bank_confirmed_minor=0,
            )
        )
    }
    assert "MISSING_PAYMENT_EVIDENCE" not in codes
    assert "MISSING_BANK_CONFIRMATION" not in codes


def test_disputes_route_to_the_feature_that_owns_the_failure():
    assert critic_mod.route_for(["WEAK_ENTITY_LINK"]) == 2
    assert critic_mod.route_for(["UNVERIFIED_CITATION"]) == 3
    assert critic_mod.route_for(["MISSING_BANK_CONFIRMATION"]) == 4
    assert critic_mod.route_for(["DOUBLE_COUNTED"]) == 5
    assert critic_mod.route_for(["ANOMALY_UNRESOLVED"]) == 6
    assert critic_mod.route_for([]) is None
    # Identity first: the cash matching below it means nothing until it is right.
    assert critic_mod.route_for(["MISSING_BANK_CONFIRMATION", "WEAK_ENTITY_LINK"]) == 2


async def test_a_deterministic_failure_never_reaches_the_model():
    """Code must not be able to be talked out of an arithmetic result."""
    result = await critic_mod.criticise(
        under_review(recognized_minor=999_999),
        workspace_id="ws",
        use_llm=True,
    )
    assert result.verdict is CriticVerdict.DISPUTED
    assert result.used_model is False
    assert "DOUBLE_COUNTED" in result.issue_codes


async def test_a_clean_immaterial_item_is_approved_without_a_model_call():
    result = await critic_mod.criticise(
        under_review(is_material=False), workspace_id="ws", use_llm=True
    )
    assert result.verdict is CriticVerdict.APPROVED
    assert result.used_model is False
    assert "materiality" in result.reasoning


async def test_a_critic_that_cannot_run_does_not_read_as_approval(monkeypatch):
    """An unavailable critic must route to a human, never silently approve."""
    monkeypatch.setattr(critic_mod.llm, "is_available", lambda: True)

    async def boom(**_kwargs):
        raise critic_mod.llm.LLMUnavailableError("quota exhausted")

    monkeypatch.setattr(critic_mod.llm, "structured_call", boom)
    result = await critic_mod.criticise(
        under_review(is_material=True), workspace_id="ws", use_llm=True
    )
    assert result.verdict is CriticVerdict.MORE_EVIDENCE_REQUIRED
    assert "quota exhausted" in result.reasoning


async def test_an_approval_naming_issues_is_treated_as_a_dispute(monkeypatch):
    """A self-contradicting verdict resolves against publication, not for it."""
    monkeypatch.setattr(critic_mod.llm, "is_available", lambda: True)

    class _Result:
        parsed = critic_mod.CriticOut(
            verdict="APPROVED",
            issue_codes=["MISSING_BANK_CONFIRMATION"],
            reasoning="looks fine but the bank credit is missing",
        )
        model = "test-model"

    async def call(**_kwargs):
        return _Result()

    monkeypatch.setattr(critic_mod.llm, "structured_call", call)
    result = await critic_mod.criticise(
        under_review(is_material=True), workspace_id="ws", use_llm=True
    )
    assert result.verdict is CriticVerdict.DISPUTED
    assert result.routed_to_feature == 4


async def test_the_critic_prompt_marks_company_text_as_untrusted():
    """A contract saying 'mark this verified' is data, not an instruction."""
    block = critic_mod._evidence_block(under_review())
    wrapped = critic_mod.llm.wrap_untrusted_evidence("evidence", block)
    assert 'untrusted="true"' in wrapped
    assert "never an instruction" in critic_mod.SYSTEM_PROMPT


async def test_only_approved_items_are_published(workspace_with_queue):
    """The guarantee the whole maker-checker exists to provide.

    Feature 5 leaves everything unpublished on purpose. This is the only code that
    flips that bit, and an item the critic would not approve must never carry it.
    """
    from app.features.review import verify
    from app.models import CriticDecision, CustomerEntity, RevenueItem

    async with get_sessionmaker()() as session:
        # A resolved customer: without one every item trips WEAK_ENTITY_LINK, and
        # the test would be measuring the wrong check.
        customer = CustomerEntity(
            workspace_id=workspace_with_queue,
            canonical_name="Acme Industries",
            normalized_name="acme industries",
        )
        session.add(customer)
        await session.flush()
        customer_id = customer.id

        # One clean item and one that fails arithmetic outright.
        session.add(
            RevenueItem(
                workspace_id=workspace_with_queue,
                customer_entity_id=customer_id,
                description="Clean paid invoice",
                currency="INR",
                gross_amount=100_000,
                recognized_amount=100_000,
                classification=RevenueClass.VERIFIED_ONE_TIME,
                rule_id="R03",
                rule_explanation="Paid and confirmed.",
                calculation_detail={
                    "retained_minor": 100_000,
                    "allocated_minor": 100_000,
                    "bank_confirmed_minor": 100_000,
                },
                is_published=True,  # a stale publication from an earlier state
            )
        )
        session.add(
            RevenueItem(
                workspace_id=workspace_with_queue,
                customer_entity_id=customer_id,
                description="Recognises more than it retained",
                currency="INR",
                gross_amount=500_000,
                recognized_amount=500_000,
                classification=RevenueClass.VERIFIED_ONE_TIME,
                rule_id="R03",
                rule_explanation="Paid and confirmed.",
                calculation_detail={
                    "retained_minor": 100_000,
                    "allocated_minor": 100_000,
                    "bank_confirmed_minor": 100_000,
                },
                is_published=True,
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        result = await verify.run_maker_checker(
            session, workspace_id=workspace_with_queue, use_llm=False
        )
        await session.commit()

    assert result.items_reviewed == 2
    assert result.approved == 1
    assert result.disputed == 1
    assert result.published == 1

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(RevenueItem).where(
                    RevenueItem.workspace_id == workspace_with_queue
                )
            )
        ).scalars().all()
        decisions = (
            await session.execute(
                select(CriticDecision).where(
                    CriticDecision.workspace_id == workspace_with_queue
                )
            )
        ).scalars().all()

    published = {r.description: r.is_published for r in rows}
    assert published["Clean paid invoice"] is True
    # The stale publication is withdrawn, which is the point: a figure that no
    # longer survives review must stop being presented as a result.
    assert published["Recognises more than it retained"] is False
    assert len(decisions) == 2
    assert {str(d.verdict) for d in decisions} == {"APPROVED", "DISPUTED"}


async def test_a_dispute_reaches_the_review_queue_with_its_packet(workspace_with_queue):
    from app.features.review import verify
    from app.models import CustomerEntity, RevenueItem

    async with get_sessionmaker()() as session:
        customer = CustomerEntity(
            workspace_id=workspace_with_queue,
            canonical_name="Beacon Systems",
            normalized_name="beacon systems",
        )
        session.add(customer)
        await session.flush()
        session.add(
            RevenueItem(
                workspace_id=workspace_with_queue,
                customer_entity_id=customer.id,
                description="Unconfirmed receipt",
                currency="INR",
                gross_amount=250_000,
                recognized_amount=250_000,
                classification=RevenueClass.VERIFIED_ONE_TIME,
                rule_id="R03",
                rule_explanation="Paid.",
                calculation_detail={"retained_minor": 250_000},
                is_material=True,
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        await verify.run_maker_checker(
            session, workspace_id=workspace_with_queue, use_llm=False
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        items = await review.list_items(session, workspace_id=workspace_with_queue)
    queued = next(i for i in items if "Unconfirmed receipt" in i["title"])
    packet = queued["evidence_packet"]
    assert packet["verdict"] == "DISPUTED"
    assert "MISSING_BANK_CONFIRMATION" in packet["issue_codes"]
    assert packet["routed_to_feature"] == 4
    assert packet["settled_by"] == "deterministic checks"


# ---------------------------------------------------------------------------
# Download *contents*
#
# A download that returns 200 and a plausible byte count can still be wrong. These
# assert the things a reader would actually check: that the statement's running
# balance follows its own movements, that a generated dataset mentions none of the
# built-in companies, and that absence is stated as absence rather than as "".
# ---------------------------------------------------------------------------

import csv as _csv  # noqa: E402
from decimal import Decimal  # noqa: E402


def _dataset(seed: str | None):
    from app.connectors.synthetic import customers as roster
    from app.connectors.synthetic import transactions as tx
    from app.connectors.synthetic.generator import generate_roster
    from app.connectors.synthetic.transactions import _translate

    customers = generate_roster(seed) if seed else None
    with roster.use_roster(customers):
        active = list(roster.CUSTOMERS)
        return (
            [
                {
                    "legal_name": c.legal_name,
                    "accounting_name": c.zoho_name or None,
                    "bank_narration_name": c.bank_narration_name,
                    "notes": _translate(c.notes),
                }
                for c in active
            ],
            tx.razorpay_payments(),
            tx.bank_csv_rows(),
        )


def test_the_statement_balance_follows_its_own_movements():
    """A balance column that does not reconcile is a table pretending to be a bank."""
    _, _, rows = _dataset(None)
    assert len(rows) == 62

    reader = list(_csv.DictReader(io.StringIO(
        "\n".join([",".join(rows[0])] + [",".join(r.values()) for r in rows])
    )))
    running = Decimal(reader[0]["Balance"]) - (
        Decimal(reader[0]["Credit"] or 0) - Decimal(reader[0]["Debit"] or 0)
    )
    for index, row in enumerate(reader):
        running += Decimal(row["Credit"] or 0) - Decimal(row["Debit"] or 0)
        assert running == Decimal(row["Balance"]), f"balance drifts at row {index + 1}"


def test_no_statement_row_is_both_a_debit_and_a_credit():
    _, _, rows = _dataset(None)
    for row in rows:
        assert not (row["Debit"] and row["Credit"])
        assert row["Debit"] or row["Credit"]


def test_a_generated_dataset_mentions_no_built_in_company():
    """Not only in the narrations — in the case notes and payment descriptions too.

    Free text names a company by whatever part of its name the writer used: a
    payment description said "Apex Founder Holdings - advance" and a case note said
    "Distinct from Blue Harbour Logistics". Matching only the four exact spellings
    left both behind, so a generated dataset still talked about Apex and Northstar.
    """
    customers, payments, rows = _dataset("leak-test")
    blob = " ".join(
        [json.dumps(customers), json.dumps(payments, default=str)]
        + [r["Description"] for r in rows]
    ).upper()
    for name in ("NORTHSTAR", "NSTAR TECH", "BLUE HARBOR", "BLUE HARBOUR",
                 "APEX FOUNDER", "GLOBAL PAY", "QUANTUM RETAIL", "MERIDIAN"):
        assert name not in blob, f"built-in company {name!r} leaked into generated data"


def test_an_absent_spelling_is_null_rather_than_blank():
    """The unexplained-cash customer has no accounting record. Say so."""
    customers, _, _ = _dataset(None)
    assert any(c["accounting_name"] is None for c in customers), (
        "the customer with no accounting record should report null"
    )
    assert not any(c["accounting_name"] == "" for c in customers)
    assert all(c["legal_name"] and c["bank_narration_name"] for c in customers)


async def test_the_report_download_is_self_contained(workspace_with_queue):
    """It has to survive being emailed to someone who will never log in."""
    async with get_sessionmaker()() as session:
        _, body = await report_builder.build_report(
            session, workspace_id=workspace_with_queue
        )
    assert body.startswith("<!doctype html>")
    assert body.rstrip().endswith("</html>")
    assert "<script" not in body
    # No external asset can be fetched: a report that renders differently offline is
    # not the same document a reviewer was shown.
    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "https://" not in body
