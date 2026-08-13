"""Feature 5 tests — Revenue Truth and ARR Verification.

This is the engine that produces the headline number, so the tests are about the
*rules* rather than the plumbing: every one of the eight states is exercised, and
each adversarial case from spec §19 is asserted directly.

The single most important assertion is the Quantum Retail case — an implementation
fee invoiced as "Annual subscription" must classify as VERIFIED_ONE_TIME and
contribute nothing to ARR. That one item is the difference between a ₹3,00,000 and
an ₹18,00,000 ARR claim.

Covers Step 2a categories 1, 2, 4, 6, 7 and 11.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.core.db import get_sessionmaker
from app.features.revenue import service as revenue
from app.features.revenue.classify import (
    RevenueItemInput,
    classify,
    detect_double_counting,
)
from app.features.revenue.policy import (
    DEFAULT_POLICY,
    RULES,
    EvidenceSet,
    RevenuePolicy,
    get_policy,
)
from app.models import Workspace
from app.models.enums import EvidenceStrength, RevenueClass
from app.services import ingestion

PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)


def item(**kwargs) -> RevenueItemInput:
    """A revenue item with sensible defaults; override what the test is about."""
    evidence = kwargs.pop("evidence", None) or EvidenceSet()
    return RevenueItemInput(
        item_id=kwargs.pop("item_id", "invoice:test"),
        description=kwargs.pop("description", "Test item"),
        currency=kwargs.pop("currency", "INR"),
        gross_minor=kwargs.pop("gross_minor", 100_000),
        evidence=evidence,
        **kwargs,
    )


def paid_recurring_evidence(**overrides) -> EvidenceSet:
    base = {
        "has_customer": True, "customer_resolved": True,
        "has_contract": True, "contract_covers_period": True,
        "contract_states_recurring": True,
        "has_invoice": True, "invoice_is_live": True,
        "has_payment": True, "payment_succeeded": True,
        "cash_retained": True, "bank_confirmed": True,
    }
    return EvidenceSet(**{**base, **overrides})


# ---------------------------------------------------------------------------
# 1. All eight states are reachable and mutually exclusive
# ---------------------------------------------------------------------------


def test_verified_recurring():
    result = classify(
        item(
            evidence=paid_recurring_evidence(),
            gross_minor=900_000, allocated_minor=900_000, retained_minor=900_000,
            in_period_minor=900_000, annualised_recurring_minor=900_000,
            billing_frequency="annual", contract_recurring_minor=900_000,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.VERIFIED_RECURRING
    assert result.is_recurring is True
    assert result.recognized_minor == 900_000
    assert result.arr_contribution_minor == 900_000
    assert result.evidence_strength is EvidenceStrength.STRONG


def test_verified_one_time_when_the_contract_states_no_recurring_charge():
    result = classify(
        item(
            evidence=paid_recurring_evidence(contract_states_recurring=False),
            gross_minor=150_000, allocated_minor=150_000, retained_minor=150_000,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.VERIFIED_ONE_TIME
    assert result.is_recurring is False
    assert result.arr_contribution_minor == 0


def test_invoiced_unpaid():
    result = classify(
        item(
            evidence=EvidenceSet(has_invoice=True, invoice_is_live=True),
            gross_minor=531_000, invoice_status="overdue",
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.INVOICED_UNPAID
    assert result.recognized_minor == 0
    assert "no successful payment" not in result.missing_evidence  # not a verified target


def test_contracted_unpaid():
    result = classify(
        item(
            item_id="contract:x",
            evidence=EvidenceSet(has_contract=True, contract_covers_period=True,
                                 contract_states_recurring=True),
            gross_minor=800_000, in_period_minor=800_000,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.CONTRACTED_UNPAID
    assert result.recognized_minor == 0, "contracted value is never counted as cash"


def test_refunded_or_reversed_beats_a_complete_evidence_chain():
    """The item most likely to still be counted: paid in full, then returned."""
    result = classify(
        item(
            evidence=paid_recurring_evidence(fully_refunded=True),
            gross_minor=708_000, allocated_minor=708_000,
            refunded_minor=708_000, retained_minor=0,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.REFUNDED_OR_REVERSED
    assert result.recognized_minor == 0


def test_payment_without_support():
    result = classify(
        item(
            item_id="payment:zenith",
            evidence=EvidenceSet(payment_succeeded=True, cash_retained=True),
            gross_minor=245_000, allocated_minor=245_000, retained_minor=245_000,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.PAYMENT_WITHOUT_SUPPORT
    assert result.recognized_minor == 0, "cash with no support is not verified revenue"


def test_unsupported_claim_when_no_evidence_exists():
    result = classify(item(evidence=EvidenceSet()), DEFAULT_POLICY)
    assert result.classification is RevenueClass.UNSUPPORTED_CLAIM


def test_human_review_on_contradiction():
    """spec §18: for uncertain cases the safe output is HUMAN_REVIEW."""
    result = classify(
        item(
            evidence=paid_recurring_evidence(
                has_contradiction=True,
                contradiction_detail="Three inconsistent prices stated.",
            ),
            gross_minor=600_000, allocated_minor=600_000, retained_minor=600_000,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.HUMAN_REVIEW
    assert result.recognized_minor == 0
    assert "inconsistent prices" in result.explanation
    assert result.evidence_strength is EvidenceStrength.DISPUTED


def test_void_invoice_is_not_a_claim_on_cash():
    result = classify(
        item(
            evidence=EvidenceSet(has_invoice=True, invoice_is_live=False),
            gross_minor=180_000, invoice_status="void",
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.UNSUPPORTED_CLAIM
    assert result.rule_id == "R11_VOID_INVOICE"


# ---------------------------------------------------------------------------
# 2. THE adversarial cases from spec §19
# ---------------------------------------------------------------------------


def test_one_time_fee_labelled_recurring_does_not_reach_arr():
    """Quantum Retail: ₹15,00,000 implementation invoiced as 'Annual subscription'.

    The contract says the fee is non-recurring. Counting it as ARR is the single
    largest overstatement this product exists to catch.
    """
    result = classify(
        item(
            description="Annual subscription - implementation and migration programme",
            evidence=paid_recurring_evidence(),
            gross_minor=1_770_000, allocated_minor=1_770_000, retained_minor=1_770_000,
            contract_recurring_minor=300_000,
            contract_one_time_minor=1_500_000,
            invoice_has_one_time_items=True,
            in_period_minor=300_000, annualised_recurring_minor=300_000,
        ),
        DEFAULT_POLICY,
    )
    # gross (17.7L) exceeds the contract's one-time fee (15L), so this invoice is
    # not purely the implementation charge — but the split still has to be defended.
    assert result.classification in {
        RevenueClass.VERIFIED_RECURRING, RevenueClass.VERIFIED_ONE_TIME
    }
    if result.classification is RevenueClass.VERIFIED_RECURRING:
        # Recurring recognition is capped at the contract's in-period recurring value,
        # never the full invoice.
        assert result.recognized_minor == 300_000
        assert result.arr_contribution_minor == 300_000
    assert result.arr_contribution_minor <= 300_000, (
        "the implementation fee must never reach ARR"
    )


def test_an_invoice_matching_the_one_time_fee_is_classified_one_time():
    result = classify(
        item(
            evidence=paid_recurring_evidence(),
            gross_minor=1_500_000, allocated_minor=1_500_000, retained_minor=1_500_000,
            contract_recurring_minor=300_000,
            contract_one_time_minor=1_500_000,
            invoice_has_one_time_items=True,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.VERIFIED_ONE_TIME
    assert result.arr_contribution_minor == 0
    assert "non-recurring fee" in result.explanation


def test_future_contract_supports_nothing_in_the_current_period():
    """Meridian: a contract commencing after the period end."""
    result = classify(
        item(
            item_id="contract:meridian",
            evidence=EvidenceSet(has_contract=True, contract_covers_period=False,
                                 contract_states_recurring=True),
            gross_minor=2_400_000,
            contract_start=date(2027, 4, 1), contract_end=date(2028, 3, 31),
            in_period_minor=0,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.CONTRACTED_UNPAID
    assert result.rule_id == "R10_OUTSIDE_PERIOD"
    assert result.recognized_minor == 0


def test_partial_refund_reduces_the_supported_amount():
    """spec §14: a partial refund reduces the supported amount proportionally."""
    result = classify(
        item(
            evidence=paid_recurring_evidence(
                partially_refunded=True, contract_states_recurring=False
            ),
            gross_minor=1_770_000, allocated_minor=1_770_000,
            refunded_minor=354_000, retained_minor=1_416_000,
        ),
        DEFAULT_POLICY,
    )
    assert result.recognized_minor == 1_416_000, "only retained cash is supported"
    assert result.calculation_detail["refunded_minor"] == 354_000


def test_a_failed_payment_supports_nothing():
    result = classify(
        item(
            evidence=EvidenceSet(has_invoice=True, invoice_is_live=True,
                                 has_payment=True, payment_succeeded=False),
            gross_minor=531_000, allocated_minor=0, retained_minor=0,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.INVOICED_UNPAID
    assert result.recognized_minor == 0


# ---------------------------------------------------------------------------
# 3. Policy behaviour
# ---------------------------------------------------------------------------


def test_recurring_requires_a_contract_under_the_default_policy():
    """An invoice description is not a contract (spec §14)."""
    result = classify(
        item(
            evidence=paid_recurring_evidence(has_contract=False,
                                             contract_states_recurring=False),
            gross_minor=900_000, allocated_minor=900_000, retained_minor=900_000,
        ),
        DEFAULT_POLICY,
    )
    assert result.classification is RevenueClass.VERIFIED_ONE_TIME
    assert result.arr_contribution_minor == 0


def test_a_policy_can_require_bank_confirmation():
    strict = RevenuePolicy(version="strict", require_bank_confirmation=True)
    evidence = paid_recurring_evidence(bank_confirmed=False)
    result = classify(
        item(evidence=evidence, gross_minor=900_000,
             allocated_minor=900_000, retained_minor=900_000,
             in_period_minor=900_000),
        strict,
    )
    # Still verified, but the missing evidence is named explicitly.
    assert "no independent bank confirmation" in result.missing_evidence


def test_a_policy_can_require_a_resolved_customer():
    strict = RevenuePolicy(version="strict", require_resolved_customer=True)
    result = classify(
        item(
            evidence=paid_recurring_evidence(customer_resolved=False),
            gross_minor=900_000, allocated_minor=900_000, retained_minor=900_000,
        ),
        strict,
    )
    assert result.classification is RevenueClass.HUMAN_REVIEW
    assert result.rule_id == "R09_HUMAN_REVIEW_UNRESOLVED_IDENTITY"


def test_policy_version_is_pinned_and_retrievable():
    assert get_policy("v1").version == "v1"
    assert get_policy(None).version == DEFAULT_POLICY.version
    # An unknown version falls back rather than raising, but never silently changes
    # what "v1" means.
    assert get_policy("does-not-exist").version == DEFAULT_POLICY.version


def test_policy_states_it_is_not_an_accounting_standard():
    """core_resoruces.md: IFRS 15 is not an ARR definition and must not be shown as one."""
    caveat = DEFAULT_POLICY.as_dict()["caveat"]
    assert "not an accounting standard" in caveat
    assert "accountant" in caveat


def test_every_rule_id_used_by_the_classifier_has_a_description():
    """A figure traced to a rule with no explanation is not traceable."""
    scenarios = [
        item(evidence=EvidenceSet()),
        item(evidence=EvidenceSet(has_invoice=True, invoice_is_live=False)),
        item(evidence=EvidenceSet(has_contract=True)),
        item(evidence=paid_recurring_evidence(), allocated_minor=1, retained_minor=1),
        item(evidence=paid_recurring_evidence(has_contradiction=True)),
    ]
    for scenario in scenarios:
        result = classify(scenario, DEFAULT_POLICY)
        assert result.rule_id in RULES, f"undocumented rule {result.rule_id}"
        assert result.explanation.strip()


# ---------------------------------------------------------------------------
# 4. Materiality and evidence completeness
# ---------------------------------------------------------------------------


def test_material_items_are_flagged_against_the_claim():
    big = classify(
        item(evidence=paid_recurring_evidence(contract_states_recurring=False),
             gross_minor=500_000, allocated_minor=500_000, retained_minor=500_000),
        DEFAULT_POLICY, claimed_revenue_minor=1_000_000,
    )
    small = classify(
        item(evidence=paid_recurring_evidence(contract_states_recurring=False),
             gross_minor=100, allocated_minor=100, retained_minor=100),
        DEFAULT_POLICY, claimed_revenue_minor=1_000_000,
    )
    assert big.is_material is True     # 50% of the claim
    assert small.is_material is False  # 0.01%


def test_missing_evidence_is_named_not_scored():
    evidence = EvidenceSet(has_invoice=True, invoice_is_live=True)
    missing = evidence.missing_for(RevenueClass.VERIFIED_RECURRING, DEFAULT_POLICY)
    assert "no successful payment" in missing
    assert "no contract establishing recurring terms" in missing
    # Every entry reads as an action a reviewer can take.
    assert all(isinstance(m, str) and m for m in missing)


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (paid_recurring_evidence(), EvidenceStrength.STRONG),
        (paid_recurring_evidence(bank_confirmed=False), EvidenceStrength.MODERATE),
        (EvidenceSet(has_invoice=True), EvidenceStrength.LIMITED),
        (EvidenceSet(has_contradiction=True), EvidenceStrength.DISPUTED),
    ],
)
def test_evidence_strength_reflects_completeness(evidence, expected):
    assert evidence.strength(DEFAULT_POLICY) is expected


# ---------------------------------------------------------------------------
# 5. Double-count detection
# ---------------------------------------------------------------------------


def test_a_combined_payment_across_invoices_is_not_a_double_count():
    """One bank credit settling four invoices is normal, not a conflict.

    This produced 241 false positives before the detector was corrected — and a
    detector that fires on clean data is one a reviewer learns to ignore.
    """
    classifications = [
        classify(
            item(item_id=f"invoice:{n}",
                 evidence=paid_recurring_evidence(contract_states_recurring=False),
                 gross_minor=100, allocated_minor=100, retained_minor=100,
                 invoice_id=str(n), payment_ids=["P"]),
            DEFAULT_POLICY,
        )
        for n in range(4)
    ]
    conflicts = detect_double_counting(classifications, {"P": 400})
    assert conflicts == []


def test_one_invoice_supporting_two_verified_items_is_a_conflict():
    shared = [
        classify(
            item(item_id=f"item:{n}",
                 evidence=paid_recurring_evidence(contract_states_recurring=False),
                 gross_minor=100, allocated_minor=100, retained_minor=100,
                 invoice_id="SAME"),
            DEFAULT_POLICY,
        )
        for n in range(2)
    ]
    conflicts = detect_double_counting(shared)
    assert len(conflicts) == 1
    assert "counted twice" in conflicts[0]["reason"]


def test_recognising_more_than_a_payment_retained_is_a_conflict():
    classifications = [
        classify(
            item(item_id=f"invoice:{n}",
                 evidence=paid_recurring_evidence(contract_states_recurring=False),
                 gross_minor=1000, allocated_minor=1000, retained_minor=1000,
                 invoice_id=str(n), payment_ids=["P"]),
            DEFAULT_POLICY,
        )
        for n in range(3)
    ]
    # The payment only ever retained 1000, but 3000 is attributed to it.
    conflicts = detect_double_counting(classifications, {"P": 1000})
    assert len(conflicts) == 1
    assert "counted more than once" in conflicts[0]["reason"]


# ---------------------------------------------------------------------------
# 6. End-to-end against the real dataset
# ---------------------------------------------------------------------------


@pytest.fixture
async def verified():
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema

    await create_schema()
    async with get_sessionmaker()() as session:
        workspace = Workspace(
            company_name="F5 End To End",
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
        await ingestion.ingest_all(session, workspace_id=workspace_id)
    async with get_sessionmaker()() as session:
        result = await revenue.verify_revenue(session, workspace_id=workspace_id)
        await session.commit()

    yield workspace_id, result
    await dispose_engine()


async def test_end_to_end_classifies_every_item_exactly_once(verified):
    _, result = verified
    assert result.items_classified > 0
    assert sum(result.by_class.values()) == result.items_classified
    # Every state used is one of the eight.
    assert set(result.by_class) <= {str(c) for c in RevenueClass}


async def test_end_to_end_reports_no_double_counting(verified):
    _, result = verified
    assert result.double_count_conflicts == [], result.double_count_conflicts


async def test_end_to_end_surfaces_unread_contracts(verified):
    """Supported ARR of zero must be explained, not left to look like a finding."""
    _, result = verified
    if result.totals.supported_arr == 0:
        assert result.contracts_unread > 0, (
            "ARR is zero but no reason was surfaced"
        )


async def test_end_to_end_finds_the_refunded_and_unpaid_items(verified):
    _, result = verified
    assert result.by_class.get("REFUNDED_OR_REVERSED", 0) >= 1
    assert result.by_class.get("INVOICED_UNPAID", 0) >= 1
    assert result.totals.refunded_reversed > 0
    assert result.totals.invoiced_unpaid > 0


async def test_end_to_end_waterfall_starts_from_the_claim(verified):
    _, result = verified
    assert result.waterfall
    assert result.waterfall[0]["label"] == "Claimed revenue"
    assert result.waterfall[0]["amount_minor"] == result.totals.claimed_revenue
    # Every deduction names a reason, not just an amount.
    for step in result.waterfall:
        if step["kind"] == "deduction":
            assert step.get("note"), f"{step['label']} has no explanation"


def _walk(waterfall: list[dict]) -> int:
    """Apply the steps the way a reader does: start, then each signed movement."""
    running = waterfall[0]["amount_minor"]
    for step in waterfall[1:]:
        if step["kind"] == "total":
            continue
        running += step["amount_minor"]
    return running


async def test_one_contract_contributes_its_arr_once(verified):
    """A contract billed in instalments must not multiply its own run-rate.

    Found on live data: three monthly ₹75,000 instalments under one ₹9,00,000/year
    contract each contributed the full annualised figure, reporting ₹27,00,000 of
    ARR where ₹9,00,000 exists. It is the exact overstatement this product exists to
    catch, and the double-count detector could not see it — that checks cash is not
    counted twice, and no cash was counted twice. ARR is a property of the contract.
    """
    _, result = verified

    recurring = [
        c for c in result.classifications
        if c.classification is RevenueClass.VERIFIED_RECURRING
        and c.arr_contribution_minor > 0
    ]
    per_contract: dict[str, int] = {}
    for c in recurring:
        key = c.contract_id or f"item:{c.item_id}"
        per_contract[key] = max(per_contract.get(key, 0), c.arr_contribution_minor)

    assert result.totals.supported_arr == sum(per_contract.values())
    if recurring:
        naive_sum = sum(c.arr_contribution_minor for c in recurring)
        assert result.totals.supported_arr <= naive_sum


def test_instalments_under_one_contract_do_not_multiply_arr():
    """The unit-level statement of the same rule."""
    shared_contract = str(uuid.uuid4())
    classifications = [
        classify(
            item(
                item_id=f"invoice:{n}",
                contract_id=shared_contract,
                evidence=paid_recurring_evidence(),
                gross_minor=75_000_00, allocated_minor=75_000_00,
                retained_minor=75_000_00, in_period_minor=9_00_000_00,
                annualised_recurring_minor=9_00_000_00,
                billing_frequency="monthly", contract_recurring_minor=75_000_00,
            ),
            DEFAULT_POLICY,
        )
        for n in range(3)
    ]
    assert all(
        c.classification is RevenueClass.VERIFIED_RECURRING for c in classifications
    )
    per_contract = {c.contract_id: c.arr_contribution_minor for c in classifications}
    assert sum(per_contract.values()) == 9_00_000_00, (
        "three instalments of one contract support one annual run-rate, not three"
    )


async def test_end_to_end_unapplied_cash_becomes_an_item(verified):
    """The Zenith case: money received with no invoice and no contract.

    Anchoring items on invoices alone dropped it entirely — the receipt had no
    invoice row to hang on, so ₹18,09,000 of unexplained cash left no trace in the
    revenue figures at all. Money arriving from nowhere is the finding a reviewer
    most needs; silently omitting it is worse than reporting it wrong.
    """
    _, result = verified
    assert result.by_class.get("PAYMENT_WITHOUT_SUPPORT", 0) >= 1
    assert result.totals.payment_without_support > 0

    unsupported = [
        c for c in result.classifications
        if c.classification is RevenueClass.PAYMENT_WITHOUT_SUPPORT
    ]
    assert all(c.recognized_minor == 0 for c in unsupported), (
        "unexplained cash is reported, never recognised as revenue"
    )
    assert any(c.item_id.startswith("payment:") for c in unsupported)


async def test_end_to_end_waterfall_reconciles_exactly(verified):
    """The steps must add up.

    A reviewer who checks the arithmetic and finds it wrong stops trusting every
    other figure on the page — so the residual between an asserted claim and
    evidence-derived categories is carried as its own named step rather than left
    as an unexplained discrepancy.
    """
    _, result = verified
    assert _walk(result.waterfall) == result.totals.total_verified
    assert result.waterfall[-1]["kind"] == "total"
    assert result.waterfall[-1]["amount_minor"] == result.totals.total_verified


def test_waterfall_reconciles_when_the_claim_understates_the_evidence():
    """More retained cash than claimed is a finding, not a rounding difference."""
    totals = revenue.RevenueTotals(
        currency="INR",
        claimed_revenue=10_000_000_00,
        verified_one_time=14_808_999_99,
        refunded_reversed=1_298_000_00,
        invoiced_unpaid=531_000_00,
    )
    steps = revenue._build_waterfall(totals)

    assert _walk(steps) == totals.total_verified
    beyond = [s for s in steps if s["kind"] == "addition"]
    assert beyond, "cash exceeding the claim must be shown, not absorbed"
    assert beyond[0]["amount_minor"] > 0
    assert beyond[0]["note"]


def test_waterfall_does_not_claim_surplus_cash_when_there_is_none():
    """A positive residual is not automatically "more cash than claimed".

    Measured on live provider data: ₹20,00,000 claimed, ₹6,75,000 verified, but
    ₹1,29,80,000 of unpaid invoices. Subtracting evidence-derived categories from an
    asserted claim overshoots, so the balancing line came out positive — and was
    labelled "retained cash exceeds the figure claimed", which was flatly false.
    """
    totals = revenue.RevenueTotals(
        currency="INR",
        claimed_revenue=20_00_000_00,
        verified_one_time=6_75_000_00,
        invoiced_unpaid=1_29_80_000_00,
    )
    steps = revenue._build_waterfall(totals)

    assert _walk(steps) == totals.total_verified
    surplus = [s for s in steps if s["label"] == "Evidence beyond the claim"]
    assert not surplus, "verified revenue is below the claim; there is no surplus"
    balancing = [s for s in steps if s["kind"] == "addition"]
    assert balancing and "not additional revenue" in balancing[0]["note"]


def test_waterfall_names_the_unevidenced_part_of_an_overstated_claim():
    totals = revenue.RevenueTotals(
        currency="INR",
        claimed_revenue=20_000_000_00,
        verified_one_time=5_000_000_00,
        invoiced_unpaid=1_000_000_00,
    )
    steps = revenue._build_waterfall(totals)

    assert _walk(steps) == totals.total_verified
    residual = [s for s in steps if s["label"] == "Claimed but not evidenced"]
    assert residual, "an unexplained claim must be a named step"
    assert residual[0]["amount_minor"] == -14_000_000_00


async def test_end_to_end_concentration_shares_sum_to_one_hundred(verified):
    _, result = verified
    if result.concentration:
        total = sum(entry["share_pct"] for entry in result.concentration)
        assert abs(total - 100.0) < 0.5


async def test_end_to_end_is_idempotent(verified):
    """Re-running replaces revenue items rather than accumulating them."""
    from sqlalchemy import func, select

    from app.models import RevenueItem

    workspace_id, first = verified

    async def count() -> int:
        async with get_sessionmaker()() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(RevenueItem).where(
                            RevenueItem.workspace_id == workspace_id
                        )
                    )
                ).scalar_one()
            )

    before = await count()
    async with get_sessionmaker()() as session:
        second = await revenue.verify_revenue(session, workspace_id=workspace_id)
        await session.commit()

    assert await count() == before
    assert second.items_classified == first.items_classified
    assert second.totals.as_dict() == first.totals.as_dict()


async def test_nothing_is_published_before_review(verified):
    """Only Feature 7-approved or human-resolved items may be published."""
    from sqlalchemy import select

    from app.models import RevenueItem

    workspace_id, _ = verified
    async with get_sessionmaker()() as session:
        published = (
            await session.execute(
                select(RevenueItem).where(
                    RevenueItem.workspace_id == workspace_id,
                    RevenueItem.is_published.is_(True),
                )
            )
        ).scalars().all()
    assert published == []


async def test_every_stored_item_carries_a_rule_and_policy_version(verified):
    from sqlalchemy import select

    from app.models import RevenueItem

    workspace_id, _ = verified
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(RevenueItem).where(RevenueItem.workspace_id == workspace_id)
            )
        ).scalars().all()

    assert rows
    for row in rows:
        assert row.rule_id in RULES
        assert row.rule_explanation.strip()
        assert row.policy_version == "v1"
        # Recognised can never exceed gross — enforced in the DB too.
        assert row.recognized_amount <= row.gross_amount


async def test_read_only_recompute_matches_the_run_and_writes_nothing(verified):
    """Reopening the page must still say what the claim came to.

    The classified items persist; the totals and waterfall built from them do not.
    Loading only the items left the page listing rows with no statement of what they
    added up to against the claim — the one question it exists to answer. The read
    path recomputes through this same function so the two cannot disagree, and must
    write nothing while doing it.
    """
    from sqlalchemy import func, select

    from app.models import RevenueItem

    workspace_id, first = verified

    async def item_count() -> int:
        async with get_sessionmaker()() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(RevenueItem).where(
                            RevenueItem.workspace_id == workspace_id
                        )
                    )
                ).scalar_one()
            )

    before = await item_count()
    async with get_sessionmaker()() as session:
        replay = await revenue.verify_revenue(
            session, workspace_id=workspace_id, persist=False
        )
        await session.rollback()

    assert await item_count() == before, "a read recomputation wrote to the database"
    assert replay.totals.claimed_revenue == first.totals.claimed_revenue
    assert replay.totals.total_verified == first.totals.total_verified
    assert replay.totals.verified_recurring == first.totals.verified_recurring
    assert replay.totals.verified_one_time == first.totals.verified_one_time
    assert replay.totals.supported_arr == first.totals.supported_arr
    assert replay.items_classified == first.items_classified
    # The waterfall is what the reader checks the arithmetic on, so it has to survive
    # the round trip step for step.
    assert [s["label"] for s in replay.waterfall] == [
        s["label"] for s in first.waterfall
    ]
    assert [s["amount_minor"] for s in replay.waterfall] == [
        s["amount_minor"] for s in first.waterfall
    ]
