"""Anomaly orchestration, persistence and feedback — F6 sub-features 7 and 8.

    F2 identities + F4 reconciled cash + F5 classifications
      → deterministic rules ∥ conditional ML ∥ graph investigation ∥ concentration
      → joined evidence packets → persisted findings → Feature 7 review queue
      → measured precision, which decides whether the model runs at all next time

Three things this module is responsible for that no individual detector can be:

**Running the scans independently and joining them.** core_resoruces.md ranks Celery
canvas first for this, for chains/groups/chords. Celery is not introduced (CLAUDE.md
§2, deviation 4): the whole scan takes seconds, and a broker would add an operational
dependency to buy latency nothing here is waiting on. `asyncio.gather` over
`to_thread` gives the same fan-out/join for CPU-bound rules and a forest, against an
async graph query — and the join semantics, which is the part that matters, are
identical. The moment a scan outgrows one process the group becomes a Celery group
without the callers changing.

**Keeping human feedback alive across runs.** A scan recomputes every finding from
scratch, so the naive persistence — delete all, insert fresh — would erase the
reviewer's "this one is a false positive" every time anyone pressed the button. That
would make sub-feature 7 unmeasurable, because precision needs labels that outlive
the run that produced them. Findings are therefore matched to their stored rows by
what they *are* (rule plus the records that triggered it), and the human's verdict
carries over.

**Deciding whether the model is allowed to speak.** Precision is measured from those
labels, per rule, and the Isolation Forest is gated on its own measured record rather
than on the promise that a model helps. With no labels, that is stated as "not yet
measured" rather than assumed good — the honest answer, and the one core_resoruces.md
asks for when it says to disable ML rather than present an unstable score.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.features.contracts.service import allocate_to_period
from app.features.identity.identifiers import normalize_name
from app.features.revenue import service as revenue
from app.models import (
    Allocation,
    Anomaly,
    BankTransaction,
    Contract,
    CustomerEntity,
    Invoice,
    Payment,
    RawRecord,
    Refund,
    RevenueItem,
    ReviewItem,
    Workspace,
)
from app.models.enums import AnomalySeverity, PaymentStatus, ReviewStatus
from app.services.audit import record_audit_event

from . import concentration as concentration_mod
from . import explain, graph, rules, scoring
from .rules import (
    ContractRecord,
    Finding,
    InvoiceRecord,
    PaymentRecord,
    RefundRecord,
)

#: Severities that earn a place in Feature 7's queue. Everything is stored and
#: visible; only these interrupt a person. A LOW-severity statistical ranking that
#: opens a review item would bury the queue in scores.
ROUTED_SEVERITIES = {AnomalySeverity.HIGH}

#: Labelled findings needed before measured precision means anything. Below this,
#: one reviewer's opinion of one flag would decide whether a model runs.
MIN_LABELS_FOR_GATE = 10

#: Precision below which the model is switched off. Deliberately generous — the
#: forest ranks records for attention rather than asserting a fault, so it earns its
#: place by being right slightly more often than not.
ML_PRECISION_FLOOR = 0.5

#: A bank debit within this many days of a recorded refund, for the same amount, is
#: that refund leaving the account rather than an unexplained return of funds.
REFUND_MATCH_WINDOW = timedelta(days=7)


@dataclass
class AnomalyRunResult:
    """Everything one scan produced, including what it declined to do and why."""

    run_id: str = ""
    findings_total: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)

    ml: dict[str, Any] = field(default_factory=dict)
    graph: dict[str, Any] = field(default_factory=dict)
    concentration: dict[str, Any] = field(default_factory=dict)
    precision: dict[str, Any] = field(default_factory=dict)
    narrative: dict[str, Any] = field(default_factory=dict)

    anomalies_persisted: int = 0
    feedback_preserved: int = 0
    anomalies_retired: int = 0
    review_items_created: int = 0

    scanned: dict[str, int] = field(default_factory=dict)
    packets: list[explain.Packet] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "findings_total": self.findings_total,
            "by_rule": self.by_rule,
            "by_severity": self.by_severity,
            "ml": self.ml,
            "graph": self.graph,
            "concentration": self.concentration,
            "precision": self.precision,
            "narrative": self.narrative,
            "anomalies_persisted": self.anomalies_persisted,
            "feedback_preserved": self.feedback_preserved,
            "anomalies_retired": self.anomalies_retired,
            "review_items_created": self.review_items_created,
            "scanned": self.scanned,
            "findings": [p.as_dict() for p in self.packets],
        }


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


async def scan(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: str | None = None,
    use_llm: bool = True,
) -> AnomalyRunResult:
    """Run every detector, join the evidence, persist and route.

    Feature 5 is re-run first rather than read from its stored truth table, for the
    same reason Feature 5 re-runs Feature 4: an anomaly measured against a stale
    classification is a confident statement about evidence that has since changed.
    The recomputation is deterministic, so it costs seconds and buys the guarantee
    that every finding here refers to the figures currently on the page.

    It is re-run *without persisting*, which is the whole distinction. Recomputing
    Feature 5's figures is free; rewriting Feature 5's rows is not, because that
    discards `is_published` and the critic decision behind it — so a scan run after
    the critic used to revert the published position to nothing.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    result = AnomalyRunResult(run_id=run_id)

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return result

    emit(
        EventKind.AGENT_STEP,
        "Anomaly Detection Agent starting: rules, model, graph and concentration "
        "scans run independently and join",
        workspace_id=str(workspace_id),
        feature=6,
        run_id=run_id,
    )

    # Read-only. This scan needs Feature 5's concentration figures and nothing else,
    # and it must not *write* Feature 5's output: a persisting re-verification
    # deletes and re-inserts every revenue item, taking `is_published` and the whole
    # critic decision set with it. Scanning for anomalies after the critic had run
    # therefore reverted the published position to zero — 67 items published one
    # minute, 67 withheld the next, with nothing on the page to say why and an audit
    # trail that recorded the publication and then the silent unpublication. Feature
    # 5 owns those rows; Feature 6 owns anomalies.
    # Feature 5 has never run on this workspace, so there is nothing to preserve and
    # something to create: scanning an empty truth table would report anomalies over
    # revenue nobody had classified. Bootstrapping is the only case in which this
    # feature writes Feature 5's rows.
    classified = int(
        (
            await session.execute(
                select(func.count())
                .select_from(RevenueItem)
                .where(RevenueItem.workspace_id == workspace_id)
            )
        ).scalar_one()
    )
    revenue_result = await revenue.verify_revenue(
        session,
        workspace_id=workspace_id,
        run_id=run_id,
        persist=classified == 0,
        refresh_allocations=True,
    )

    inputs = await _build_inputs(session, workspace=workspace)
    result.scanned = {
        "payments": len(inputs.payments),
        "refunds": len(inputs.refunds),
        "invoices": len(inputs.invoices),
        "contracts": len(inputs.contracts),
        "customers": len(inputs.customer_ids),
        "bank_rows": inputs.bank_rows,
    }

    # Sub-feature 7 first: the model's own measured record decides whether it runs.
    result.precision = await measure_precision(session, workspace_id=workspace_id)
    ml_allowed = result.precision["ml_enabled"]

    # --- the four scans, independent, joined -------------------------------
    verified_by_customer = {
        entry["customer"]: entry["amount_minor"]
        for entry in revenue_result.concentration
    }

    async def run_rules() -> list[Finding]:
        return await asyncio.to_thread(
            rules.run_all,
            payments=inputs.payments,
            refunds=inputs.refunds,
            invoices=inputs.invoices,
            contracts=inputs.contracts,
            period_start=workspace.reporting_period_start,
            period_end=workspace.reporting_period_end,
        )

    async def run_model() -> scoring.ScoringResult:
        if not ml_allowed:
            return scoring.ScoringResult(
                enabled=False, reason=result.precision["ml_reason"]
            )
        return await asyncio.to_thread(scoring.score_payments, inputs.payments)

    async def run_concentration() -> concentration_mod.ConcentrationResult:
        return await asyncio.to_thread(
            concentration_mod.measure,
            verified_by_customer,
            currency=workspace.base_currency,
            customer_ids=inputs.customer_ids,
        )

    rule_findings, ml_result, graph_result, conc_result = await asyncio.gather(
        run_rules(),
        run_model(),
        _investigate_graph(workspace_id, inputs),
        run_concentration(),
    )

    result.ml = ml_result.as_dict()
    result.graph = graph_result.as_dict()
    result.concentration = conc_result.as_dict()

    findings: list[Finding] = [
        *rule_findings,
        *graph_result.findings,
        *conc_result.findings,
        *ml_result.findings,
    ]

    emit(
        EventKind.RULE,
        f"{len(rule_findings)} rule findings, {len(graph_result.findings)} graph, "
        f"{len(conc_result.findings)} concentration, {len(ml_result.findings)} model "
        f"({'model ran' if ml_result.enabled else 'model off: ' + ml_result.reason})",
        workspace_id=str(workspace_id),
        feature=6,
        run_id=run_id,
    )

    # --- packets, then narrative for the material ones ---------------------
    packets = [explain.build_packet(finding) for finding in findings]
    result.packets = packets
    if use_llm:
        result.narrative = await explain.add_narratives(
            packets, workspace_id=str(workspace_id)
        )
    else:
        result.narrative = {
            "attempted": 0, "written": 0, "rejected": 0, "skipped": 0,
            "reason": "narratives disabled for this run",
        }

    result.findings_total = len(findings)
    for finding in findings:
        result.by_rule[finding.rule_id] = result.by_rule.get(finding.rule_id, 0) + 1
        key = str(finding.severity)
        result.by_severity[key] = result.by_severity.get(key, 0) + 1

    # --- persist, preserving any human verdict -----------------------------
    persisted, preserved, retired = await _persist(
        session,
        workspace_id=workspace_id,
        packets=packets,
        key_to_entity=inputs.key_to_entity,
    )
    result.anomalies_persisted = persisted
    result.feedback_preserved = preserved
    result.anomalies_retired = retired

    result.review_items_created = await _route_to_review(
        session, workspace_id=workspace_id, packets=packets
    )

    # Precision is re-read after persisting: this run may have retired findings a
    # reviewer had labelled, and the figure shown must describe the findings that
    # now exist rather than the set that existed when the scan began.
    result.precision = await measure_precision(session, workspace_id=workspace_id)

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id="anomaly_detection",
        action="anomaly.scanned",
        object_type="workspace",
        object_id=str(workspace_id),
        after_state={
            "findings": result.findings_total,
            "by_rule": result.by_rule,
            "by_severity": result.by_severity,
            "ml_enabled": ml_result.enabled,
            "ml_model_version": ml_result.model_version,
            "graph_method": graph_result.method,
        },
        reason=f"anomaly scan {run_id}",
    )

    emit(
        EventKind.RESULT,
        f"Feature 6 complete: {result.findings_total} anomaly indicators "
        f"({result.by_severity.get('high', 0)} high), "
        f"{result.review_items_created} routed to review, "
        f"top customer {conc_result.top_share_pct:.1f}% of verified revenue. "
        f"Every finding is an indicator requiring review, never a finding of wrongdoing.",
        workspace_id=str(workspace_id),
        feature=6,
        severity=Severity.SUCCESS,
        run_id=run_id,
        by_rule=result.by_rule,
    )
    return result


# ---------------------------------------------------------------------------
# Adapters — canonical rows into the plain records the detectors take
# ---------------------------------------------------------------------------


def customer_key(entity_name: str | None, stated_name: str | None) -> str | None:
    """The one key every detector groups customers by.

    This has to be a single key space, and the reason is a bug this function exists
    to prevent rather than a matter of taste. The detectors group by
    `customer_id or customer_name`, so mixing resolved UUIDs and raw names puts the
    *same* customer under two different keys as soon as identity resolution has
    linked some of its records and not others — which is the normal state of a real
    workspace, not an edge case.

    Measured on the §15 dataset, that split hid the Blue Harbor near-duplicate
    outright: one of the two ₹59,000 payments had been linked to a customer entity
    and the other had not, so they landed in different groups and the pair the
    dataset exists to catch went unreported. Partially-resolved identity was
    therefore *worse* than none at all — the scan found the duplicate before
    Feature 2 ran and lost it afterwards.

    A normalised name is the only key both a resolved and an unresolved record can
    produce, so that is the key. The entity UUID is recovered at persistence time,
    where a foreign key is what is actually needed.
    """
    key = normalize_name(entity_name) or normalize_name(stated_name)
    return key or None


@dataclass
class ScanInputs:
    payments: list[PaymentRecord] = field(default_factory=list)
    refunds: list[RefundRecord] = field(default_factory=list)
    invoices: list[InvoiceRecord] = field(default_factory=list)
    contracts: list[ContractRecord] = field(default_factory=list)
    customer_ids: dict[str, str] = field(default_factory=dict)
    #: normalised customer key → the entity UUID behind it, where one is resolved
    key_to_entity: dict[str, str] = field(default_factory=dict)
    #: customer key → the key it is folded into when two records are one customer
    representative: dict[str, str] = field(default_factory=dict)
    #: representative key → every key folded into it (reported as A13)
    duplicate_groups: dict[str, list[str]] = field(default_factory=dict)
    #: customer key → the bank counterparties that paid on its behalf
    payer_accounts: dict[str, set[str]] = field(default_factory=dict)
    #: normalised counterparty → display name
    account_labels: dict[str, str] = field(default_factory=dict)
    #: counterparty → total credited / debited, for the flow graph
    credits: dict[str, int] = field(default_factory=dict)
    unexplained_debits: dict[str, int] = field(default_factory=dict)
    customer_names: dict[str, str] = field(default_factory=dict)
    customer_domains: dict[str, list[str]] = field(default_factory=dict)
    related_parties: dict[str, list[str]] = field(default_factory=dict)
    bank_rows: int = 0


async def _build_inputs(
    session: AsyncSession, *, workspace: Workspace
) -> ScanInputs:
    """Turn canonical evidence into detector inputs, ordered so runs are comparable."""
    workspace_id = workspace.id
    inputs = ScanInputs()

    async def fetch(model, *where):
        query = select(model).where(model.workspace_id == workspace_id, *where)
        return list((await session.execute(query.order_by(model.id))).scalars().all())

    customers = await fetch(CustomerEntity)
    entity_name: dict[str, str] = {}
    for customer in customers:
        entity_id = str(customer.id)
        entity_name[entity_id] = customer.canonical_name
        key = customer_key(customer.canonical_name, None)
        if key is None:
            continue
        inputs.customer_ids[customer.canonical_name] = entity_id
        inputs.key_to_entity.setdefault(key, entity_id)
        inputs.customer_names[key] = customer.canonical_name
        if customer.domains:
            inputs.customer_domains.setdefault(key, []).extend(customer.domains)
        if customer.related_party_status:
            inputs.related_parties[key] = list(customer.related_party_reasons or [])

    # Fold unmerged records of one customer together *before* anything is detected,
    # so every rule counts economic parties rather than database rows. Doing it here
    # rather than per-rule is what keeps the answers consistent: a duplicate that is
    # one customer to the concentration metric and two to the shared-account rule
    # would produce a page that contradicts itself.
    inputs.duplicate_groups = _suspected_duplicates(inputs.customer_names)
    inputs.representative = {
        member: group[0]
        for group in inputs.duplicate_groups.values()
        for member in group
    }
    for member, root in inputs.representative.items():
        if member == root:
            continue
        inputs.customer_domains.setdefault(root, []).extend(
            inputs.customer_domains.pop(member, [])
        )
        if member in inputs.related_parties:
            inputs.related_parties.setdefault(root, []).extend(
                inputs.related_parties.pop(member)
            )

    def resolve(key: str | None) -> str | None:
        return inputs.representative.get(key or "", key)

    bank_rows = await fetch(BankTransaction)
    inputs.bank_rows = len(bank_rows)
    bank_by_id = {str(row.id): row for row in bank_rows}

    # Which bank counterparty settled which payment. This is the only evidence in
    # the system for "who actually paid": the payment record names the customer the
    # invoice was raised on, while the bank statement names the account the money
    # left. When those disagree — a parent settling a subsidiary's bill, or one
    # agent settling for two unrelated companies — the difference is the finding.
    allocations = list(
        (
            await session.execute(
                select(Allocation).where(
                    Allocation.workspace_id == workspace_id,
                    Allocation.payment_id.is_not(None),
                    Allocation.bank_transaction_id.is_not(None),
                    Allocation.reversed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    payer_by_payment: dict[str, str] = {}
    for allocation in allocations:
        bank_row = bank_by_id.get(str(allocation.bank_transaction_id))
        if bank_row is None or not bank_row.counterparty:
            continue
        # Keyed on the account, not on the customer Feature 2 resolved it to. A06
        # asks whether several customers pay from *one account*, so folding the
        # account into the customer that paid from it makes the rule vacuous by
        # construction: every customer would own exactly one account and no account
        # could ever be shared. The circular-flow graph below keys the same rows on
        # the entity instead, because it is asking a different question.
        account = normalize_name(bank_row.counterparty) or bank_row.counterparty
        payer_by_payment.setdefault(str(allocation.payment_id), account)
        inputs.account_labels.setdefault(account, bank_row.counterparty)

    # --- payments -----------------------------------------------------------
    # Only successful payments become records. A failed payment is money that never
    # arrived: counting it would inflate the refund-rate denominator and let two
    # declined attempts read as a duplicate capture.
    for payment in await fetch(Payment):
        if not PaymentStatus(payment.status).is_successful:
            continue
        payment_key = str(payment.id)
        account = payer_by_payment.get(payment_key)
        key = resolve(
            customer_key(
                entity_name.get(str(payment.customer_entity_id) or ""),
                payment.stated_customer_name,
            )
        )
        if account and key:
            inputs.payer_accounts.setdefault(key, set()).add(account)

        inputs.payments.append(
            PaymentRecord(
                id=payment_key,
                customer_id=key,
                customer_name=(
                    inputs.customer_names.get(key or "")
                    or payment.stated_customer_name
                ),
                amount_minor=payment.amount,
                currency=payment.currency,
                status=str(payment.status),
                captured_at=payment.payment_time,
                refunded_minor=payment.amount_refunded,
                account_fingerprint=account,
                reference=payment.reference,
            )
        )

    # --- refunds ------------------------------------------------------------
    refund_rows = await fetch(Refund)
    for refund in refund_rows:
        inputs.refunds.append(
            RefundRecord(
                id=str(refund.id),
                payment_id=str(refund.payment_id) if refund.payment_id else None,
                amount_minor=refund.amount,
                refunded_at=refund.refund_time,
            )
        )

    # --- invoices -----------------------------------------------------------
    # Version count comes from the provenance vault rather than a counter on the
    # invoice: Feature 1 already versions a source record whenever its payload
    # changes, so the vault is the only place that knows an invoice was re-issued.
    invoices = await fetch(Invoice)
    raw_ids = [i.raw_record_id for i in invoices if i.raw_record_id]
    versions: dict[str, int] = {}
    if raw_ids:
        for raw in (
            (
                await session.execute(
                    select(RawRecord).where(
                        RawRecord.workspace_id == workspace_id,
                        RawRecord.id.in_(raw_ids),
                    )
                )
            )
            .scalars()
            .all()
        ):
            versions[str(raw.id)] = raw.version

    for invoice in invoices:
        key = resolve(
            customer_key(
                entity_name.get(str(invoice.customer_entity_id) or ""),
                invoice.stated_customer_name,
            )
        )
        inputs.invoices.append(
            InvoiceRecord(
                id=str(invoice.id),
                number=invoice.invoice_number,
                customer_id=key,
                customer_name=(
                    inputs.customer_names.get(key or "")
                    or invoice.stated_customer_name
                ),
                total_minor=invoice.total,
                issued_on=invoice.issue_date,
                description=_invoice_wording(invoice),
                one_time_hint=invoice.has_one_time_items,
                version_count=versions.get(str(invoice.raw_record_id), 1),
            )
        )

    # --- contracts ----------------------------------------------------------
    for contract in await fetch(Contract):
        if contract.recurring_amount == 0 and contract.one_time_amount == 0:
            continue  # not read by Feature 3 yet; it states nothing to contradict
        allocation = allocate_to_period(
            recurring_minor=contract.recurring_amount,
            one_time_minor=contract.one_time_amount,
            frequency=contract.billing_frequency,
            contract_start=contract.start_date,
            contract_end=contract.end_date,
            period_start=workspace.reporting_period_start,
            period_end=workspace.reporting_period_end,
            currency=contract.currency,
        )
        # Contracts almost never carry a resolved entity: Feature 3 reads them from
        # PDFs, and the only party it has is the name written in the document. Keying
        # them on `customer_entity_id` therefore looked up `None` for all fourteen,
        # so A04 could not fire even with every contract read — the Quantum Retail
        # case, which is the single most valuable finding this feature makes, was
        # unreachable by construction. The stated name is the join that exists.
        inputs.contracts.append(
            ContractRecord(
                id=str(contract.id),
                document_name=contract.document_name,
                customer_id=resolve(
                    customer_key(
                        entity_name.get(str(contract.customer_entity_id) or ""),
                        contract.stated_customer_name,
                    )
                ),
                start_date=contract.start_date,
                end_date=contract.end_date,
                recurring_minor=contract.recurring_amount,
                one_time_minor=contract.one_time_amount,
                future_period_minor=allocation.get("future_period_minor", 0),
                in_period_minor=allocation.get("in_period_minor", 0),
            )
        )

    _align_contract_keys(inputs)

    # --- money flow, for the circular-funds search --------------------------
    _build_flows(inputs, bank_rows=bank_rows, refunds=refund_rows)
    return inputs


def _core_key(key: str) -> str:
    """The first two significant tokens of a party name.

    Enough to recognise that "Quantum Retail Solutions" and "Quantum Retail
    Implementation" are the same company, and deliberately not enough to merge
    "Blue Harbor Analytics" with "Blue Harbour Logistics" — those differ in the
    second token and stay apart, which is the false merge Feature 2 was fixed to
    refuse and this must not reintroduce.
    """
    return " ".join(key.split()[:2])


def _align_contract_keys(inputs: ScanInputs) -> None:
    """Point each contract at the customer key its invoices actually use.

    A04 asks whether an invoice's wording disagrees with its contract, so it needs
    the two to meet under one key. They frequently do not: an invoice carries the
    accounting system's name for the customer and a contract carries whatever the
    PDF's signature block says. On the §15 dataset the invoice resolved to "quantum
    retail implementation" and the contract to "quantum retail solutions", so the
    lookup missed and the single most valuable finding this feature makes — the
    ₹15,00,000 implementation fee invoiced as "Annual subscription" — went
    unreported while three less interesting versions of the same rule fired.

    An exact match wins. Otherwise a contract adopts an invoice's key when they
    share a core name and exactly one such key exists; ambiguity is left alone
    rather than guessed, because a contract attached to the wrong customer would
    make the rule state something false about both.
    """
    invoice_keys = {i.customer_id for i in inputs.invoices if i.customer_id}
    by_core: dict[str, set[str]] = {}
    for key in invoice_keys:
        by_core.setdefault(_core_key(key), set()).add(key)

    for contract in inputs.contracts:
        if not contract.customer_id or contract.customer_id in invoice_keys:
            continue
        candidates = by_core.get(_core_key(contract.customer_id), set())
        if len(candidates) == 1:
            contract.customer_id = next(iter(candidates))


def _invoice_wording(invoice: Invoice) -> str | None:
    """How the invoice describes itself, in the words that reach an ARR claim.

    An invoice has no single description — it has line items — and the line that
    matters to A04 is the one the normaliser already marked as reading one-time. The
    Quantum Retail case is exactly this: "Annual subscription — implementation and
    migration programme" is one line whose wording says recurring and whose substance
    the contract makes one-time. Quoting that line back is what lets a reviewer see
    the disagreement rather than being told about it.
    """
    items = invoice.line_items or []
    for item in items:
        if isinstance(item, dict) and item.get("is_one_time_hint"):
            text = str(item.get("description") or "").strip()
            if text:
                return text[:200]
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("description") or "").strip()
            if text:
                return text[:200]
    return None


def _build_flows(
    inputs: ScanInputs,
    *,
    bank_rows: list[BankTransaction],
    refunds: list[Refund],
) -> None:
    """Total credits and *unexplained* debits per counterparty.

    A refund leaving the account is money going back to where it came from, so it
    forms a loop in the bank statement exactly as a recycled receipt does. The
    difference is that a refund is already accounted for — Feature 1 recorded it,
    Feature 4 pushed it back onto the invoices it settled, and Feature 5 classified
    the item as REFUNDED_OR_REVERSED. Reporting it again as "funds appear to move in
    a circle" would fire on every ordinary reversal, which is precisely how a
    detector teaches a reviewer to scroll past it. Debits matched to a recorded
    refund are therefore excluded from the flow graph, and only unexplained
    outflows can close a loop.

    Parties are keyed on the customer entity Feature 2 resolved, not on the
    counterparty text. The bank narration folds the *purpose* of a transfer into the
    party name — the same counterparty arrives as "APEX FOUNDER HOLDINGS PVT LTD
    ADVANCE" on the way in and "APEX FOUNDER HOLDINGS PVT LTD ADVISORY FEE" on the
    way out — so keying on that string gives one party two identities, and a round
    trip stops being a round trip. Both legs resolve to the same entity.
    """
    unmatched = [(r.amount, r.refund_time.date() if r.refund_time else None) for r in refunds]

    # One key space, resolved in a first pass. Keying each row on its own entity
    # where it happens to have one splits a party in exactly the way the customer
    # keys did: Feature 2 builds identities from money coming *in*, so an outflow
    # never carries an entity, and Apex's credits keyed on `entity:83fc…` while its
    # debits keyed on the bare name. Neither leg could see the other and the round
    # trip vanished. So the entity is resolved per *counterparty name* and then
    # applied to every row bearing that name, inbound or outbound.
    entity_of_name: dict[str, str] = {}
    for row in bank_rows:
        name = normalize_name(row.counterparty) or row.counterparty
        if name and row.customer_entity_id:
            entity_of_name.setdefault(name, f"entity:{row.customer_entity_id}")

    def account_key(row: BankTransaction) -> str | None:
        name = normalize_name(row.counterparty) or row.counterparty
        if name:
            return entity_of_name.get(name, name)
        return f"entity:{row.customer_entity_id}" if row.customer_entity_id else None

    for row in bank_rows:
        account = account_key(row)
        if account is None:
            continue
        inputs.account_labels.setdefault(account, row.counterparty or account)

        if str(row.direction) == "credit":
            inputs.credits[account] = inputs.credits.get(account, 0) + row.amount
            continue

        matched = _match_refund(unmatched, amount=row.amount, on=row.transaction_date)
        if matched is not None:
            unmatched.pop(matched)
            continue
        inputs.unexplained_debits[account] = (
            inputs.unexplained_debits.get(account, 0) + row.amount
        )


def _match_refund(
    unmatched: list[tuple[int, date | None]], *, amount: int, on: date
) -> int | None:
    """Index of a recorded refund this debit settles, if there is one."""
    for index, (refund_amount, refund_date) in enumerate(unmatched):
        if refund_amount != amount:
            continue
        if refund_date is None or abs(on - refund_date) <= REFUND_MATCH_WINDOW:
            return index
    return None


# ---------------------------------------------------------------------------
# Graph — two questions, deliberately two graphs
# ---------------------------------------------------------------------------


def _suspected_duplicates(customer_names: dict[str, str]) -> dict[str, list[str]]:
    """Customer keys that read as the same customer, grouped under a representative.

    A related-party finding is a claim that two *different* parties are connected.
    When entity resolution has not merged two records for one company, the graph
    sees two customers sharing a domain and every payer account, and says exactly
    that — producing "Cobalt Media is connected to Cobalt Media Networks", which is
    true, useless, and indistinguishable at a glance from the founder-linked
    relationship a reviewer needs to act on.

    Measured on the §15 dataset this was not a marginal effect: of eight related-
    party findings, six were a company connected to itself, and the two genuine ones
    — Northstar to the founder's Apex Holdings, and Meridian's parent to its
    subsidiary — were buried among them. CLAUDE.md's known gap that Feature 2
    over-splits is the cause; the consequence lands here, and this feature has to be
    honest about which of the two things it is looking at.

    So: names where one contains the other are treated as one party for detection,
    and reported separately as a merge Feature 2 should re-evaluate — the
    "can request that Feature 2 re-evaluate an entity relationship" path idea_
    features.md §Feature 6 asks for. Containment is deliberately narrow. It catches
    "Northstar Tech" inside "Northstar Technologies" without touching Blue Harbor
    Analytics and Blue Harbour Logistics, which are genuinely different companies
    that Feature 2 already fought to keep apart.
    """
    keys = sorted(customer_names)
    parent: dict[str, str] = {key: key for key in keys}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            if left in right or right in left:
                a, b = find(left), find(right)
                if a != b:
                    parent[b] = a

    groups: dict[str, list[str]] = {}
    for key in keys:
        groups.setdefault(find(key), []).append(key)
    # A group of one is just a customer; only real collisions are duplicates.
    return {root: members for root, members in groups.items() if len(members) > 1}


async def _investigate_graph(
    workspace_id: uuid.UUID, inputs: ScanInputs
) -> graph.GraphInvestigation:
    """Related-party clusters and circular flows, over separate edge sets.

    The two questions need different graphs and merging them destroys both. Every
    account transacts with the company, so a single graph containing both the
    "customers share this payer" edges and the "money moved between us and this
    party" edges puts the company node next to everything — and weakly connected
    components then return one cluster containing the entire workspace. That is the
    Feature 4 candidate-explosion failure in graph form: a result that is technically
    correct and tells a reviewer nothing.

    So: shared-attribute edges answer *who is connected to whom*, money-flow edges
    answer *does money return to where it started*, and each investigation
    contributes only the finding it is competent to make.
    """
    duplicates = inputs.duplicate_groups
    representative = inputs.representative

    attribute_nodes: list[dict[str, str]] = []
    attribute_edges: list[dict[str, str]] = []

    for customer_id, name in sorted(inputs.customer_names.items()):
        if representative.get(customer_id, customer_id) != customer_id:
            continue  # folded into its representative below
        attribute_nodes.append({"id": customer_id, "label": name, "kind": "customer"})

    seen_accounts: set[str] = set()
    for raw_customer_id, accounts in sorted(inputs.payer_accounts.items()):
        customer_id = representative.get(raw_customer_id, raw_customer_id)
        for account in sorted(accounts):
            node_id = f"account:{account}"
            if node_id not in seen_accounts:
                seen_accounts.add(node_id)
                attribute_nodes.append(
                    {
                        "id": node_id,
                        "label": inputs.account_labels.get(account, account),
                        "kind": "account",
                    }
                )
            # One direction only. An edge pair between a customer and its payer
            # would be a two-node cycle to any directed search, and "this company
            # paid its own bill" is not a circular flow.
            attribute_edges.append({"source": customer_id, "target": node_id})

    domain_owners: dict[str, list[str]] = {}
    for raw_customer_id, domains in sorted(inputs.customer_domains.items()):
        customer_id = representative.get(raw_customer_id, raw_customer_id)
        for domain in domains:
            owners = domain_owners.setdefault(domain.lower(), [])
            if customer_id not in owners:
                owners.append(customer_id)
    for domain, owners in sorted(domain_owners.items()):
        if len(owners) < 2:
            continue  # a domain one customer owns links nothing
        node_id = f"domain:{domain}"
        attribute_nodes.append({"id": node_id, "label": domain, "kind": "domain"})
        for customer_id in owners:
            attribute_edges.append({"source": customer_id, "target": node_id})

    clusters = await graph.investigate(
        str(workspace_id), attribute_nodes, attribute_edges
    )

    # --- money flow --------------------------------------------------------
    company_id = "company"
    flow_nodes: list[dict[str, str]] = [
        {"id": company_id, "label": "This company", "kind": "company"}
    ]
    flow_edges: list[dict[str, str]] = []
    involved = sorted(set(inputs.credits) | set(inputs.unexplained_debits))
    for account in involved:
        node_id = f"account:{account}"
        flow_nodes.append(
            {
                "id": node_id,
                "label": inputs.account_labels.get(account, account),
                "kind": "account",
            }
        )
        if inputs.credits.get(account):
            flow_edges.append({"source": node_id, "target": company_id})
        if inputs.unexplained_debits.get(account):
            flow_edges.append({"source": company_id, "target": node_id})

    flows = await graph.investigate(str(workspace_id), flow_nodes, flow_edges)

    merged = graph.GraphInvestigation(
        gds_available=clusters.gds_available,
        method=clusters.method,
        clusters=clusters.clusters,
        cycles=flows.cycles,
        findings=[
            *[f for f in clusters.findings if f.rule_id == "A10_RELATED_PARTY_REVENUE"],
            *[f for f in flows.findings if f.rule_id == "A11_CIRCULAR_FUNDS"],
        ],
    )

    # The unmerged records, reported as what they are rather than as a relationship
    # between two parties. This is the request back to Feature 2, and it matters to
    # the figures on the page: two records for one company halve its measured
    # concentration, which is the same understatement a wrong merge causes, reached
    # from the opposite direction.
    for members in sorted(duplicates.values()):
        names = [inputs.customer_names.get(key, key) for key in members]
        merged.findings.append(
            rules._finding(
                "A13_SUSPECTED_DUPLICATE_CUSTOMER",
                AnomalySeverity.LOW,
                explanation=(
                    f"{len(members)} customer records read as the same customer: "
                    + ", ".join(names[:5])
                    + ". Their revenue is currently counted under separate customers."
                ),
                observed_value=f"{len(members)} records, one apparent customer",
                baseline_value="one record per customer once identity is resolved",
                customer_id=members[0],
                related_records=[
                    {"type": "customer", "id": inputs.key_to_entity.get(key, key)}
                    for key in members
                ],
                caveats=[
                    ("This is an identity-resolution gap rather than a financial "
                    "anomaly, and it is reported so concentration is read with the "
                    "split in mind."),
                    ("Names that contain one another are not proof of one company; "
                    "two genuinely different firms can share a prefix."),
                ],
            )
        )

    # Feature 2 may already have concluded a customer is a related party from
    # identifiers the payment graph never sees — a customer on the founder's own
    # domain, say. That conclusion belongs in this feature's output too; it is the
    # same finding reached by different evidence.
    known = {
        member["id"]
        for cluster in merged.clusters
        for member in cluster["members"]
    }
    for customer_id, reasons in sorted(inputs.related_parties.items()):
        if customer_id in known:
            continue
        name = inputs.customer_names.get(customer_id, customer_id)
        merged.findings.append(
            rules._finding(
                "A10_RELATED_PARTY_REVENUE",
                AnomalySeverity.MEDIUM,
                explanation=(
                    f"{name} was identified as a potentially related party during "
                    f"identity resolution"
                    + (f": {reasons[0]}." if reasons else ".")
                ),
                observed_value="related-party indicator on the customer record",
                baseline_value="customers are normally unconnected third parties",
                customer_id=customer_id,
                related_records=[{"type": "customer", "id": customer_id}],
                caveats=[
                    ("Related-party revenue is not improper. It is simply not "
                    "third-party validation of demand, so it is worth separating "
                    "from arm's-length revenue."),
                    ("This indicator comes from shared identifiers and cannot "
                    "establish legal ownership."),
                ],
            )
        )
    return merged


# ---------------------------------------------------------------------------
# Persistence — findings are recomputed, verdicts are not
# ---------------------------------------------------------------------------


def fingerprint(rule_id: str, related_records: list[dict[str, Any]]) -> str:
    """Identify a finding by what it is, so the same one survives a re-scan.

    Row IDs cannot do this job: a scan replaces its findings, and matching on the
    database id of a row that was just deleted matches nothing. A finding is the
    rule that fired plus the records it fired on, which is stable across runs for
    as long as the underlying evidence is.
    """
    parts = sorted(
        f"{record.get('type', '?')}:{record.get('id') or record.get('name') or ''}"
        for record in related_records
    )
    return f"{rule_id}|{';'.join(parts)}"


async def _persist(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    packets: list[explain.Packet],
    key_to_entity: dict[str, str],
) -> tuple[int, int, int]:
    """Write findings, carrying any human verdict onto the matching new row."""
    existing = list(
        (
            await session.execute(
                select(Anomaly).where(Anomaly.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    previous = {
        fingerprint(row.rule_id, row.related_records or []): row for row in existing
    }

    written = 0
    preserved = 0
    seen: set[str] = set()

    for packet in packets:
        finding = packet.finding
        key = fingerprint(finding.rule_id, finding.related_records)
        if key in seen:
            # Two detectors reaching the same conclusion about the same records is
            # one finding, not two. Reporting it twice would double every count a
            # reviewer uses to decide where to start.
            continue
        seen.add(key)

        row = previous.get(key)
        if row is None:
            row = Anomaly(workspace_id=workspace_id, rule_id=finding.rule_id)
            session.add(row)
            written += 1
        elif row.is_false_positive is not None or row.status != ReviewStatus.OPEN:
            preserved += 1

        row.title = finding.title[:300]
        row.severity = finding.severity
        row.customer_entity_id = _as_uuid(
            key_to_entity.get(finding.customer_id or "", finding.customer_id)
        )
        row.related_records = finding.related_records
        row.observed_value = (finding.observed_value or "")[:200] or None
        row.baseline_value = (finding.baseline_value or "")[:200] or None
        row.explanation = _with_narrative(packet)
        row.required_check = finding.required_check
        row.caveats = packet.caveats
        row.graph_path = finding.graph_path
        row.model_version = finding.model_version
        row.model_score = finding.model_score
        # `status` and `is_false_positive` are deliberately never assigned here.
        # They belong to the reviewer, and a scan that reset them would erase the
        # only labels sub-feature 7 has to measure precision against.

    retired = 0
    for key, row in previous.items():
        if key in seen:
            continue
        # A finding that no longer fires is removed rather than left open — the
        # evidence behind it changed, and an indicator a reviewer cannot reproduce
        # wastes the time this feature exists to direct. A labelled one is kept, so
        # the precision record is not quietly improved by deleting its own misses.
        if row.is_false_positive is not None:
            continue
        await session.delete(row)
        retired += 1

    await session.flush()
    return written, preserved, retired


def _with_narrative(packet: explain.Packet) -> str:
    """The deterministic explanation, with model prose appended only if it exists."""
    body = packet.finding.explanation
    if packet.narrative:
        body += (
            "\n\n"
            + packet.narrative.get("summary", "")
            + " "
            + packet.narrative.get("why_it_matters", "")
            + " "
            + packet.narrative.get("what_would_resolve_it", "")
        ).rstrip()
    return body


def _as_uuid(value: str | None) -> uuid.UUID | None:
    """Findings carry a name key; only a resolved one maps to a foreign key.

    An unresolved customer stores `None` rather than failing the write. The finding
    still names the customer in its text and its related records — losing the whole
    anomaly because identity resolution has not caught up would be a poor trade.
    """
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _route_to_review(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    packets: list[explain.Packet],
) -> int:
    """Send material findings to Feature 7, linked to the anomaly they came from."""
    created = 0
    anomalies = list(
        (
            await session.execute(
                select(Anomaly).where(Anomaly.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    by_key = {
        fingerprint(row.rule_id, row.related_records or []): row for row in anomalies
    }

    for packet in packets:
        finding = packet.finding
        if finding.severity not in ROUTED_SEVERITIES:
            continue
        row = by_key.get(fingerprint(finding.rule_id, finding.related_records))
        if row is None:
            continue

        existing = (
            await session.execute(
                select(ReviewItem)
                .where(
                    ReviewItem.workspace_id == workspace_id,
                    ReviewItem.anomaly_id == row.id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()

        title = f"{finding.title}: {finding.observed_value or finding.rule_id}"
        packet_body = packet.as_dict()
        if existing is not None:
            existing.title = title[:300]
            existing.detail = finding.explanation[:4000]
            existing.evidence_packet = packet_body
            continue

        session.add(
            ReviewItem(
                workspace_id=workspace_id,
                category=(
                    "related_party"
                    if finding.rule_id
                    in {"A10_RELATED_PARTY_REVENUE", "A11_CIRCULAR_FUNDS"}
                    else "agent_disagreement"
                ),
                title=title[:300],
                detail=finding.explanation[:4000],
                severity=finding.severity,
                anomaly_id=row.id,
                evidence_packet=packet_body,
            )
        )
        created += 1

    await session.flush()
    return created


# ---------------------------------------------------------------------------
# Sub-feature 7 — feedback, precision, drift and the model gate
# ---------------------------------------------------------------------------


async def measure_precision(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Precision per rule from reviewer labels, and the ML decision that follows.

    Precision only — recall is not measurable here and claiming it would be a lie.
    Recall needs to know about the anomalies that were never flagged, and nobody
    labels the records a detector stayed silent on. What a reviewer *can* tell us is
    whether the things they were shown were worth their time, and that is exactly
    the review-budget question core_resoruces.md ranks the precision-recall harness
    for.
    """
    rows = list(
        (
            await session.execute(
                select(Anomaly).where(Anomaly.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )

    per_rule: dict[str, dict[str, int]] = {}
    ml_confirmed = ml_false = 0
    labelled = 0

    for row in rows:
        bucket = per_rule.setdefault(
            row.rule_id, {"total": 0, "labelled": 0, "confirmed": 0, "false_positive": 0}
        )
        bucket["total"] += 1
        if row.is_false_positive is None:
            continue
        labelled += 1
        bucket["labelled"] += 1
        if row.is_false_positive:
            bucket["false_positive"] += 1
        else:
            bucket["confirmed"] += 1
        if row.model_version:
            if row.is_false_positive:
                ml_false += 1
            else:
                ml_confirmed += 1

    for bucket in per_rule.values():
        bucket["precision"] = (
            round(bucket["confirmed"] / bucket["labelled"], 3)
            if bucket["labelled"]
            else None
        )

    ml_labelled = ml_confirmed + ml_false
    ml_precision = round(ml_confirmed / ml_labelled, 3) if ml_labelled else None

    if ml_labelled < MIN_LABELS_FOR_GATE:
        ml_enabled = True
        ml_reason = (
            f"{ml_labelled} of {MIN_LABELS_FOR_GATE} labelled model findings needed "
            f"before precision can be measured — the model runs, and its findings "
            f"stay LOW severity and out of the review queue until it has a record"
        )
    elif ml_precision is not None and ml_precision < ML_PRECISION_FLOOR:
        ml_enabled = False
        ml_reason = (
            f"model precision {ml_precision:.0%} over {ml_labelled} labelled "
            f"findings is below the {ML_PRECISION_FLOOR:.0%} floor — it is costing "
            f"more review time than it saves, so it is switched off"
        )
    else:
        ml_enabled = True
        ml_reason = (
            f"model precision {ml_precision:.0%} over {ml_labelled} labelled findings"
        )

    return {
        "total_findings": len(rows),
        "labelled": labelled,
        "overall_precision": (
            round(
                sum(b["confirmed"] for b in per_rule.values()) / labelled, 3
            )
            if labelled
            else None
        ),
        "per_rule": per_rule,
        "ml_labelled": ml_labelled,
        "ml_precision": ml_precision,
        "ml_enabled": ml_enabled,
        "ml_reason": ml_reason,
        "min_labels_for_gate": MIN_LABELS_FOR_GATE,
        "precision_floor": ML_PRECISION_FLOOR,
        "note": (
            "Precision only. Recall would require labelling the records no detector "
            "flagged, which nobody has been asked to do."
        ),
    }


async def record_feedback(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    anomaly_id: uuid.UUID,
    is_false_positive: bool,
    actor_id: str,
    note: str | None = None,
) -> Anomaly | None:
    """A reviewer's verdict on one finding. This is the label everything else measures."""
    row = await session.get(Anomaly, anomaly_id)
    if row is None or row.workspace_id != workspace_id:
        return None

    before = {"status": str(row.status), "is_false_positive": row.is_false_positive}
    row.is_false_positive = is_false_positive
    row.status = ReviewStatus.DISMISSED if is_false_positive else ReviewStatus.RESOLVED

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="human",
        actor_id=actor_id,
        action="anomaly.feedback",
        object_type="anomaly",
        object_id=str(anomaly_id),
        before_state=before,
        after_state={
            "status": str(row.status),
            "is_false_positive": is_false_positive,
        },
        reason=note or ("marked a false positive" if is_false_positive else "confirmed"),
    )
    await session.flush()

    emit(
        EventKind.RULE,
        f"Reviewer marked {row.rule_id} as "
        f"{'a false positive' if is_false_positive else 'confirmed'} — precision "
        f"and the model gate are measured from labels like this one",
        workspace_id=str(workspace_id),
        feature=6,
    )
    return row
