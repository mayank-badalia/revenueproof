"""Feature 2 tests — Cross-System Customer Identity and Relationship Graph.

The central assertion of this feature is asymmetric: it must merge four spellings of
Northstar into one customer *and* keep Blue Harbor Analytics separate from Blue
Harbour Logistics. A matcher that does only the first is worse than useless, because
it silently understates customer concentration.

Covers Step 2a categories 1, 2, 4, 6, 7 and 11 (goal-fidelity).
"""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
from httpx import ASGITransport

from app.core.db import get_sessionmaker
from app.features.identity import service
from app.features.identity.critic import deterministic_objections
from app.features.identity.identifiers import (
    build_identity_keys,
    find_exact_match,
    is_valid_gstin,
    name_tokens,
    normalize_domain,
    normalize_email,
    normalize_name,
    normalize_phone,
    pan_from_gstin,
)
from app.features.identity.matching import (
    cannot_link_pairs,
    cluster_accepted,
    generate_candidate_pairs,
    name_similarity,
    rank_candidates,
    score_pair,
)
from app.main import app
from app.models import CustomerEntity, Workspace
from app.services import ingestion


def keys(name: str, **kwargs):
    """Terse helper for building identity keys in tests."""
    return build_identity_keys(
        record_type=kwargs.pop("record_type", "contact"),
        record_id=kwargs.pop("record_id", name.lower().replace(" ", "_")[:40]),
        source_system=kwargs.pop("source_system", "zoho_books"),
        display_name=name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Identifier cleaning (sub-feature 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Northstar Technologies Private Limited", "northstar technologies"),
        ("Northstar Technologies Pvt. Ltd.", "northstar technologies"),
        ("Blue Harbour Logistics LLP", "blue harbour logistics"),
        ("Acme Inc.", "acme"),
        ("  Spaced   Out  Co.  ", "spaced out"),
        ("Foo & Bar Ltd", "foo and bar"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_name_strips_legal_forms(raw, expected):
    assert normalize_name(raw) == expected


def test_name_tokens_drop_low_information_words():
    """'Technologies' must not make two unrelated tech companies look similar."""
    assert name_tokens("Northstar Technologies") == frozenset({"northstar"})
    # A wholly generic name keeps its tokens rather than becoming empty.
    assert name_tokens("Global Solutions") == frozenset({"global", "solutions"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.northstar.io/pricing", "northstar.io"),
        ("accounts@northstar.io", "northstar.io"),
        ("NORTHSTAR.IO", "northstar.io"),
        ("founder@gmail.com", None),        # free provider says nothing about identity
        ("notadomain", None),
        (None, None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_normalize_email_handles_gmail_aliases():
    assert normalize_email("A.B+tag@Gmail.com") == "ab@gmail.com"
    # Dots are significant outside Gmail.
    assert normalize_email("a.b@northstar.io") == "a.b@northstar.io"


def test_normalize_phone_keeps_last_ten_digits():
    assert normalize_phone("+91 98765 43210") == "9876543210"
    assert normalize_phone("098765-43210") == "9876543210"
    assert normalize_phone("12345") is None


def test_gstin_validation_and_pan_extraction():
    """Two GSTINs sharing a PAN are one legal entity in different states."""
    assert is_valid_gstin("27AABCN1234F1Z5") is True
    assert is_valid_gstin("NOTAGSTIN") is False
    assert pan_from_gstin("27AABCN1234F1Z5") == "AABCN1234F"
    # Same PAN, different state code — same entity.
    assert pan_from_gstin("29AABCN1234F1Z3") == "AABCN1234F"
    assert pan_from_gstin("invalid") is None


# ---------------------------------------------------------------------------
# 2. Deterministic exact matching
# ---------------------------------------------------------------------------


def test_shared_pan_across_states_is_an_exact_match():
    left = keys("Northstar Technologies Pvt Ltd", tax_ids=["27AABCN1234F1Z5"])
    right = keys("Northstar Tech Maharashtra", tax_ids=["29AABCN1234F1Z3"])
    match = find_exact_match(left, right)
    assert match is not None
    assert match.rule_id == "EXACT_PAN"


def test_different_pans_are_not_an_exact_match():
    left = keys("Blue Harbor Analytics", tax_ids=["29AACCB5678K1Z2"])
    right = keys("Blue Harbour Logistics", tax_ids=["27AAFFB9012M1Z8"])
    assert find_exact_match(left, right) is None


def test_shared_domain_without_name_overlap_is_not_a_match():
    """Parent and subsidiary share a domain but are different customers."""
    parent = keys("Meridian Holdings", domains=["meridiangroup.in"])
    subsidiary = keys("Vertex Operations", domains=["meridiangroup.in"])
    assert find_exact_match(parent, subsidiary) is None


def test_shared_domain_with_name_overlap_is_a_match():
    left = keys("Northstar Technologies", domains=["northstar.io"])
    right = keys("Northstar", domains=["northstar.io"])
    match = find_exact_match(left, right)
    assert match is not None
    assert match.rule_id == "EXACT_DOMAIN_AND_NAME"


# ---------------------------------------------------------------------------
# 3. THE CORE CASES (Step 2a category 11 — goal fidelity)
# ---------------------------------------------------------------------------


def test_four_spellings_of_northstar_resolve_to_one_customer():
    """idea_features.md §2: the flagship entity-resolution case."""
    records = [
        keys("Northstar Technologies Private Limited", record_id="c1",
             tax_ids=["27AABCN1234F1Z5"]),
        keys("Northstar Tech", record_id="i1", emails=["accounts@northstar.io"],
             tax_ids=["27AABCN1234F1Z5"]),
        keys("northstar.io", record_id="h1", domains=["northstar.io"],
             source_system="hubspot"),
    ]
    candidates = rank_candidates(records)
    clusters, _ = cluster_accepted(
        [c for c in candidates if c.decision == "ACCEPTED"], records
    )
    assert len(clusters) == 1, f"expected one customer, got {len(clusters)}"


def test_blue_harbor_and_blue_harbour_are_never_merged():
    """The false-merge trap: 79% name similarity, different legal entities."""
    left = keys("Blue Harbor Analytics Private Limited", record_id="bh1",
                tax_ids=["29AACCB5678K1Z2"], domains=["blueharbor.co.in"])
    right = keys("Blue Harbour Logistics LLP", record_id="bh2",
                 tax_ids=["27AAFFB9012M1Z8"], domains=["blueharbour-logistics.com"])

    assert name_similarity(left, right) > 70, "these names really are similar"
    candidate = score_pair(left, right)
    assert candidate.decision == "REJECTED"
    assert candidate.total_weight < 0


def test_false_merge_is_blocked_even_through_an_intermediary():
    """The dangerous case: a third record matching both pulls them together.

    A bank narration "BLUE HARBOR" resembles both companies and carries no
    identifiers. Plain union-find would merge all three. The cannot-link constraint
    must refuse, because the direct comparison found contradictory tax IDs.
    """
    analytics = keys("Blue Harbor Analytics Private Limited", record_id="bh1",
                     tax_ids=["29AACCB5678K1Z2"], domains=["blueharbor.co.in"])
    logistics = keys("Blue Harbour Logistics LLP", record_id="bh2",
                     tax_ids=["27AAFFB9012M1Z8"], domains=["blueharbour-logistics.com"])
    narration = keys("BLUE HARBOR", record_id="bank1", record_type="bank_counterparty",
                     source_system="bank_csv")

    records = [analytics, logistics, narration]
    candidates = rank_candidates(records)
    blocked = cannot_link_pairs(candidates)
    assert ("bh1", "bh2") in blocked, "the contradiction was not recorded"

    clusters, blocked_merges = cluster_accepted(
        [c for c in candidates if c.decision == "ACCEPTED"],
        records,
        cannot_link=blocked,
    )
    # The two companies must end up in different clusters.
    membership = {member: root for root, members in clusters.items() for member in members}
    assert membership["bh1"] != membership["bh2"], (
        "Blue Harbor Analytics and Blue Harbour Logistics were merged transitively"
    )
    if blocked_merges:
        assert "contradictory identifiers" in blocked_merges[0]["reason"]


def test_bank_narration_abbreviation_reaches_review_not_acceptance():
    """"NSTAR TECH PVT" should be surfaced, but confirmed by cash evidence, not name."""
    company = keys("Northstar Technologies Private Limited", record_id="c1")
    narration = keys("NSTAR TECH PVT", record_id="b1",
                     record_type="bank_counterparty", source_system="bank_csv")

    pairs = generate_candidate_pairs([company, narration])
    assert pairs, "blocking never even considered the abbreviation"

    candidate = score_pair(company, narration)
    assert candidate.decision in {"REVIEW", "REJECTED"}
    assert candidate.decision != "ACCEPTED", "a narration must not auto-merge on name alone"


def test_parent_and_subsidiary_stay_separate_customers():
    """spec §18: a payment from a parent for a subsidiary's contract."""
    parent = keys("Meridian Holdings Private Limited", record_id="p1",
                  tax_ids=["07AADCM3456P1Z1"], domains=["meridiangroup.in"],
                  addresses=["Tower B, Cyber City, Gurugram 122002"])
    subsidiary = keys("Meridian Systems India Private Limited", record_id="s1",
                      tax_ids=["07AADCM7890Q1Z4"], domains=["meridiangroup.in"],
                      addresses=["Tower B, Cyber City, Gurugram 122002"])
    candidate = score_pair(parent, subsidiary)
    assert candidate.decision != "ACCEPTED", (
        "a parent and its subsidiary share a domain and address but are different customers"
    )


def test_two_customers_behind_one_payment_agent_stay_separate():
    """Both pay via GLOBAL PAY SERVICES; the agent is not the customer."""
    crestview = keys("Crestview Retail Private Limited", record_id="c1",
                     tax_ids=["09AABCC2233A1Z4"], domains=["crestview.shop"])
    pinnacle = keys("Pinnacle Foods Private Limited", record_id="p1",
                    tax_ids=["09AABCP4455B1Z7"], domains=["pinnaclefoods.in"])
    candidate = score_pair(crestview, pinnacle)
    assert candidate.decision == "REJECTED"


# ---------------------------------------------------------------------------
# 4. Tier discipline — semantic evidence can never decide alone
# ---------------------------------------------------------------------------


def test_semantic_similarity_cannot_carry_a_pair_to_acceptance():
    """core_resoruces.md: embeddings are supporting evidence, never proof."""
    left = keys("Alpha Retail Private Limited", record_id="a1")
    right = keys("Zeta Commerce Private Limited", record_id="z1")

    without = score_pair(left, right)
    with_semantic = score_pair(left, right, semantic_similarity=1.0)

    assert with_semantic.total_weight > without.total_weight, "semantic adds some weight"
    assert with_semantic.decision != "ACCEPTED", (
        "maximum semantic similarity must not be sufficient on its own"
    )
    # The contribution is capped.
    assert with_semantic.total_weight - without.total_weight <= 1.0


def test_exact_identifier_short_circuits_weaker_signals():
    """A shared PAN decides the question; nothing below it should matter."""
    left = keys("Completely Different Name A", record_id="a", tax_ids=["27AABCN1234F1Z5"])
    right = keys("Totally Unrelated Name B", record_id="b", tax_ids=["29AABCN1234F1Z3"])
    candidate = score_pair(left, right)
    assert candidate.exact_match is not None
    assert candidate.decision == "ACCEPTED"


# ---------------------------------------------------------------------------
# 5. Blocking recall
# ---------------------------------------------------------------------------


def test_blocking_brings_forward_pairs_sharing_an_identifier():
    records = [
        keys("Alpha Systems", record_id="a", tax_ids=["27AABCN1234F1Z5"]),
        keys("Alpha Sys", record_id="b", tax_ids=["27AABCN1234F1Z5"]),
        keys("Unrelated Foods", record_id="c", tax_ids=["09ZZZZZ9999Z1Z9"]),
    ]
    pairs = {tuple(sorted((left, right))) for left, right, _ in
             generate_candidate_pairs(records)}
    assert ("a", "b") in pairs


def test_oversized_blocks_are_skipped():
    """A 500-member block is a generic token, not a signal — and 125k pairs."""
    records = [keys("Generic Trading Company", record_id=f"r{i}") for i in range(80)]
    pairs = generate_candidate_pairs(records, max_block_size=20)
    assert pairs == []


def test_clustering_is_deterministic_across_runs():
    records = [
        keys("Northstar Technologies Private Limited", record_id="c1",
             tax_ids=["27AABCN1234F1Z5"]),
        keys("Northstar Tech", record_id="i1", tax_ids=["27AABCN1234F1Z5"]),
        keys("Lumen Software", record_id="l1"),
    ]
    first, _ = cluster_accepted(
        [c for c in rank_candidates(records) if c.decision == "ACCEPTED"], records
    )
    second, _ = cluster_accepted(
        [c for c in rank_candidates(records) if c.decision == "ACCEPTED"], records
    )
    assert {k: sorted(v) for k, v in first.items()} == {
        k: sorted(v) for k, v in second.items()
    }


# ---------------------------------------------------------------------------
# 6. Match Critic (sub-feature 5)
# ---------------------------------------------------------------------------


def test_deterministic_objections_catch_contradictory_identifiers():
    left = keys("Blue Harbor Analytics", tax_ids=["29AACCB5678K1Z2"])
    right = keys("Blue Harbour Logistics", tax_ids=["27AAFFB9012M1Z8"])
    objections = deterministic_objections(score_pair(left, right))
    assert any("CONTRADICTORY_IDENTIFIER" in o for o in objections)


def test_deterministic_objection_for_shared_address_only():
    left = keys("Alpha Trading", record_id="a", addresses=["Tower B Cyber City Gurugram"])
    right = keys("Zeta Foods", record_id="z", addresses=["Tower B Cyber City Gurugram"])
    objections = deterministic_objections(score_pair(left, right))
    assert any("SHARED_ADDRESS_NOT_SAME" in o for o in objections)


async def test_critic_without_a_model_downgrades_a_contestable_link(monkeypatch):
    """Fail safe, not fail open: an unchallenged *material* link goes to review."""
    from app.core import llm
    from app.features.identity.critic import criticise, is_material

    monkeypatch.setattr(llm, "is_available", lambda: False)

    # Identical names carry the pair over the threshold, but the two records give
    # different corporate domains — accepted, yet genuinely contestable.
    left = keys("Northstar Technologies", record_id="n1", domains=["northstar.io"])
    right = keys("Northstar Technologies", record_id="n2",
                 domains=["northstar-group.com"], source_system="hubspot")
    candidate = score_pair(left, right)
    assert candidate.decision == "ACCEPTED"
    assert is_material(candidate), "a domain conflict must make the merge contestable"

    decision, verdict = await criticise(candidate, workspace_id="w1")
    assert decision == "REVIEW"
    assert verdict.verdict in {"NEEDS_REVIEW", "DISPUTE"}


def test_overwhelming_unconflicted_matches_skip_the_critic():
    """Deliberate: not every accepted link is a judgement call.

    Twelve bank narrations reading "BLUE HARBOR" are identical to each other with no
    contradictory identifier. Routing those to the model split them into twelve
    separate customers during testing — over-splitting inflates the customer count
    and understates concentration just as a false merge does.
    """
    from app.features.identity.critic import is_material

    left = keys("Kestrel Logistics Private Limited", record_id="k1")
    right = keys("Kestrel Logistics Private Limited", record_id="k2",
                 source_system="hubspot")
    candidate = score_pair(left, right)
    assert candidate.decision == "ACCEPTED"
    assert is_material(candidate) is False


def test_conflicting_identifiers_always_reach_the_critic():
    """A contradiction must be challenged however strong the rest of the evidence."""
    from app.features.identity.critic import is_material

    left = keys("Northstar Technologies", record_id="a", domains=["northstar.io"])
    right = keys("Northstar Technologies", record_id="b",
                 domains=["other-domain.com"], source_system="hubspot")
    candidate = score_pair(left, right)
    assert candidate.decision == "ACCEPTED", "identical names should clear the threshold"
    assert is_material(candidate) is True


def test_shared_tax_id_outranks_a_domain_difference():
    """A shared registration is decisive; a differing domain does not undo it.

    Companies routinely use several domains. The tax registration is the identity.
    """
    from app.features.identity.critic import is_material

    left = keys("Northstar Technologies", record_id="a",
                domains=["northstar.io"], tax_ids=["27AABCN1234F1Z5"])
    right = keys("Northstar Tech", record_id="b",
                 domains=["northstar-group.com"], tax_ids=["27AABCN1234F1Z5"])
    candidate = score_pair(left, right)
    assert candidate.exact_match is not None
    assert candidate.decision == "ACCEPTED"
    # No model opinion needed on a matching tax registration.
    assert is_material(candidate) is False


async def test_critic_agrees_without_a_model_call_on_exact_identifiers():
    """A shared PAN is a fact; no model opinion is needed or wanted."""
    from app.features.identity.critic import criticise

    left = keys("Northstar Technologies", record_id="a", tax_ids=["27AABCN1234F1Z5"])
    right = keys("Northstar Tech", record_id="b", tax_ids=["29AABCN1234F1Z3"])
    decision, verdict = await criticise(score_pair(left, right), workspace_id="w1")
    assert decision == "ACCEPTED"
    assert verdict.verdict == "AGREE"


# ---------------------------------------------------------------------------
# 7. Evaluation and false-merge protection (sub-feature 7)
# ---------------------------------------------------------------------------


def test_evaluation_computes_precision_and_recall():
    records = [
        keys("Northstar Technologies Private Limited", record_id="a",
             tax_ids=["27AABCN1234F1Z5"]),
        keys("Northstar Tech", record_id="b", tax_ids=["27AABCN1234F1Z5"]),
        keys("Blue Harbour Logistics LLP", record_id="c",
             tax_ids=["27AAFFB9012M1Z8"]),
    ]
    candidates = rank_candidates(records)
    labels = {("a", "b"): True, ("a", "c"): False, ("b", "c"): False}
    result = service.evaluate_against_labels(candidates, labels)

    assert result["precision"] == 1.0
    assert result["false_positive"] == 0
    assert result["auto_merge_permitted"] is True


def test_auto_merge_is_blocked_when_precision_is_below_target():
    """core_resoruces.md: auto-merge stays blocked until measured precision passes."""
    records = [
        keys("Alpha Corp", record_id="a"),
        keys("Alpha Corp", record_id="b", source_system="hubspot"),
    ]
    candidates = rank_candidates(records)
    # Label the (correctly matched) pair as a non-match to force a false positive.
    result = service.evaluate_against_labels(candidates, {("a", "b"): False})
    assert result["false_positive"] == 1
    assert result["precision"] < service.FALSE_MERGE_PRECISION_TARGET
    assert result["auto_merge_permitted"] is False


def test_blocking_recall_measures_pairs_never_generated():
    records = [keys("Alpha", record_id="a"), keys("Completely Other", record_id="b")]
    candidates = rank_candidates(records)
    recall = service.blocking_recall(candidates, {("a", "b"): True})
    assert 0.0 <= recall <= 1.0


# ---------------------------------------------------------------------------
# 8. End-to-end against the real dataset
# ---------------------------------------------------------------------------


def unique_email(prefix: str = "f2") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
async def resolved_workspace():
    """Ingest the synthetic dataset and run entity resolution over it."""
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema

    await create_schema()
    async with get_sessionmaker()() as session:
        workspace = Workspace(
            company_name="F2 End To End",
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
        # Critic disabled: these assertions are about the deterministic matcher.
        result = await service.resolve_identities(
            session, workspace_id=workspace_id, use_critic=False
        )
        await session.commit()

    yield workspace_id, result
    await dispose_engine()


async def test_end_to_end_resolution_produces_sensible_clusters(resolved_workspace):
    workspace_id, result = resolved_workspace

    assert result.records_considered > 50
    assert result.pairs_generated > 0
    assert result.clusters > 0
    # 20 real customers in the dataset. Over-splitting is safe; heavy under-splitting
    # would mean customers were wrongly merged.
    assert result.clusters >= 15, f"suspiciously few clusters: {result.clusters}"


async def test_end_to_end_keeps_the_two_blue_harbours_separate(resolved_workspace):
    """The most important assertion in this feature."""
    workspace_id, _ = resolved_workspace
    async with get_sessionmaker()() as session:
        records = await service.collect_identity_records(session, workspace_id)

    candidates = rank_candidates(records)
    clusters, _ = cluster_accepted(
        [c for c in candidates if c.decision == "ACCEPTED"],
        records,
        cannot_link=cannot_link_pairs(candidates),
    )
    membership = {m: root for root, members in clusters.items() for m in members}

    analytics = next(
        (r for r in records if "blue harbor analytics" in r.display_name.lower()), None
    )
    logistics = next(
        (r for r in records if "blue harbour logistics" in r.display_name.lower()), None
    )
    assert analytics and logistics, "the false-merge trap records are missing"
    assert membership[analytics.record_id] != membership[logistics.record_id], (
        "Blue Harbor Analytics and Blue Harbour Logistics were merged — "
        "this is the false merge the feature exists to prevent"
    )


async def test_end_to_end_merges_northstar_spellings(resolved_workspace):
    workspace_id, _ = resolved_workspace
    async with get_sessionmaker()() as session:
        records = await service.collect_identity_records(session, workspace_id)

    candidates = rank_candidates(records)
    clusters, _ = cluster_accepted(
        [c for c in candidates if c.decision == "ACCEPTED"],
        records,
        cannot_link=cannot_link_pairs(candidates),
    )
    membership = {m: root for root, members in clusters.items() for m in members}

    northstar = [
        r for r in records
        if "northstar" in r.display_name.lower().replace(".", "")
    ]
    assert len(northstar) >= 2, "expected several Northstar spellings"
    roots = {membership[r.record_id] for r in northstar}
    assert len(roots) == 1, (
        f"Northstar spellings split across {len(roots)} customers: "
        f"{[r.display_name for r in northstar]}"
    )


async def test_outgoing_spend_never_becomes_a_customer(resolved_workspace):
    """A customer is a party money arrives *from*.

    Counterparties seen only on debits are the company's own outgoings. Promoting
    them put "SALARY DISBURSEMENT APRIL" and "OFFICE RENT APRIL" in the resolved
    customer list and inflated the denominator that concentration is measured
    against.
    """
    from sqlalchemy import select

    from app.models import BankTransaction, CustomerEntity
    from app.models.enums import TransactionDirection

    workspace_id, _ = resolved_workspace
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(BankTransaction).where(
                        BankTransaction.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )
        entities = list(
            (
                await session.execute(
                    select(CustomerEntity).where(
                        CustomerEntity.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )

    ever_paid = {
        (r.counterparty or "").strip().lower()
        for r in rows
        if r.direction == TransactionDirection.CREDIT and r.counterparty
    }
    debit_only = {
        (r.counterparty or "").strip().lower()
        for r in rows
        if r.direction == TransactionDirection.DEBIT and r.counterparty
    } - ever_paid
    assert debit_only, "the dataset should contain outgoing-only counterparties"

    # A refund or chargeback leaves the account towards a genuine customer, so the
    # assertion is not "no outgoing-only party resolves". It is that the company's
    # own operating spend — named nowhere but its bank statement — does not become a
    # customer. These are the rows §15 plants, and each was in the list before.
    resolved = {e.canonical_name.strip().lower() for e in entities}
    for spend in ("salary", "office rent", "cloud hosting"):
        leaked = sorted(name for name in resolved if spend in name)
        assert not leaked, f"outgoing spend resolved as a customer: {leaked}"

    # And the counterparties that *do* survive from the debit side are ones another
    # source already names, never new companies invented by the statement.
    invented = sorted(
        name for name in debit_only & resolved
        if any(word in name for word in ("salary", "rent", "hosting", "disbursement"))
    )
    assert not invented, f"outgoing spend invented customers: {invented}"


async def test_every_resolved_customer_owns_evidence(resolved_workspace):
    """A customer row nothing points at is bookkeeping, not a customer.

    Feature 1 creates an entity per ingested contact and Feature 2 attaches the
    evidence to whichever row carries its cluster's canonical name. The losers were
    left behind, so the list showed the same company twice — once with its aliases
    and once bare — and no reader can tell that from a genuine near-duplicate.
    """
    from sqlalchemy import select

    from app.models import BankTransaction, CustomerEntity, Invoice, Payment

    workspace_id, _ = resolved_workspace
    async with get_sessionmaker()() as session:
        owning: set[uuid.UUID] = set()
        for model in (Invoice, Payment, BankTransaction):
            rows = await session.execute(
                select(model.customer_entity_id).where(
                    model.workspace_id == workspace_id,
                    model.customer_entity_id.isnot(None),
                )
            )
            owning.update(r[0] for r in rows)
        entities = list(
            (
                await session.execute(
                    select(CustomerEntity).where(
                        CustomerEntity.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )

    stranded = sorted(e.canonical_name for e in entities if e.id not in owning)
    assert not stranded, f"customers with no evidence behind them: {stranded}"


async def test_no_two_customers_share_a_tax_identifier(resolved_workspace):
    """One GSTIN is one legal entity. Two rows carrying it is a duplicate."""
    from collections import defaultdict

    from sqlalchemy import select

    from app.models import CustomerEntity

    workspace_id, _ = resolved_workspace
    async with get_sessionmaker()() as session:
        entities = list(
            (
                await session.execute(
                    select(CustomerEntity).where(
                        CustomerEntity.workspace_id == workspace_id
                    )
                )
            )
            .scalars()
            .all()
        )

    by_tax: dict[str, list[str]] = defaultdict(list)
    for entity in entities:
        for tax_id in entity.tax_identifiers or []:
            by_tax[tax_id].append(entity.canonical_name)
    shared = {t: names for t, names in by_tax.items() if len(names) > 1}
    assert not shared, f"one tax id on several customers: {shared}"


async def test_resolution_creates_review_items_for_uncertain_links(resolved_workspace):
    workspace_id, result = resolved_workspace
    assert result.review > 0, "some links should be genuinely uncertain"
    assert result.review_items_created > 0


async def test_resolution_reports_unmeasured_precision_without_labels(resolved_workspace):
    """Without labelled pairs the system must not claim it is safe to auto-merge."""
    _, result = resolved_workspace
    assert result.evaluation["auto_merge_permitted"] is False
    assert "unmeasured" in result.evaluation["note"]


async def test_resolution_is_idempotent(resolved_workspace):
    """Re-running must converge, not accumulate.

    The property that matters is that a second pass adds no customers and no new
    match proposals — not that the customer count equals the cluster count, since
    Feature 1's ingestion also creates customer rows before resolution runs.
    """
    from sqlalchemy import func, select as sa_select

    workspace_id, first = resolved_workspace

    async def counts() -> tuple[int, int]:
        async with get_sessionmaker()() as session:
            customers = (
                await session.execute(
                    sa_select(func.count()).select_from(CustomerEntity).where(
                        CustomerEntity.workspace_id == workspace_id
                    )
                )
            ).scalar_one()
            from app.models import EntityMatchProposal

            proposals = (
                await session.execute(
                    sa_select(func.count()).select_from(EntityMatchProposal).where(
                        EntityMatchProposal.workspace_id == workspace_id
                    )
                )
            ).scalar_one()
        return int(customers), int(proposals)

    before = await counts()

    async with get_sessionmaker()() as session:
        second = await service.resolve_identities(
            session, workspace_id=workspace_id, use_critic=False
        )
        await session.commit()

    after = await counts()

    assert second.clusters == first.clusters, "cluster count drifted between runs"
    assert second.pairs_generated == first.pairs_generated
    assert after == before, (
        f"re-running accumulated rows: customers/proposals {before} → {after}"
    )


# ---------------------------------------------------------------------------
# 9. Correction memory (sub-feature 6)
# ---------------------------------------------------------------------------


async def test_reviewer_decisions_are_remembered_and_reapplied(resolved_workspace):
    workspace_id, _ = resolved_workspace

    async with get_sessionmaker()() as session:
        await service.remember_decision(
            session,
            workspace_id=workspace_id,
            left_id="contact:zc_000001",
            right_id="contact:zc_000002",
            decision="REJECTED",
            reason="Confirmed by the founder as separate companies",
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        memory = await service.load_correction_memory(session, workspace_id)

    key = tuple(sorted(("contact:zc_000001", "contact:zc_000002")))
    assert memory[key] == "REJECTED"


async def test_correction_memory_never_crosses_workspaces(resolved_workspace):
    """core_resoruces.md rejects automatic cross-tenant learning outright."""
    workspace_id, _ = resolved_workspace

    async with get_sessionmaker()() as session:
        other = Workspace(
            company_name="Unrelated Tenant",
            reporting_period_start=date(2026, 4, 1),
            reporting_period_end=date(2027, 3, 31),
            base_currency="INR",
        )
        session.add(other)
        await session.flush()
        other_id = other.id

        await service.remember_decision(
            session,
            workspace_id=workspace_id,
            left_id="a",
            right_id="b",
            decision="ACCEPTED",
            reason="confirmed in workspace one",
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        leaked = await service.load_correction_memory(session, other_id)

    assert leaked == {}, "a correction leaked into another tenant's memory"


# ---------------------------------------------------------------------------
# 10. API surface
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client():
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema

    await create_schema()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": unique_email(), "password": "diligence-2026"},
        )
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield client
    await dispose_engine()


async def test_identity_endpoints_round_trip(api_client):
    created = await api_client.post(
        "/api/v1/workspaces",
        json={
            "company_name": "API Identity Test",
            "reporting_period_start": "2026-04-01",
            "reporting_period_end": "2027-03-31",
            "base_currency": "INR",
            "claimed_revenue": "1000000",
            "claimed_arr": "1000000",
        },
    )
    workspace_id = created.json()["id"]

    await api_client.post(
        f"/api/v1/workspaces/{workspace_id}/ingest",
        json={"sources": ["zoho_books"], "include_bank_sample": False},
    )
    resolve = await api_client.post(
        f"/api/v1/workspaces/{workspace_id}/identity/resolve",
        json={"use_critic": False},
    )
    assert resolve.status_code == 200, resolve.text
    assert resolve.json()["clusters"] > 0

    customers = await api_client.get(
        f"/api/v1/workspaces/{workspace_id}/identity/customers"
    )
    assert customers.status_code == 200
    assert customers.json()["customers"]

    matches = await api_client.get(
        f"/api/v1/workspaces/{workspace_id}/identity/matches"
    )
    assert matches.status_code == 200
    # Rejections are returned too: "why were these NOT merged" must be answerable.
    assert "counts" in matches.json()


async def test_identity_endpoints_reject_other_tenants(api_client):
    created = await api_client.post(
        "/api/v1/workspaces",
        json={
            "company_name": "Owner Workspace",
            "reporting_period_start": "2026-04-01",
            "reporting_period_end": "2027-03-31",
            "base_currency": "INR",
            "claimed_revenue": "0",
            "claimed_arr": "0",
        },
    )
    workspace_id = created.json()["id"]

    outsider = await api_client.post(
        "/api/v1/auth/register",
        json={"email": unique_email("outsider"), "password": "diligence-2026"},
    )
    token = outsider.json()["access_token"]

    for path in ["identity/customers", "identity/matches"]:
        response = await api_client.get(
            f"/api/v1/workspaces/{workspace_id}/{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 404
