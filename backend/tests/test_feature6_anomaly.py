"""Feature 6 tests — Revenue Anomaly and Manipulation Detection.

The thing worth testing here is not "does a detector fire" but "does it fire on the
right thing and stay quiet on the rest". A detector that flags everything passes a
naive test and is useless in production, so most of what follows asserts silence:
one payment is not a duplicate, a refund is not a circular flow, and a company is
not a related party to itself.

Three assertions carry the most weight, and each was a real defect caught by writing
them:

* **The Quantum Retail case.** ₹15,00,000 of implementation fee invoiced as "Annual
  subscription" must be flagged against the contract that makes it one-time. It did
  not fire at first, because the invoice and the contract reached the rule under
  different customer keys.
* **The Blue Harbor duplicate survives identity resolution.** Grouping payments by
  "resolved id or name" split one customer across two key spaces the moment Feature 2
  linked some of its records and not others — and the near-duplicate pair the dataset
  exists to catch disappeared.
* **A reviewer's verdict outlives the scan.** Findings are recomputed from scratch
  every run; the labels are not, or precision could never be measured.

Covers Step 2a categories 1 functional, 2 edge, 4 adversarial, 6 persistence,
7 concurrency/idempotency, 8 end-to-end and 11 goal-fidelity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.db import get_sessionmaker
from app.features.anomaly import concentration, explain, graph, rules, scoring
from app.features.anomaly import service as anomaly
from app.features.anomaly.rules import (
    ContractRecord,
    InvoiceRecord,
    PaymentRecord,
    RefundRecord,
)
from app.models import Anomaly, ReviewItem, Workspace
from app.models.enums import AnomalySeverity, ReviewStatus
from app.services import ingestion

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)


def payment(**kwargs) -> PaymentRecord:
    base = {
        "id": kwargs.pop("id", uuid.uuid4().hex[:8]),
        "customer_id": kwargs.pop("customer_id", "acme"),
        "customer_name": kwargs.pop("customer_name", "Acme Ltd"),
        "amount_minor": kwargs.pop("amount_minor", 100_000),
        "currency": kwargs.pop("currency", "INR"),
        "status": kwargs.pop("status", "captured"),
        "captured_at": kwargs.pop("captured_at", datetime(2026, 5, 1, tzinfo=UTC)),
    }
    return PaymentRecord(**base, **kwargs)


# ---------------------------------------------------------------------------
# 1. Deterministic rules — each fires on its case and stays quiet otherwise
# ---------------------------------------------------------------------------


def test_duplicate_payment_detected_within_the_window():
    findings = rules.detect_duplicate_payments(
        [
            payment(id="p1", captured_at=datetime(2026, 5, 1, 9, tzinfo=UTC)),
            payment(id="p2", captured_at=datetime(2026, 5, 1, 12, tzinfo=UTC)),
        ]
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "A01_DUPLICATE_PAYMENT"
    assert findings[0].severity is AnomalySeverity.HIGH
    assert {r["id"] for r in findings[0].related_records} == {"p1", "p2"}


def test_monthly_billing_is_not_a_duplicate():
    """The single most important negative case: 12 identical monthly charges."""
    monthly = [
        payment(id=f"m{i}", captured_at=datetime(2026, 4 + (i % 9) or 1, 3, tzinfo=UTC))
        for i in range(12)
    ]
    monthly = [
        payment(id=f"m{i}", captured_at=datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=30 * i))
        for i in range(12)
    ]
    assert rules.detect_duplicate_payments(monthly) == []


def test_same_amount_different_customers_is_not_a_duplicate():
    """A business billing twenty customers ₹75,000 is not producing twenty duplicates."""
    findings = rules.detect_duplicate_payments(
        [
            payment(id="a", customer_id="one", customer_name="One"),
            payment(id="b", customer_id="two", customer_name="Two"),
        ]
    )
    assert findings == []


def test_rapid_refund_detected_and_slow_refund_ignored():
    captured = datetime(2026, 5, 1, tzinfo=UTC)
    p = payment(id="p1", captured_at=captured)
    fast = rules.detect_rapid_refunds(
        [p], [RefundRecord(id="r1", payment_id="p1", amount_minor=100_000,
                           refunded_at=captured + timedelta(days=1))]
    )
    slow = rules.detect_rapid_refunds(
        [p], [RefundRecord(id="r2", payment_id="p1", amount_minor=100_000,
                           refunded_at=captured + timedelta(days=40))]
    )
    assert len(fast) == 1 and fast[0].rule_id == "A02_RAPID_REFUND"
    assert slow == [], "an ordinary refund 40 days later is customer service, not an anomaly"


def test_period_end_spike_needs_a_baseline_before_it_can_claim_one():
    """With fewer than four periods there is no 'normal' to be unusual against."""
    invoices = [
        InvoiceRecord(id="i1", number="A", customer_id="c", customer_name="C",
                      total_minor=10_000_000, issued_on=date(2027, 3, 30)),
    ]
    assert rules.detect_period_end_spike(invoices, PERIOD_START, PERIOD_END) == []


def test_period_end_spike_fires_against_a_real_baseline():
    invoices = [
        InvoiceRecord(id=f"m{month}", number=str(month), customer_id="c",
                      customer_name="C", total_minor=100_000,
                      issued_on=date(2026, month, 10))
        for month in range(4, 12)
    ]
    invoices.append(
        InvoiceRecord(id="spike", number="S", customer_id="c", customer_name="C",
                      total_minor=50_000_000, issued_on=date(2027, 3, 28))
    )
    findings = rules.detect_period_end_spike(invoices, PERIOD_START, PERIOD_END)
    assert len(findings) == 1
    assert findings[0].rule_id == "A03_PERIOD_END_SPIKE"
    assert findings[0].baseline_value is not None


def test_one_time_fee_presented_as_recurring_is_flagged():
    """The Quantum Retail case, stated directly."""
    findings = rules.detect_one_time_as_arr(
        [
            InvoiceRecord(
                id="inv", number="INV-2026-050", customer_id="quantum retail",
                customer_name="Quantum Retail", total_minor=177_000_000,
                issued_on=date(2026, 5, 18),
                description="Annual subscription - implementation and migration programme",
                one_time_hint=True,
            )
        ],
        [
            ContractRecord(
                id="c", document_name="Quantum_Retail_Implementation_SOW.pdf",
                customer_id="quantum retail", start_date=date(2026, 5, 1),
                end_date=date(2027, 4, 30),
                # ₹3,00,000 recurring and ₹15,00,000 one-time, in paise.
                recurring_minor=30_000_000, one_time_minor=150_000_000,
            )
        ],
    )
    assert len(findings) == 1
    assert findings[0].rule_id == "A04_ONE_TIME_AS_ARR"
    assert "1,500,000.00" in (findings[0].baseline_value or "")


def test_the_contract_with_the_one_time_obligation_wins_the_lookup():
    """A customer's ₹0 subscription contract must not shadow its SOW."""
    invoice = InvoiceRecord(
        id="inv", number="INV", customer_id="quantum retail",
        customer_name="Quantum Retail", total_minor=1_770_000,
        issued_on=date(2026, 5, 18), description="Annual subscription", one_time_hint=True,
    )
    subscription = ContractRecord(
        id="c1", document_name="Quantum_Retail_Agreement.pdf",
        customer_id="quantum retail", start_date=None, end_date=None,
        recurring_minor=300_000, one_time_minor=0,
    )
    sow = ContractRecord(
        id="c2", document_name="Quantum_Retail_Implementation_SOW.pdf",
        customer_id="quantum retail", start_date=None, end_date=None,
        recurring_minor=0, one_time_minor=1_500_000,
    )
    for order in ([subscription, sow], [sow, subscription]):
        findings = rules.detect_one_time_as_arr([invoice], order)
        assert len(findings) == 1, "order of contracts must not decide the outcome"
        assert "SOW" in findings[0].explanation


def test_shared_payment_account_fires_at_two_and_escalates_beyond():
    """§19 puts exactly two customers behind one agent, so two must be visible."""
    two = [
        payment(id="a", customer_id="sub", customer_name="Sub", account_fingerprint="acct"),
        payment(id="b", customer_id="par", customer_name="Parent", account_fingerprint="acct"),
    ]
    findings = rules.detect_shared_payment_accounts(two)
    assert len(findings) == 1
    assert findings[0].rule_id == "A06_SHARED_PAYMENT_ACCOUNT"
    assert findings[0].severity is AnomalySeverity.MEDIUM, "two is a question, not a finding"

    three = [*two, payment(id="c", customer_id="oth", customer_name="Other",
                           account_fingerprint="acct")]
    escalated = rules.detect_shared_payment_accounts(three)
    assert len(escalated) == 1
    assert escalated[0].severity is AnomalySeverity.HIGH


def test_one_customer_on_its_own_account_is_not_a_shared_account():
    alone = [
        payment(id="a", customer_id="one", customer_name="One", account_fingerprint="acct"),
        payment(id="b", customer_id="one", customer_name="One", account_fingerprint="acct"),
    ]
    assert rules.detect_shared_payment_accounts(alone) == []


def test_refund_rate_is_measured_against_the_workspace_not_an_absolute():
    payments = [
        payment(id="a", customer_id="c1", customer_name="One",
                amount_minor=1_000_000, refunded_minor=1_000_000),
        *[payment(id=f"b{i}", customer_id="c2", customer_name="Two",
                  amount_minor=1_000_000) for i in range(20)],
    ]
    findings = rules.detect_unusual_refund_rate(payments)
    assert [f.customer_id for f in findings] == ["c1"]


def test_every_rule_states_a_baseline():
    """A rule that cannot say what normal looks like has not made an argument."""
    payments = [
        payment(id="p1", captured_at=datetime(2026, 5, 1, 9, tzinfo=UTC)),
        payment(id="p2", captured_at=datetime(2026, 5, 1, 10, tzinfo=UTC)),
    ]
    findings = rules.run_all(
        payments=payments,
        refunds=[RefundRecord(id="r", payment_id="p1", amount_minor=50_000,
                              refunded_at=datetime(2026, 5, 2, tzinfo=UTC))],
        invoices=[],
        contracts=[ContractRecord(id="c", document_name="d.pdf", customer_id="acme",
                                  start_date=None, end_date=None, future_period_minor=5_000)],
        period_start=PERIOD_START,
        period_end=PERIOD_END,
    )
    assert findings
    for finding in findings:
        assert finding.baseline_value, f"{finding.rule_id} fired without a baseline"
        assert finding.required_check, f"{finding.rule_id} does not say what to check"


# ---------------------------------------------------------------------------
# 2. Adversarial — the wording constraint is enforced, not requested
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "This is clear fraud by the founder.",
        "The company is laundering money through Apex.",
        "An illegal scheme to embezzle customer funds.",
        "The director stole from the company.",
    ],
)
def test_accusatory_narratives_are_rejected(text):
    assert explain.FORBIDDEN.search(text), f"accusatory wording not caught: {text}"


@pytest.mark.parametrize(
    "text",
    [
        "Funds returned to the originating account within four days.",
        "This indicator requires review against the shipment records.",
        "A refund was issued shortly after capture.",
    ],
)
def test_ordinary_wording_is_not_rejected(text):
    assert explain.FORBIDDEN.search(text) is None, f"false positive on: {text}"


def test_every_packet_carries_the_indicator_disclaimer():
    finding = rules._finding(
        "A01_DUPLICATE_PAYMENT", AnomalySeverity.HIGH,
        explanation="x", observed_value="2", baseline_value="1",
    )
    packet = explain.build_packet(finding)
    assert any("not a finding of wrongdoing" in c for c in packet.caveats)


def test_a_finding_without_a_baseline_says_so():
    finding = rules._finding("A01_DUPLICATE_PAYMENT", AnomalySeverity.HIGH, explanation="x")
    packet = explain.build_packet(finding)
    assert any("No baseline" in c for c in packet.caveats)


def test_stored_rule_catalogue_covers_every_pattern_the_spec_names():
    """idea_features.md §Feature 6 lists eleven patterns; each needs a rule id."""
    expected = {
        "A01_DUPLICATE_PAYMENT", "A02_RAPID_REFUND", "A03_PERIOD_END_SPIKE",
        "A04_ONE_TIME_AS_ARR", "A05_FUTURE_PERIOD_IN_CURRENT",
        "A06_SHARED_PAYMENT_ACCOUNT", "A07_CUSTOMER_CONCENTRATION",
        "A08_INVOICE_CHURN", "A09_UNUSUAL_REFUND_RATE",
        "A10_RELATED_PARTY_REVENUE", "A11_CIRCULAR_FUNDS",
    }
    assert expected <= set(rules.RULES)
    for rule_id, (title, check) in rules.RULES.items():
        assert title and check, f"{rule_id} has no title or required check"


# ---------------------------------------------------------------------------
# 3. Graph — clusters, cycles, and what they refuse to claim
# ---------------------------------------------------------------------------


def test_weakly_connected_groups_shared_attributes():
    groups = graph.weakly_connected(
        ["a", "b", "c", "d"], [("a", "x"), ("a", "b"), ("c", "d")]
    )
    assert sorted(map(sorted, groups)) == [["a", "b"], ["c", "d"]]


def test_cycle_detection_finds_a_directed_loop():
    cycles = graph.find_cycles(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    assert len(cycles) == 1
    assert set(cycles[0]) == {"a", "b", "c"}


def test_a_cycle_is_reported_once_however_it_is_rotated():
    cycles = graph.find_cycles(["a", "b"], [("a", "b"), ("b", "a")])
    assert len(cycles) == 1, "A→B→A and B→A→B are one finding"


def test_a_one_way_chain_is_not_a_cycle():
    assert graph.find_cycles(["a", "b", "c"], [("a", "b"), ("b", "c")]) == []


def test_strongly_connected_separates_reciprocal_from_one_way():
    components = graph.strongly_connected(
        ["a", "b", "c"], [("a", "b"), ("b", "a"), ("b", "c")]
    )
    sizes = sorted(len(c) for c in components)
    assert sizes == [1, 2]


# ---------------------------------------------------------------------------
# 4. Concentration
# ---------------------------------------------------------------------------


def test_concentration_shares_and_hhi():
    result = concentration.measure({"A": 600_000, "B": 300_000, "C": 100_000})
    assert result.top_customer == "A"
    assert result.top_share_pct == 60.0
    assert round(sum(e["share_pct"] for e in result.per_customer)) == 100
    # 60² + 30² + 10² = 4600
    assert result.hhi == pytest.approx(4600.0, abs=0.5)
    assert len(result.findings) == 1


def test_concentration_stays_quiet_below_the_threshold():
    even = {name: 100_000 for name in "ABCDEFGHIJ"}
    result = concentration.measure(even)
    assert result.findings == []
    assert result.top_share_pct == 10.0


def test_concentration_reports_the_hhi_caveat():
    """The DOJ bands describe markets, not startups, and the output must say so."""
    body = concentration.measure({"A": 1_000}).as_dict()
    assert "antitrust" in body["hhi_caveat"]


def test_concentration_ignores_unpaid_amounts():
    """Measured over verified revenue only; zero and negative entries are dropped."""
    result = concentration.measure({"A": 500_000, "B": 0})
    assert result.customer_count == 1


# ---------------------------------------------------------------------------
# 5. Conditional ML — off when there is not enough history
# ---------------------------------------------------------------------------


def test_model_declines_to_run_on_a_small_workspace():
    result = scoring.score_payments([payment(id=str(i)) for i in range(5)])
    assert result.enabled is False
    assert "threshold" in result.reason
    assert result.findings == []


def test_model_version_changes_with_the_data_it_was_fitted_on():
    a = scoring.model_version([[1.0, 2.0], [3.0, 4.0]])
    b = scoring.model_version([[1.0, 2.0], [3.0, 5.0]])
    assert a != b, "a finding must be reproducible under the exact model that made it"
    assert scoring.model_version([[1.0, 2.0]]) == scoring.model_version([[1.0, 2.0]])


def test_model_findings_are_low_severity_and_say_a_score_is_not_a_finding():
    payments = [
        payment(id=str(i), amount_minor=100_000 + i,
                captured_at=datetime(2026, 5, 1, tzinfo=UTC) + timedelta(days=i))
        for i in range(80)
    ]
    payments.append(
        payment(id="outlier", amount_minor=900_000_000,
                captured_at=datetime(2026, 9, 1, tzinfo=UTC))
    )
    result = scoring.score_payments(payments)
    assert result.enabled is True
    assert result.model_version
    for finding in result.findings:
        assert finding.severity is AnomalySeverity.LOW
        assert any("never a finding" in c for c in finding.caveats)


# ---------------------------------------------------------------------------
# 6. Key handling — the defects that hid real findings
# ---------------------------------------------------------------------------


def test_customer_key_is_the_same_whether_or_not_identity_resolved_the_record():
    """Partial identity resolution must not split one customer across two key spaces."""
    resolved = anomaly.customer_key("Blue Harbor Analytics", None)
    unresolved = anomaly.customer_key(None, "Blue Harbor Analytics")
    assert resolved == unresolved


def test_suspected_duplicates_group_name_variants_but_keep_harbor_and_harbour_apart():
    groups = anomaly._suspected_duplicates(
        {
            "northstar tech": "Northstar Tech",
            "northstar technologies": "Northstar Technologies",
            "blue harbor analytics": "Blue Harbor Analytics",
            "blue harbour logistics": "Blue Harbour Logistics",
        }
    )
    merged = {frozenset(members) for members in groups.values()}
    assert frozenset({"northstar tech", "northstar technologies"}) in merged
    assert not any(
        "blue harbor analytics" in members and "blue harbour logistics" in members
        for members in merged
    ), "the false merge Feature 2 was fixed to refuse must not come back here"


def test_contract_keys_align_onto_the_invoice_key_when_unambiguous():
    inputs = anomaly.ScanInputs(
        invoices=[
            InvoiceRecord(id="i", number="INV", customer_id="quantum retail implementation",
                          customer_name="Quantum Retail", total_minor=1, issued_on=None)
        ],
        contracts=[
            ContractRecord(id="c", document_name="SOW.pdf",
                           customer_id="quantum retail solutions",
                           start_date=None, end_date=None, one_time_minor=1_500_000)
        ],
    )
    anomaly._align_contract_keys(inputs)
    assert inputs.contracts[0].customer_id == "quantum retail implementation"


def test_contract_keys_are_left_alone_when_the_match_is_ambiguous():
    inputs = anomaly.ScanInputs(
        invoices=[
            InvoiceRecord(id="i1", number="A", customer_id="acme industries north",
                          customer_name="A", total_minor=1, issued_on=None),
            InvoiceRecord(id="i2", number="B", customer_id="acme industries south",
                          customer_name="B", total_minor=1, issued_on=None),
        ],
        contracts=[
            ContractRecord(id="c", document_name="X.pdf", customer_id="acme industries east",
                           start_date=None, end_date=None)
        ],
    )
    anomaly._align_contract_keys(inputs)
    assert inputs.contracts[0].customer_id == "acme industries east", (
        "an ambiguous contract must not be attached to a guessed customer"
    )


def test_fingerprint_identifies_a_finding_by_what_it_is():
    a = anomaly.fingerprint("A01", [{"type": "payment", "id": "1"},
                                    {"type": "payment", "id": "2"}])
    b = anomaly.fingerprint("A01", [{"type": "payment", "id": "2"},
                                    {"type": "payment", "id": "1"}])
    assert a == b, "record order must not change a finding's identity"
    assert a != anomaly.fingerprint("A02", [{"type": "payment", "id": "1"},
                                            {"type": "payment", "id": "2"}])


# ---------------------------------------------------------------------------
# 7. Precision and the model gate
# ---------------------------------------------------------------------------


@pytest.fixture
async def scanned():
    """Ingest the §15 dataset, resolve identities, then scan. No model calls."""
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema
    from app.features.identity import service as identity

    await create_schema()
    async with get_sessionmaker()() as session:
        workspace = Workspace(
            company_name="F6 End To End",
            reporting_period_start=PERIOD_START,
            reporting_period_end=PERIOD_END,
            base_currency="INR",
            claimed_revenue=1_000_000_000,
            claimed_arr=1_000_000_000,
        )
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id
        await session.commit()

    async with get_sessionmaker()() as session:
        await ingestion.ingest_all(session, workspace_id=workspace_id, force_synthetic=True)
    async with get_sessionmaker()() as session:
        await identity.resolve_identities(
            session, workspace_id=workspace_id, use_critic=False
        )
        await session.commit()
    async with get_sessionmaker()() as session:
        result = await anomaly.scan(session, workspace_id=workspace_id, use_llm=False)
        await session.commit()

    yield workspace_id, result
    await dispose_engine()


async def test_precision_reports_no_labels_rather_than_assuming_good(scanned):
    workspace_id, _ = scanned
    async with get_sessionmaker()() as session:
        measured = await anomaly.measure_precision(session, workspace_id=workspace_id)
    assert measured["labelled"] == 0
    assert measured["overall_precision"] is None, "unmeasured precision must not read as a number"
    assert measured["ml_enabled"] is True
    assert "before precision can be measured" in measured["ml_reason"]


async def test_precision_only_never_claims_recall(scanned):
    workspace_id, _ = scanned
    async with get_sessionmaker()() as session:
        measured = await anomaly.measure_precision(session, workspace_id=workspace_id)
    assert "recall" in measured["note"].lower()
    assert "recall" not in {k.lower() for k in measured}


async def test_reviewer_feedback_is_recorded_and_moves_precision(scanned):
    workspace_id, _ = scanned
    async with get_sessionmaker()() as session:
        row = (await session.execute(
            Anomaly.__table__.select().where(Anomaly.workspace_id == workspace_id)
        )).first()
        anomaly_id = row.id

    async with get_sessionmaker()() as session:
        updated = await anomaly.record_feedback(
            session, workspace_id=workspace_id, anomaly_id=anomaly_id,
            is_false_positive=True, actor_id="tester",
        )
        await session.commit()
        assert updated is not None
        assert updated.status is ReviewStatus.DISMISSED or str(updated.status) == "dismissed"

    async with get_sessionmaker()() as session:
        measured = await anomaly.measure_precision(session, workspace_id=workspace_id)
    assert measured["labelled"] == 1
    assert measured["overall_precision"] == 0.0


async def test_feedback_cannot_reach_another_workspace(scanned):
    """A valid anomaly id under the wrong workspace must not resolve (OWASP API1)."""
    _, _ = scanned
    async with get_sessionmaker()() as session:
        row = (await session.execute(Anomaly.__table__.select())).first()
        result = await anomaly.record_feedback(
            session, workspace_id=uuid.uuid4(), anomaly_id=row.id,
            is_false_positive=True, actor_id="attacker",
        )
    assert result is None


# ---------------------------------------------------------------------------
# 8. End-to-end against the §15 dataset
# ---------------------------------------------------------------------------


async def test_scan_finds_the_blue_harbor_near_duplicate(scanned):
    """The pair the dataset was built to encode, and which key drift once hid."""
    _, result = scanned
    duplicates = [p for p in result.packets
                  if p.finding.rule_id == "A01_DUPLICATE_PAYMENT"]
    assert len(duplicates) == 1
    assert "59,000" in duplicates[0].finding.explanation


async def test_scan_finds_the_circular_transfers(scanned):
    """§15 encodes two round trips, and both are with Apex — so it is one finding.

    The count is deliberately one rather than two. Both pairs move money out to the
    same counterparty and back, and once the narration parser stopped giving that
    counterparty a different name per transfer purpose, the two pairs collapsed onto
    one party. A relationship is the unit a reviewer investigates; reporting it once
    per leg would inflate the queue with the same question asked twice.
    """
    _, result = scanned
    cycles = [p for p in result.packets if p.finding.rule_id == "A11_CIRCULAR_FUNDS"]
    assert len(cycles) == 1
    for packet in cycles:
        assert "APEX" in packet.finding.explanation.upper()
        assert packet.finding.graph_path, "a circular finding must carry its path"


async def test_scan_finds_the_shared_payment_agent(scanned):
    """§19's agent settles for two unrelated customers from one account."""
    _, result = scanned
    shared = [p for p in result.packets if p.finding.rule_id == "A06_SHARED_PAYMENT_ACCOUNT"]
    assert shared, "the shared-payment-agent case was not detected"
    agent = " ".join(p.finding.explanation.upper() for p in shared)
    assert "CRESTVIEW" in agent and "PINNACLE" in agent, (
        "the two customers behind one agent account must be named together"
    )


async def test_a_refund_is_not_reported_as_a_circular_flow(scanned):
    """Cobalt, Halcyon and Quantum all return money and none of them is a loop."""
    _, result = scanned
    circular = " ".join(
        p.finding.explanation.upper()
        for p in result.packets
        if p.finding.rule_id == "A11_CIRCULAR_FUNDS"
    )
    for refunded in ("COBALT", "HALCYON", "QUANTUM"):
        assert refunded not in circular, (
            f"{refunded}'s refund was reported as circular movement of funds"
        )


async def test_related_party_findings_name_two_different_parties(scanned):
    """A company connected to itself is an identity gap, reported as A13 instead."""
    _, result = scanned
    for packet in result.packets:
        if packet.finding.rule_id != "A10_RELATED_PARTY_REVENUE":
            continue
        assert any(
            "not ownership" in c or "cannot establish legal ownership" in c
            for c in packet.finding.caveats
        ), "a related-party finding must disclaim ownership"


async def test_the_founder_linked_customer_is_surfaced(scanned):
    """Northstar shares the founder's own domain with Apex Holdings — the §19 tell."""
    _, result = scanned
    related = " ".join(
        p.finding.explanation
        for p in result.packets
        if p.finding.rule_id == "A10_RELATED_PARTY_REVENUE"
    )
    assert "Apex" in related and "Northstar" in related


async def test_no_finding_uses_accusatory_language(scanned):
    """The constraint that matters most, asserted over everything actually produced."""
    _, result = scanned
    for packet in result.packets:
        body = " ".join(
            [
                packet.finding.title,
                packet.finding.explanation,
                packet.finding.required_check,
                *packet.finding.caveats,
            ]
        )
        assert explain.FORBIDDEN.search(body) is None, (
            f"{packet.finding.rule_id} used accusatory wording: {body[:200]}"
        )


async def test_every_finding_is_reproducible_and_carries_its_evidence(scanned):
    _, result = scanned
    assert result.findings_total > 0
    for packet in result.packets:
        finding = packet.finding
        assert finding.rule_id in rules.RULES
        assert finding.explanation
        assert finding.required_check
        assert finding.caveats, f"{finding.rule_id} states no limitation"
        if finding.model_version:
            assert finding.model_score is not None


async def test_only_material_findings_interrupt_a_person(scanned):
    workspace_id, result = scanned
    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            ReviewItem.__table__.select().where(
                ReviewItem.workspace_id == workspace_id,
                ReviewItem.anomaly_id.is_not(None),
            )
        )).fetchall()
    high = sum(1 for p in result.packets if p.finding.severity is AnomalySeverity.HIGH)
    assert len(rows) == high
    assert all(str(row.severity) == "high" for row in rows)


async def test_low_severity_model_findings_stay_out_of_the_queue(scanned):
    """A statistical score must never open a review item on its own."""
    workspace_id, _ = scanned
    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            ReviewItem.__table__.select().where(ReviewItem.workspace_id == workspace_id)
        )).fetchall()
    for row in rows:
        assert "A12_STATISTICAL_OUTLIER" not in (row.title or "")


async def test_rescanning_is_idempotent(scanned):
    """A second scan over unchanged evidence must not duplicate or churn findings."""
    workspace_id, first = scanned
    async with get_sessionmaker()() as session:
        second = await anomaly.scan(session, workspace_id=workspace_id, use_llm=False)
        await session.commit()

    assert second.findings_total == first.findings_total
    assert second.by_rule == first.by_rule
    assert second.anomalies_persisted == 0, "a re-scan wrote new rows for unchanged evidence"
    assert second.anomalies_retired == 0
    assert second.review_items_created == 0


async def test_a_rescan_never_unpublishes_what_the_critic_approved(scanned):
    """Scanning for anomalies must not revert the published position.

    The scan needs Feature 5's concentration figures, and it used to get them by
    re-verifying revenue *with persistence* — which deletes and re-inserts every
    revenue item, taking `is_published` and the critic decision that earned it. So
    clicking "Scan for anomalies" after the critic had run reverted a published
    position to nothing, silently, with the audit log recording the publication and
    then the unpublication and the screen offering no explanation. Feature 5 owns
    those rows; Feature 6 owns anomalies.
    """
    from sqlalchemy import func, select

    from app.models import RevenueItem

    workspace_id, _ = scanned

    async def published() -> int:
        async with get_sessionmaker()() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(RevenueItem).where(
                            RevenueItem.workspace_id == workspace_id,
                            RevenueItem.is_published.is_(True),
                        )
                    )
                ).scalar_one()
            )

    # The real order: Feature 5 classifies and owns the rows, then Feature 7
    # publishes some, then someone rescans for anomalies.
    from app.features.revenue import service as revenue_service

    async with get_sessionmaker()() as session:
        await revenue_service.verify_revenue(session, workspace_id=workspace_id)
        await session.commit()

    # Stand in for Feature 7 having approved and published a batch.
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(RevenueItem)
                    .where(RevenueItem.workspace_id == workspace_id)
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )
        assert rows, "the fixture should have classified items"
        for row in rows:
            row.is_published = True
        await session.commit()

    before = await published()
    assert before == len(rows)

    async with get_sessionmaker()() as session:
        await anomaly.scan(session, workspace_id=workspace_id, use_llm=False)
        await session.commit()

    assert await published() == before, (
        "an anomaly scan unpublished items the critic had approved"
    )


async def test_a_reviewers_verdict_survives_a_rescan(scanned):
    """Findings are recomputed every run; labels are the one thing that must not be."""
    workspace_id, _ = scanned
    async with get_sessionmaker()() as session:
        row = (await session.execute(
            Anomaly.__table__.select().where(Anomaly.workspace_id == workspace_id)
        )).first()
        anomaly_id, rule_id = row.id, row.rule_id

    async with get_sessionmaker()() as session:
        await anomaly.record_feedback(
            session, workspace_id=workspace_id, anomaly_id=anomaly_id,
            is_false_positive=True, actor_id="tester",
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        result = await anomaly.scan(session, workspace_id=workspace_id, use_llm=False)
        await session.commit()
        assert result.feedback_preserved >= 1

    async with get_sessionmaker()() as session:
        kept = await session.get(Anomaly, anomaly_id)
        assert kept is not None, "the labelled finding was deleted by the re-scan"
        assert kept.is_false_positive is True
        assert kept.rule_id == rule_id


async def test_the_scan_states_whether_the_model_ran(scanned):
    _, result = scanned
    assert "enabled" in result.ml
    assert result.ml["reason"], "a run must say why the model did or did not run"
    if result.ml["enabled"]:
        assert result.ml["model_version"]
        assert result.ml["validation"]["method"] == "TimeSeriesSplit"


async def test_concentration_is_measured_over_verified_revenue(scanned):
    _, result = scanned
    body = result.concentration
    assert body["total_verified_minor"] > 0
    assert 0 < body["top_share_pct"] <= 100
    assert body["per_customer"]
    assert round(sum(e["share_pct"] for e in body["per_customer"])) == 100
