"""Entity resolution orchestration — Feature 2, sub-features 6-7 plus the pipeline.

Runs the documented Feature 2 workflow end to end:

    canonical records from Feature 1 → normalised identifiers → blocking rules →
    fuzzy/semantic features → probability and evidence → Neo4j proposed link →
    independent Match Critic → accept, reject or Feature 7 review →
    workspace-scoped alias memory → canonical customer IDs for Features 3, 4 and 6

Also holds the two governance pieces the spec insists on:

* **Correction memory (sub-feature 6)** is workspace-scoped and applied *before*
  scoring. A reviewer who once said "these two are different" should never be asked
  again, and that decision must never leak to another tenant.
* **False-merge protection (sub-feature 7)** blocks auto-merge until measured
  precision on labelled pairs clears a threshold — core_resoruces.md: "auto-merge
  remains blocked until labelled-pair evaluation meets a chosen false-merge target."
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.events import EventKind, Severity, emit
from app.features.identity import graph as identity_graph
from app.features.identity.critic import criticise, is_material
from app.features.identity.identifiers import (
    IdentityKeys,
    _has_conflicting_tax_ids,
    build_identity_keys,
)
from app.features.identity.matching import (
    MatchCandidate,
    cannot_link_pairs,
    cluster_accepted,
    rank_candidates,
    transitivity_conflicts,
)
from app.models import (
    BankTransaction,
    Contract,
    CorrectionMemory,
    CustomerEntity,
    EntityMatchProposal,
    Invoice,
    Payment,
    RawRecord,
    ReviewItem,
    RevenueItem,
)
from app.models.enums import (
    AnomalySeverity,
    MatchDecision,
    RecordType,
    TransactionDirection,
)
from app.services.audit import record_audit_event

# Auto-merge stays disabled until measured precision on labelled pairs clears this.
FALSE_MERGE_PRECISION_TARGET = 0.95


@dataclass
class ResolutionResult:
    records_considered: int = 0
    pairs_generated: int = 0
    accepted: int = 0
    review: int = 0
    rejected: int = 0
    clusters: int = 0
    critic_disputes: int = 0
    memory_applied: int = 0
    critic_calls: int = 0
    not_challenged: int = 0
    transitivity_conflicts: list[dict] = field(default_factory=list)
    blocked_merges: list[dict] = field(default_factory=list)
    review_items_created: int = 0
    evaluation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "records_considered": self.records_considered,
            "pairs_generated": self.pairs_generated,
            "accepted": self.accepted,
            "review": self.review,
            "rejected": self.rejected,
            "clusters": self.clusters,
            "critic_disputes": self.critic_disputes,
            "memory_applied": self.memory_applied,
            "critic_calls": self.critic_calls,
            "not_challenged": self.not_challenged,
            "transitivity_conflicts": self.transitivity_conflicts,
            "blocked_merges": self.blocked_merges,
            "review_items_created": self.review_items_created,
            "evaluation": self.evaluation,
        }


# ---------------------------------------------------------------------------
# Collecting identity records from Feature 1's output
# ---------------------------------------------------------------------------


async def collect_identity_records(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[IdentityKeys]:
    """Build comparable identity keys from every source that names a customer.

    Deliberately reads the *source* records rather than the canonical customer
    table: the whole point of Feature 2 is to decide which of these are the same
    customer, so starting from an already-merged view would beg the question.
    """
    records: list[IdentityKeys] = []

    # Accounting contacts carry the richest identifiers (GSTIN, email, address).
    raw_contacts = (
        (
            await session.execute(
                select(RawRecord)
                .where(
                    RawRecord.workspace_id == workspace_id,
                    RawRecord.record_type == RecordType.CUSTOMER,
                    RawRecord.superseded_by_id.is_(None),
                )
                .order_by(RawRecord.source_id.asc())
            )
        )
        .scalars()
        .all()
    )
    for row in raw_contacts:
        payload = row.payload or {}
        address = (payload.get("billing_address") or {}).get("address")
        records.append(
            build_identity_keys(
                record_type="contact",
                record_id=f"contact:{row.source_id}",
                source_system=str(row.source_system),
                display_name=payload.get("contact_name")
                or payload.get("company_name")
                or row.source_id,
                emails=[payload.get("email")] if payload.get("email") else [],
                phones=[payload.get("phone")] if payload.get("phone") else [],
                tax_ids=[payload.get("gst_no")] if payload.get("gst_no") else [],
                addresses=[address] if address else [],
                website=payload.get("website"),
                platform_ids=[row.source_id],
            )
        )

    # CRM accounts contribute a domain and an alternative spelling.
    raw_crm = (
        (
            await session.execute(
                select(RawRecord)
                .where(
                    RawRecord.workspace_id == workspace_id,
                    RawRecord.record_type == RecordType.CRM_ACCOUNT,
                    RawRecord.superseded_by_id.is_(None),
                )
                .order_by(RawRecord.source_id.asc())
            )
        )
        .scalars()
        .all()
    )
    for row in raw_crm:
        properties = (row.payload or {}).get("properties") or {}
        if not properties.get("name"):
            continue
        records.append(
            build_identity_keys(
                record_type="crm_account",
                record_id=f"crm:{row.source_id}",
                source_system=str(row.source_system),
                display_name=properties["name"],
                domains=[properties.get("domain")] if properties.get("domain") else [],
                addresses=[properties.get("city")] if properties.get("city") else [],
            )
        )

    # Contracts name the legal entity, which often differs from the trading name.
    contracts = (
        (
            await session.execute(
                select(Contract)
                .where(Contract.workspace_id == workspace_id)
                .order_by(Contract.document_name.asc(), Contract.id.asc())
            )
        )
        .scalars()
        .all()
    )
    for contract in contracts:
        # A filename is a guess about the party, and a guess is only worth making
        # once the document itself has been read and produced nothing. Feature 2
        # runs before Feature 3, so on a first pass every contract is unread — and
        # inventing a customer from `Blue_Harbor_Analytics_Agreement.pdf` created a
        # customer of that name even where the contract inside names someone else
        # entirely. Silence is the honest answer until the contract is read; a
        # rerun after Feature 3 picks up the real party name.
        extracted = "terms_not_yet_extracted" not in (contract.unknown_fields or [])
        name = contract.stated_customer_name or (
            _name_from_filename(contract.document_name) if extracted else None
        )
        if name:
            records.append(
                build_identity_keys(
                    record_type="contract",
                    record_id=f"contract:{contract.id}",
                    source_system="google_drive",
                    display_name=name,
                )
            )

    # Payment and bank descriptions are the weakest names but the ones that carry
    # the cash, so they must be resolvable.
    payments = (
        (
            await session.execute(
                select(Payment)
                .where(
                    Payment.workspace_id == workspace_id,
                    Payment.stated_customer_name.isnot(None),
                )
                .order_by(Payment.source_id.asc(), Payment.id.asc())
            )
        )
        .scalars()
        .all()
    )
    seen_payment_names: set[str] = set()
    for payment in payments:
        key = (payment.stated_customer_name or "").lower()
        if not key or key in seen_payment_names:
            continue
        seen_payment_names.add(key)
        records.append(
            build_identity_keys(
                record_type="payment",
                record_id=f"payment:{payment.id}",
                source_system=str(payment.source_system),
                display_name=payment.stated_customer_name,
                emails=[payment.contact_email] if payment.contact_email else [],
                phones=[payment.contact_phone] if payment.contact_phone else [],
            )
        )

    bank_rows = (
        (
            await session.execute(
                select(BankTransaction)
                .where(
                    BankTransaction.workspace_id == workspace_id,
                    BankTransaction.counterparty.isnot(None),
                )
                .order_by(BankTransaction.source_id.asc(), BankTransaction.id.asc())
            )
        )
        .scalars()
        .all()
    )
    # A customer is a party money arrives *from*. A counterparty seen only on debits
    # is the company's own outgoing spend — payroll, rent, hosting — and promoting it
    # to a customer put "SALARY DISBURSEMENT APRIL" in the resolved-customer list and
    # inflated the count that customer concentration is measured against.
    #
    # "Only on debits" is not the same as "not a customer", which is why the test is
    # not simply on direction. A refund or chargeback goes *out* to a real customer,
    # and its narration may spell the name differently from the receipt that came in
    # ("COBALT MEDIA NETWORKS" against "COBALT MEDIA"). So an outgoing-only party is
    # kept when some other source already names it and dropped only when the bank
    # statement is the sole place it appears: this stops outgoing spend *creating* a
    # customer without severing a known one's outbound leg, which Feature 6 needs to
    # close a circular flow.
    paying_counterparties = {
        (row.counterparty or "").lower()
        for row in bank_rows
        if row.direction == TransactionDirection.CREDIT
    }
    known_elsewhere = {record.normalized_name for record in records}
    seen_counterparties: set[str] = set()
    for row in bank_rows:
        key = (row.counterparty or "").lower()
        if not key or key in seen_counterparties:
            continue
        if key not in paying_counterparties:
            probe = build_identity_keys(
                record_type="bank_counterparty",
                record_id=f"bank:{row.id}",
                source_system="bank_csv",
                display_name=row.counterparty,
            )
            if probe.normalized_name not in known_elsewhere:
                continue
        seen_counterparties.add(key)
        records.append(
            build_identity_keys(
                record_type="bank_counterparty",
                record_id=f"bank:{row.id}",
                source_system="bank_csv",
                display_name=row.counterparty,
            )
        )

    return records


def _name_from_filename(filename: str) -> str:
    """Recover a customer name from a contract filename.

    A fallback only, used when the contract body has not been parsed yet
    (Feature 3 supplies the real party name). Filenames are a convention, not
    evidence, so the resulting link will rarely clear the acceptance threshold on
    its own — which is the correct outcome.
    """
    import re

    stem = filename.rsplit(".", 1)[0]
    stem = re.sub(
        r"_(MSA|Agreement|Contract|SOW|Amendment|Subscription|Services|Annual|Scanned)"
        r"(_\d+)?|_\d{4}|_FY\d{4}",
        " ",
        stem,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[_-]+", " ", stem).strip()


# ---------------------------------------------------------------------------
# Correction memory (sub-feature 6)
# ---------------------------------------------------------------------------


async def load_correction_memory(
    session: AsyncSession, workspace_id: uuid.UUID
) -> dict[tuple[str, str], str]:
    """Reviewer-confirmed decisions for this workspace only.

    Never aggregated across tenants: core_resoruces.md rejects automatic
    cross-tenant learning outright, and one company's alias list is not evidence
    about another's.
    """
    rows = (
        (
            await session.execute(
                select(CorrectionMemory).where(
                    CorrectionMemory.workspace_id == workspace_id,
                    CorrectionMemory.correction_type == "identity_link",
                    CorrectionMemory.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    memory: dict[tuple[str, str], str] = {}
    for row in rows:
        value = row.corrected_value or {}
        left, right = value.get("left"), value.get("right")
        decision = value.get("decision")
        if left and right and decision:
            memory[tuple(sorted((left, right)))] = decision
    return memory


async def remember_decision(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    left_id: str,
    right_id: str,
    decision: str,
    reason: str,
    user_id: uuid.UUID | None = None,
) -> CorrectionMemory:
    """Store a human decision so the same pair is never re-asked."""
    left, right = sorted((left_id, right_id))
    existing = (
        await session.execute(
            select(CorrectionMemory).where(
                CorrectionMemory.workspace_id == workspace_id,
                CorrectionMemory.correction_type == "identity_link",
                CorrectionMemory.subject == f"{left}|{right}",
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.corrected_value = {"left": left, "right": right, "decision": decision}
        existing.reason = reason
        existing.is_active = True
        entry = existing
    else:
        entry = CorrectionMemory(
            workspace_id=workspace_id,
            correction_type="identity_link",
            subject=f"{left}|{right}",
            corrected_value={"left": left, "right": right, "decision": decision},
            reason=reason,
            created_by_user_id=user_id,
        )
        session.add(entry)

    await session.flush()
    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="human" if user_id else "system",
        actor_id=str(user_id) if user_id else "system",
        action="identity.correction_remembered",
        object_type="correction_memory",
        object_id=f"{left}|{right}",
        after_state={"decision": decision},
        reason=reason,
    )
    return entry


# ---------------------------------------------------------------------------
# Evaluation and false-merge protection (sub-feature 7)
# ---------------------------------------------------------------------------


def evaluate_against_labels(
    candidates: list[MatchCandidate],
    labelled_pairs: dict[tuple[str, str], bool],
) -> dict[str, Any]:
    """Measure precision and recall against known-correct pairs.

    Precision is the number that gates auto-merge: it is the rate at which accepted
    links are genuinely the same customer. A recall miss costs review time; a
    precision miss corrupts customer concentration and can hide a related party.
    """
    true_positive = false_positive = false_negative = true_negative = 0

    for candidate in candidates:
        key = tuple(sorted((candidate.left.record_id, candidate.right.record_id)))
        if key not in labelled_pairs:
            continue
        should_match = labelled_pairs[key]
        did_match = candidate.decision == "ACCEPTED"
        if should_match and did_match:
            true_positive += 1
        elif should_match and not did_match:
            false_negative += 1
        elif not should_match and did_match:
            false_positive += 1
        else:
            true_negative += 1

    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive)
        else 1.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative)
        else 1.0
    )
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "labelled_pairs_evaluated": true_positive
        + false_positive
        + false_negative
        + true_negative,
        "auto_merge_permitted": precision >= FALSE_MERGE_PRECISION_TARGET,
        "precision_target": FALSE_MERGE_PRECISION_TARGET,
    }


def blocking_recall(
    candidates: list[MatchCandidate], labelled_pairs: dict[tuple[str, str], bool]
) -> float:
    """Share of true pairs that blocking even brought forward for comparison.

    A pair never generated can never be matched, so blocking recall bounds overall
    recall no matter how good the scorer is.
    """
    generated = {
        tuple(sorted((c.left.record_id, c.right.record_id))) for c in candidates
    }
    true_pairs = {pair for pair, is_match in labelled_pairs.items() if is_match}
    if not true_pairs:
        return 1.0
    return round(len(true_pairs & generated) / len(true_pairs), 4)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def resolve_identities(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: str | None = None,
    use_critic: bool = True,
    labelled_pairs: dict[tuple[str, str], bool] | None = None,
) -> ResolutionResult:
    """Run Feature 2 end to end for one workspace."""
    run_id = run_id or uuid.uuid4().hex[:12]
    result = ResolutionResult()

    emit(
        EventKind.AGENT_STEP,
        "Entity Resolution Agent starting",
        workspace_id=str(workspace_id),
        feature=2,
        run_id=run_id,
    )

    records = await collect_identity_records(session, workspace_id)
    result.records_considered = len(records)
    if len(records) < 2:
        emit(
            EventKind.RESULT,
            "Entity resolution skipped: fewer than two identity records",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=2,
            run_id=run_id,
        )
        return result

    memory = await load_correction_memory(session, workspace_id)
    candidates = rank_candidates(records)
    result.pairs_generated = len(candidates)

    emit(
        EventKind.RULE,
        f"Blocking produced {len(candidates)} candidate pairs from {len(records)} records",
        workspace_id=str(workspace_id),
        feature=2,
        run_id=run_id,
    )

    # Apply reviewer memory before anything else decides.
    final_decisions: dict[int, str] = {}
    critic_notes: dict[int, str] = {}
    for index, candidate in enumerate(candidates):
        key = tuple(sorted((candidate.left.record_id, candidate.right.record_id)))
        if key in memory:
            final_decisions[index] = memory[key]
            critic_notes[index] = "Applied a previously confirmed reviewer decision."
            result.memory_applied += 1

    # Spend the critic budget on the riskiest links first. A pair carrying a
    # contradiction can produce a false merge, which is the damaging error; a pair
    # sitting just above the acceptance threshold is the next most contestable.
    # Anything past the budget keeps its deterministic decision and is *reported* as
    # unchallenged rather than quietly treated as approved.
    pending = [i for i, _ in enumerate(candidates) if i not in final_decisions]

    def risk_rank(index: int) -> tuple[int, float]:
        candidate = candidates[index]
        has_conflict = any(
            comparison.outcome == "conflict" for comparison in candidate.comparisons
        )
        return (0 if has_conflict else 1, candidate.total_weight)

    material = sorted(
        (i for i in pending if is_material(candidates[i])), key=risk_rank
    )
    budget = settings.critic_call_budget if use_critic else 0
    to_challenge = set(material[:budget])
    result.not_challenged = max(0, len(material) - len(to_challenge))

    for index in pending:
        candidate = candidates[index]
        if index not in to_challenge:
            final_decisions[index] = candidate.decision
            if use_critic and is_material(candidate):
                critic_notes[index] = (
                    "Not independently challenged: the per-run critic budget was "
                    "exhausted. This link rests on deterministic evidence alone."
                )
            continue
        decision, verdict = await criticise(
            candidate, workspace_id=str(workspace_id), run_id=run_id
        )
        result.critic_calls += 1
        final_decisions[index] = decision
        critic_notes[index] = verdict.reasoning
        if verdict.verdict == "DISPUTE":
            result.critic_disputes += 1

    if result.not_challenged:
        emit(
            EventKind.SYSTEM,
            f"{result.not_challenged} contestable links were not independently "
            f"challenged (critic budget {budget} exhausted)",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=2,
            run_id=run_id,
        )

    # Re-derive `decision` from the critic's verdict for clustering.
    for index, candidate in enumerate(candidates):
        decision = final_decisions.get(index, candidate.decision)
        if decision != candidate.decision:
            # Weaken in place: the exact-match short-circuit must not survive a
            # deterministic objection.
            candidate.exact_match = None if decision != "ACCEPTED" else candidate.exact_match
            candidate.total_weight = (
                candidate.total_weight if decision == "ACCEPTED" else min(
                    candidate.total_weight, 5.0 if decision == "REVIEW" else 0.0
                )
            )

    accepted_candidates = [
        candidate
        for index, candidate in enumerate(candidates)
        if final_decisions.get(index) == "ACCEPTED"
    ]
    result.accepted = len(accepted_candidates)
    result.review = sum(1 for d in final_decisions.values() if d == "REVIEW")
    result.rejected = sum(1 for d in final_decisions.values() if d == "REJECTED")

    # Cannot-link constraints are derived from ALL scored pairs, not just accepted
    # ones: the contradiction lives in a pair that was rejected, and it must still
    # prevent those two records being merged through some third record.
    blocked = cannot_link_pairs(candidates)
    clusters, blocked_merges = cluster_accepted(
        accepted_candidates, records, cannot_link=blocked
    )
    result.clusters = len(clusters)
    result.blocked_merges = blocked_merges
    # Any conflict surviving the constraint is a defect in the constraint itself.
    result.transitivity_conflicts = transitivity_conflicts(candidates, clusters)

    # Evaluation gate: auto-merge is only permitted when measured precision clears
    # the target. Without labels the result is reported as unmeasured, not as a pass.
    if labelled_pairs:
        result.evaluation = evaluate_against_labels(candidates, labelled_pairs)
        result.evaluation["blocking_recall"] = blocking_recall(candidates, labelled_pairs)
    else:
        result.evaluation = {
            "labelled_pairs_evaluated": 0,
            "auto_merge_permitted": False,
            "note": (
                "No labelled pairs supplied; matcher precision is unmeasured for this "
                "workspace and automatic merging stays disabled."
            ),
        }

    await _persist_proposals(
        session,
        workspace_id=workspace_id,
        candidates=candidates,
        decisions=final_decisions,
        notes=critic_notes,
    )
    result.review_items_created = await _create_review_items(
        session,
        workspace_id=workspace_id,
        candidates=candidates,
        decisions=final_decisions,
        notes=critic_notes,
        conflicts=result.transitivity_conflicts + result.blocked_merges,
    )
    await _apply_clusters(session, workspace_id=workspace_id, clusters=clusters, records=records)
    retired = await _retire_unevidenced_entities(session, workspace_id=workspace_id)

    # Mirror into the graph. A graph failure must not lose the PostgreSQL result.
    try:
        await identity_graph.persist_source_records(
            str(workspace_id),
            [
                {
                    "id": record.record_id,
                    "display_name": record.display_name,
                    "source_system": record.source_system,
                    "record_type": record.record_type,
                    "normalized_name": record.normalized_name,
                }
                for record in records
            ],
        )
        await identity_graph.persist_match_links(str(workspace_id), candidates)
        names = {
            root: next(
                (r.display_name for r in records if r.record_id == root), root
            )
            for root in clusters
        }
        await identity_graph.persist_customer_clusters(
            str(workspace_id),
            {root: sorted(members) for root, members in clusters.items()},
            names,
        )
        await identity_graph.persist_shared_attributes(
            str(workspace_id),
            shared_domains=_group_by_attribute(records, clusters, "domains"),
            shared_addresses=_group_by_attribute(records, clusters, "addresses"),
            shared_accounts={},
        )
    except Exception as exc:
        emit(
            EventKind.ERROR,
            f"Evidence graph write failed: {exc}",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=2,
            run_id=run_id,
        )

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id="entity_resolution",
        action="identity.resolved",
        object_type="workspace",
        object_id=str(workspace_id),
        after_state=result.as_dict(),
        reason=f"entity resolution run {run_id}",
    )

    if retired:
        emit(
            EventKind.RESULT,
            f"Retired {retired} customer records that own no evidence — ingestion stubs "
            f"superseded by the resolved clusters",
            workspace_id=str(workspace_id),
            severity=Severity.INFO,
            feature=2,
            run_id=run_id,
        )

    emit(
        EventKind.RESULT,
        f"Entity resolution: {result.clusters} customers from {result.records_considered} "
        f"records — {result.accepted} links accepted, {result.review} for review, "
        f"{result.critic_disputes} disputed by the critic",
        workspace_id=str(workspace_id),
        severity=Severity.SUCCESS,
        feature=2,
        run_id=run_id,
        **{k: v for k, v in result.as_dict().items()
           if k not in {"transitivity_conflicts", "blocked_merges"}},
    )
    return result


def _group_by_attribute(
    records: list[IdentityKeys], clusters: dict[str, set[str]], attribute: str
) -> dict[str, list[str]]:
    """Map an attribute value to the clusters that carry it."""
    member_of = {
        member: root for root, members in clusters.items() for member in members
    }
    grouped: dict[str, set[str]] = {}
    for record in records:
        cluster = member_of.get(record.record_id)
        if cluster is None:
            continue
        for value in getattr(record, attribute, frozenset()):
            grouped.setdefault(value, set()).add(cluster)
    return {value: sorted(roots) for value, roots in grouped.items() if len(roots) > 1}


async def _persist_proposals(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    candidates: list[MatchCandidate],
    decisions: dict[int, str],
    notes: dict[int, str],
) -> None:
    """Store every scored pair, including rejections."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    for index, candidate in enumerate(candidates):
        decision = decisions.get(index, candidate.decision)
        values = {
            "workspace_id": workspace_id,
            "left_type": candidate.left.record_type,
            "left_id": candidate.left.record_id,
            "left_label": candidate.left.display_name[:300],
            "right_type": candidate.right.record_type,
            "right_id": candidate.right.record_id,
            "right_label": candidate.right.display_name[:300],
            "method": candidate.method,
            "score": round(candidate.probability, 4),
            "signals": candidate.explain(),
            "decision": MatchDecision(decision),
            "decided_by": "agent",
            "critic_note": notes.get(index, "")[:2000] or None,
        }
        statement = (
            pg_insert(EntityMatchProposal)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["workspace_id", "left_type", "left_id",
                                "right_type", "right_id"],
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in {"workspace_id", "left_type", "left_id",
                                 "right_type", "right_id"}
                },
            )
        )
        await session.execute(statement)


async def _create_review_items(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    candidates: list[MatchCandidate],
    decisions: dict[int, str],
    notes: dict[int, str],
    conflicts: list[dict],
) -> int:
    """Queue uncertain links for a human (Feature 7's queue)."""
    created = 0
    for index, candidate in enumerate(candidates):
        if decisions.get(index) != "REVIEW":
            continue
        existing = (
            await session.execute(
                select(ReviewItem)
                .where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.category == "ambiguous_match",
                    ReviewItem.title
                    == f"{candidate.left.display_name} ↔ {candidate.right.display_name}",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue

        session.add(
            ReviewItem(
                workspace_id=workspace_id,
                category="ambiguous_match",
                title=f"{candidate.left.display_name} ↔ {candidate.right.display_name}",
                detail=(
                    notes.get(index)
                    or "Insufficient evidence to accept or reject this identity link."
                ),
                severity=AnomalySeverity.MEDIUM,
                evidence_packet={
                    "left": {
                        "id": candidate.left.record_id,
                        "name": candidate.left.display_name,
                        "source": candidate.left.source_system,
                        "tax_ids": sorted(candidate.left.tax_ids),
                        "domains": sorted(candidate.left.domains),
                    },
                    "right": {
                        "id": candidate.right.record_id,
                        "name": candidate.right.display_name,
                        "source": candidate.right.source_system,
                        "tax_ids": sorted(candidate.right.tax_ids),
                        "domains": sorted(candidate.right.domains),
                    },
                    "match_weight": round(candidate.total_weight, 2),
                    "probability": round(candidate.probability, 4),
                    "evidence": candidate.explain(),
                    "blocking_rule": candidate.blocking_rule,
                },
            )
        )
        created += 1

    for conflict in conflicts:
        # A merge the cannot-link constraint *blocked* is the system working, not a
        # task: it was refused on decisive evidence — conflicting tax identifiers or
        # domains — and it is already listed as a prevented merge on the identity
        # screen. Queueing "I correctly declined to do the wrong thing" as a
        # high-severity action item put 21 rows in front of a reviewer that had
        # nothing for them to decide, and a queue mostly made of those is one people
        # stop reading. Only a conflict nobody resolved still needs a person.
        if conflict.get("blocked_by_constraint") or conflict.get("would_have_merged"):
            continue
        pair = conflict.get("would_have_merged") or [
            conflict.get("left", "?"), conflict.get("right", "?")
        ]
        title = f"Prevented false merge: {pair[0]} ↔ {pair[1]}"[:300]
        existing = (
            await session.execute(
                select(ReviewItem)
                .where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.title == title,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            ReviewItem(
                workspace_id=workspace_id,
                category="ambiguous_match",
                title=title,
                detail=conflict["reason"],
                severity=AnomalySeverity.HIGH,
                evidence_packet=conflict,
            )
        )
        created += 1

    await session.flush()
    return created


async def _apply_clusters(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    clusters: dict[str, set[str]],
    records: list[IdentityKeys],
) -> None:
    """Write resolved clusters onto canonical customers and link their evidence."""
    by_id = {record.record_id: record for record in records}

    for root in sorted(clusters):
        members = clusters[root]
        member_records = [by_id[m] for m in sorted(members) if m in by_id]
        if not member_records:
            continue

        # The longest name is usually the fullest legal form, which is the most
        # useful label for a reviewer.
        canonical = max(member_records, key=lambda r: len(r.display_name))
        aliases = sorted({r.display_name for r in member_records})
        domains = sorted({d for r in member_records for d in r.domains})
        tax_ids = sorted({t for r in member_records for t in r.tax_ids})
        emails = sorted({e for r in member_records for e in r.emails})
        addresses = sorted({a for r in member_records for a in r.addresses})

        # Feature 1 also creates CustomerEntity rows during ingestion, and the
        # schema does not enforce one row per normalised name, so more than one can
        # legitimately exist. Take the oldest deterministically rather than assuming
        # uniqueness — a re-run must converge, not raise.
        entity = (
            await session.execute(
                select(CustomerEntity)
                .where(
                    CustomerEntity.workspace_id == workspace_id,
                    CustomerEntity.normalized_name == canonical.normalized_name,
                )
                .order_by(CustomerEntity.created_at.asc(), CustomerEntity.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if entity is None:
            entity = CustomerEntity(
                workspace_id=workspace_id,
                canonical_name=canonical.display_name,
                normalized_name=canonical.normalized_name,
            )
            session.add(entity)

        # A human-confirmed cluster is never silently rewritten by a later run.
        if not entity.human_confirmed:
            entity.canonical_name = canonical.display_name
            entity.known_aliases = aliases
            entity.domains = domains
            entity.tax_identifiers = tax_ids
            entity.email_addresses = emails
            entity.addresses = addresses
            entity.match_confidence = 1.0 if len(members) == 1 else 0.9

        await session.flush()
        await _link_evidence_to_entity(
            session,
            workspace_id=workspace_id,
            entity_id=entity.id,
            member_records=member_records,
        )
        await _absorb_duplicate_entities(
            session,
            workspace_id=workspace_id,
            entity=entity,
            member_names={r.normalized_name for r in member_records if r.normalized_name},
        )


async def _absorb_duplicate_entities(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    entity: CustomerEntity,
    member_names: set[str],
) -> int:
    """Fold Feature 1's name-keyed customer rows into the cluster that resolved them.

    Feature 1 creates a customer per name it meets during ingestion and binds that
    name's invoices to it. Feature 2 then decides several of those names are one
    company — but only ever wrote its answer to the row matching the cluster's
    *canonical* name, so the losing rows kept the evidence Feature 1 had already
    given them. "LARKSPUR SYSTEM LTD" held four invoices and "Larkspur Systems Pvt
    Ltd" held the rest: one customer, one GSTIN, revenue split across two rows.

    That is not a cosmetic duplicate. Concentration is measured per customer, so a
    customer halved reads as two smaller ones and the largest-customer share comes
    out too low — the exact understatement Feature 2 exists to prevent, arriving
    through the back door after the matching had already got the answer right.

    Only rows whose normalised name is one this cluster actually contains are
    absorbed, and never a human-confirmed row: this moves evidence onto the customer
    the matcher already decided owns it, and decides nothing itself.
    """
    if not member_names:
        return 0

    duplicates = (
        (
            await session.execute(
                select(CustomerEntity).where(
                    CustomerEntity.workspace_id == workspace_id,
                    CustomerEntity.id != entity.id,
                    CustomerEntity.human_confirmed.is_(False),
                    CustomerEntity.normalized_name.in_(member_names),
                )
            )
        )
        .scalars()
        .all()
    )
    # Sharing a name with a cluster member is enough to *propose* the fold and not
    # enough to perform it. Clustering can legitimately hold a record naming the
    # subsidiary inside the parent's cluster — Meridian Holdings pays invoices raised
    # on Meridian Systems, and both trade on one domain from one address. Absorbing
    # on the name alone turned that soft association into a hard, irreversible merge
    # of two legal entities, which is the single most damaging thing this feature can
    # do. Contradictory tax registrations veto the fold, exactly as they veto a merge
    # during clustering; a shared PAN across two state GSTINs is not a contradiction.
    target = build_identity_keys(
        record_type="entity",
        record_id=str(entity.id),
        source_system="resolved",
        display_name=entity.canonical_name,
        tax_ids=list(entity.tax_identifiers or []),
        domains=list(entity.domains or []),
    )
    safe: list[CustomerEntity] = []
    for duplicate in duplicates:
        other = build_identity_keys(
            record_type="entity",
            record_id=str(duplicate.id),
            source_system="resolved",
            display_name=duplicate.canonical_name,
            tax_ids=list(duplicate.tax_identifiers or []),
            domains=list(duplicate.domains or []),
        )
        if not _has_conflicting_tax_ids(target, other):
            safe.append(duplicate)
    duplicates = safe

    if not duplicates:
        return 0

    stale_ids = [d.id for d in duplicates]
    for model in (Invoice, Payment, BankTransaction, RevenueItem, Contract):
        await session.execute(
            update(model)
            .where(
                model.workspace_id == workspace_id,
                model.customer_entity_id.in_(stale_ids),
            )
            .values(customer_entity_id=entity.id)
        )

    # The absorbed spellings are part of this customer's identity now; losing them
    # would make the merge unreviewable.
    merged_aliases = set(entity.known_aliases or [])
    merged_tax = set(entity.tax_identifiers or [])
    merged_domains = set(entity.domains or [])
    for duplicate in duplicates:
        merged_aliases.update(duplicate.known_aliases or [])
        merged_aliases.add(duplicate.canonical_name)
        merged_tax.update(duplicate.tax_identifiers or [])
        merged_domains.update(duplicate.domains or [])
        await session.delete(duplicate)
    entity.known_aliases = sorted(merged_aliases)
    entity.tax_identifiers = sorted(merged_tax)
    entity.domains = sorted(merged_domains)
    await session.flush()
    return len(duplicates)


async def _retire_unevidenced_entities(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> int:
    """Delete customer rows that no evidence points at.

    Feature 1 creates a `CustomerEntity` per ingested contact, and Feature 2 then
    resolves clusters and attaches the evidence to whichever row carries the
    cluster's canonical name. The rows that lost that contest were left behind, so
    the resolved-customer list showed 44 customers where 21 owned a record —
    including the same company twice, once with its aliases and once without. A
    reviewer reading that list cannot tell the duplicate from a genuine near-match,
    and every count measured over customers (concentration above all) is wrong.

    Only a row that owns nothing at all is removed, and never one a human has
    confirmed: this deletes bookkeeping, never a judgement or a piece of evidence.
    """
    owned: set[uuid.UUID] = set()
    for model in (Invoice, Payment, BankTransaction, RevenueItem, Contract):
        column = model.customer_entity_id
        rows = await session.execute(
            select(column).where(
                model.workspace_id == workspace_id, column.isnot(None)
            ).distinct()
        )
        owned.update(row[0] for row in rows)

    stale = (
        (
            await session.execute(
                select(CustomerEntity).where(
                    CustomerEntity.workspace_id == workspace_id,
                    CustomerEntity.human_confirmed.is_(False),
                    CustomerEntity.id.notin_(owned) if owned else true(),
                )
            )
        )
        .scalars()
        .all()
    )
    for entity in stale:
        await session.delete(entity)
    if stale:
        await session.flush()
    return len(stale)


async def _link_evidence_to_entity(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    member_records: list[IdentityKeys],
) -> None:
    """Attach invoices, payments and bank rows to their resolved customer."""
    contact_ids = {
        r.record_id.split(":", 1)[1] for r in member_records if r.record_type == "contact"
    }
    payment_ids = {
        r.record_id.split(":", 1)[1] for r in member_records if r.record_type == "payment"
    }
    bank_ids = {
        r.record_id.split(":", 1)[1]
        for r in member_records
        if r.record_type == "bank_counterparty"
    }

    if contact_ids:
        invoices = (
            (
                await session.execute(
                    select(Invoice).where(
                        Invoice.workspace_id == workspace_id,
                        Invoice.customer_entity_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for invoice in invoices:
            raw = await session.get(RawRecord, invoice.raw_record_id) if invoice.raw_record_id else None
            customer_ref = (raw.payload or {}).get("customer_id") if raw else None
            if customer_ref and str(customer_ref) in contact_ids:
                invoice.customer_entity_id = entity_id

    for payment_id in payment_ids:
        try:
            payment = await session.get(Payment, uuid.UUID(payment_id))
        except ValueError:
            continue
        if payment is not None and payment.workspace_id == workspace_id:
            payment.customer_entity_id = entity_id

    for bank_id in bank_ids:
        try:
            row = await session.get(BankTransaction, uuid.UUID(bank_id))
        except ValueError:
            continue
        if row is not None and row.workspace_id == workspace_id:
            row.customer_entity_id = entity_id

    await session.flush()
