"""Contract intelligence orchestration — F3 sub-features 6, 7, 9 plus the pipeline.

Runs the documented Feature 3 workflow:

    Drive retrieval → safe file checks → digital text or layout OCR → page-aware
    clauses → targeted retrieval → schema-constrained Contract Reader →
    deterministic dates/money → amendment precedence → citation verification →
    accepted obligations to Features 4-5, ambiguous clauses to Feature 7

The division of labour is the point. The model reads *language*; every number and
date it produces is then re-derived, bounded and allocated by deterministic code
(`app.core.money`). idea_features.md §14 is explicit that AI must not perform
calculations ordinary code can do reliably — so the model never computes a period
allocation, it only says what the contract states.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.core.money import Money, days_inclusive, overlap_days, prorate_for_period
from app.features.contracts import extraction as extract
from app.features.contracts import parsing
from app.models import Citation, Contract, RawRecord, ReviewItem, Workspace
from app.models.enums import AnomalySeverity, BillingFrequency
from app.services import vault
from app.services.audit import record_audit_event


@dataclass
class ContractOutcome:
    document_name: str
    contract_id: str | None = None
    parsed_chars: int = 0
    ocr_applied: bool = False
    clauses: int = 0
    citations_verified: int = 0
    citations_total: int = 0
    needs_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    recurring_minor: int = 0
    one_time_minor: int = 0
    future_period_minor: int = 0
    in_period_minor: int = 0
    error: str | None = None


@dataclass
class ContractRunResult:
    processed: int = 0
    extracted: int = 0
    needs_review: int = 0
    failed: int = 0
    ocr_used: int = 0
    amendments_resolved: int = 0
    outcomes: list[ContractOutcome] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "extracted": self.extracted,
            "needs_review": self.needs_review,
            "failed": self.failed,
            "ocr_used": self.ocr_used,
            "amendments_resolved": self.amendments_resolved,
            "outcomes": [
                {
                    "document_name": o.document_name,
                    "contract_id": o.contract_id,
                    "ocr_applied": o.ocr_applied,
                    "clauses": o.clauses,
                    "citations": f"{o.citations_verified}/{o.citations_total}",
                    "needs_review": o.needs_review,
                    "review_reasons": o.review_reasons,
                    "unknown_fields": o.unknown_fields,
                    "recurring_minor": o.recurring_minor,
                    "one_time_minor": o.one_time_minor,
                    "future_period_minor": o.future_period_minor,
                    "in_period_minor": o.in_period_minor,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }


# ---------------------------------------------------------------------------
# Sub-feature 6 — deterministic period allocation
# ---------------------------------------------------------------------------


def annualised_recurring(amount_minor: int, frequency: str, currency: str) -> int:
    """Normalise a per-period fee to an annual figure.

    idea_features.md §8 requires annualisation rules to be displayed rather than
    hidden, so this is a plain multiplication by periods-per-year and nothing else.
    A frequency that does not recur annualises to zero, not to itself.
    """
    try:
        periods = BillingFrequency(frequency).periods_per_year
    except ValueError:
        return 0
    if periods is None:
        return 0
    return amount_minor * periods


def allocate_to_period(
    *,
    recurring_minor: int,
    one_time_minor: int,
    frequency: str,
    contract_start: date | None,
    contract_end: date | None,
    period_start: date,
    period_end: date,
    currency: str,
) -> dict[str, Any]:
    """Split contract value into in-period, future-period and out-of-period amounts.

    This is the calculation that stops a future contract inflating current ARR
    (spec §14) and the one that separates a ₹10 lakh contract into its genuinely
    recurring part. Every input and the resulting day ratio are returned so a
    reviewer can check the arithmetic rather than trust it.
    """
    detail: dict[str, Any] = {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "contract_start": contract_start.isoformat() if contract_start else None,
        "contract_end": contract_end.isoformat() if contract_end else None,
        "frequency": frequency,
        "currency": currency,
    }

    if contract_start is None or contract_end is None:
        detail["rule"] = "DATES_UNKNOWN"
        detail["explanation"] = (
            "Contract dates could not be established, so no amount can be allocated "
            "to the reporting period."
        )
        return {
            "in_period_minor": 0,
            "future_period_minor": 0,
            "annualised_recurring_minor": 0,
            "detail": detail,
        }

    annualised = annualised_recurring(recurring_minor, frequency, currency)
    term_days = days_inclusive(contract_start, contract_end)
    covered = overlap_days(contract_start, contract_end, period_start, period_end)
    detail["term_days"] = term_days
    detail["days_in_period"] = covered
    detail["annualised_recurring_minor"] = annualised

    if covered == 0:
        # spec §14: a contract outside the reporting period supports no revenue.
        entirely_future = contract_start > period_end
        detail["rule"] = "OUTSIDE_PERIOD_FUTURE" if entirely_future else "OUTSIDE_PERIOD_PAST"
        detail["explanation"] = (
            f"The contract term {contract_start} to {contract_end} does not overlap "
            f"the reporting period {period_start} to {period_end}, so none of it "
            f"supports revenue in this period."
        )
        # The total contract value is still reported, as future-period value.
        total = _total_contract_value(recurring_minor, frequency, term_days)
        return {
            "in_period_minor": 0,
            "future_period_minor": (total + one_time_minor) if entirely_future else 0,
            "annualised_recurring_minor": annualised,
            "detail": detail,
        }

    total_recurring = _total_contract_value(recurring_minor, frequency, term_days)
    prorated = prorate_for_period(
        Money(total_recurring, currency),
        contract_start, contract_end, period_start, period_end,
    )
    detail["day_ratio"] = f"{covered}/{term_days}"
    detail["total_recurring_over_term_minor"] = total_recurring
    detail["rule"] = "DAY_PRORATED" if covered < term_days else "FULLY_IN_PERIOD"
    detail["explanation"] = (
        f"{covered} of the contract's {term_days} days fall inside the reporting "
        f"period, so {covered}/{term_days} of the recurring value is allocated to it."
    )

    # A one-time fee belongs to the period in which it becomes payable, which is the
    # contract start — it is not spread across the term.
    one_time_in_period = (
        one_time_minor if period_start <= contract_start <= period_end else 0
    )
    detail["one_time_in_period"] = one_time_in_period > 0
    future_recurring = max(0, total_recurring - prorated.minor)

    return {
        "in_period_minor": prorated.minor + one_time_in_period,
        "future_period_minor": future_recurring,
        "annualised_recurring_minor": annualised,
        "detail": detail,
    }


def _total_contract_value(recurring_minor: int, frequency: str, term_days: int) -> int:
    """Total recurring value across the whole contract term."""
    try:
        periods = BillingFrequency(frequency).periods_per_year
    except ValueError:
        periods = None
    if periods is None or recurring_minor == 0:
        return recurring_minor
    # Number of billing periods the term actually spans.
    period_length_days = 365 / periods
    occurrences = max(1, round(term_days / period_length_days))
    return recurring_minor * occurrences


# ---------------------------------------------------------------------------
# Sub-feature 7 — amendment precedence
# ---------------------------------------------------------------------------


def _amendment_group_key(contract: Contract) -> Any:
    """Which contracts could plausibly amend one another.

    Grouping on `customer_entity_id` alone was wrong: Feature 3 runs *before*
    identity resolution has necessarily linked a contract to a canonical customer,
    so every contract shared a `None` key and no amendment was ever matched. The
    party name extracted from the contract text is available at this point and is a
    better key anyway — it comes from the document itself rather than from a
    downstream inference.
    """
    if contract.customer_entity_id is not None:
        return contract.customer_entity_id
    stated = (contract.stated_customer_name or "").strip().lower()
    if stated:
        from app.features.identity.identifiers import normalize_name

        normalised = normalize_name(stated)
        if normalised:
            return f"name:{normalised}"
    # Fall back to the filename stem, which is how amendments are usually named.
    import re

    # Drop the extension first: otherwise "Acme_Agreement.pdf" normalises to
    # "acme agreement pdf" while "Acme_Agreement_Amendment_1.pdf" normalises to
    # "acme agreement", and the two never group together.
    stem = contract.document_name.rsplit(".", 1)[0]
    stem = re.split(
        r"[_-]?(amendment|addendum|amended)", stem, flags=re.IGNORECASE
    )[0]
    stem = re.sub(r"[^a-z0-9]+", " ", stem.lower()).strip()
    return f"file:{stem}" if stem else None


def resolve_amendment_chain(contracts: list[Contract]) -> list[dict[str, Any]]:
    """Link amendments to the agreements they modify.

    Matching is by customer plus document naming, and the result is *proposed*, not
    applied silently: an amendment that changes a price mid-period changes revenue,
    so the link and its effective date are recorded for review. The LLM may flag a
    document as an amendment; this decides what it supersedes.
    """
    resolutions: list[dict[str, Any]] = []
    by_customer: dict[Any, list[Contract]] = {}
    for contract in contracts:
        by_customer.setdefault(_amendment_group_key(contract), []).append(contract)

    for customer_id, group in by_customer.items():
        if customer_id is None or len(group) < 2:
            continue
        amendments = [c for c in group if c.is_amendment]
        originals = [c for c in group if not c.is_amendment]
        if not amendments or not originals:
            continue

        for amendment in amendments:
            # The original is the most recent agreement starting before the
            # amendment takes effect.
            effective = amendment.effective_from or amendment.start_date
            candidates = [
                original
                for original in originals
                if original.start_date
                and (effective is None or original.start_date <= effective)
            ]
            if not candidates:
                continue
            superseded = max(candidates, key=lambda c: c.start_date)
            resolutions.append(
                {
                    "amendment_id": str(amendment.id),
                    "amendment_name": amendment.document_name,
                    "supersedes_id": str(superseded.id),
                    "supersedes_name": superseded.document_name,
                    "effective_from": effective.isoformat() if effective else None,
                    "previous_recurring_minor": superseded.recurring_amount,
                    "new_recurring_minor": amendment.recurring_amount,
                    "explanation": (
                        f"'{amendment.document_name}' amends "
                        f"'{superseded.document_name}'"
                        + (f" with effect from {effective}" if effective else "")
                        + ". Revenue before that date uses the original terms; "
                        "revenue after uses the amended terms."
                    ),
                }
            )
    return resolutions


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


async def process_contracts(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: str | None = None,
    limit: int | None = None,
) -> ContractRunResult:
    """Read every vaulted contract and populate its structured terms."""
    run_id = run_id or uuid.uuid4().hex[:12]
    result = ContractRunResult()

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return result

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
    if limit:
        contracts = contracts[:limit]

    emit(
        EventKind.AGENT_STEP,
        f"Contract Reader Agent starting on {len(contracts)} documents",
        workspace_id=str(workspace_id),
        feature=3,
        run_id=run_id,
    )

    for contract in contracts:
        outcome = await _process_one(
            session,
            workspace=workspace,
            contract=contract,
            run_id=run_id,
        )
        result.outcomes.append(outcome)
        result.processed += 1
        if outcome.error:
            result.failed += 1
        elif outcome.needs_review:
            result.needs_review += 1
        else:
            result.extracted += 1
        if outcome.ocr_applied:
            result.ocr_used += 1

    await session.flush()

    # Amendment precedence, once every contract has its dates.
    refreshed = (
        (
            await session.execute(
                select(Contract).where(Contract.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )
    for resolution in resolve_amendment_chain(refreshed):
        amendment = next(
            (c for c in refreshed if str(c.id) == resolution["amendment_id"]), None
        )
        if amendment is None:
            continue
        amendment.supersedes_contract_id = uuid.UUID(resolution["supersedes_id"])
        result.amendments_resolved += 1
        await _queue_review(
            session,
            workspace_id=workspace_id,
            category="clause_conflict",
            title=f"Amendment changes pricing: {resolution['amendment_name']}",
            detail=resolution["explanation"],
            severity=AnomalySeverity.MEDIUM,
            packet=resolution,
        )
        emit(
            EventKind.RULE,
            f"Contract Amendment Agent: {resolution['amendment_name']} supersedes "
            f"{resolution['supersedes_name']}",
            workspace_id=str(workspace_id),
            feature=3,
            severity=Severity.INFO,
            run_id=run_id,
        )

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id="contract_reader",
        action="contracts.processed",
        object_type="workspace",
        object_id=str(workspace_id),
        after_state={
            k: v for k, v in result.as_dict().items() if k != "outcomes"
        },
        reason=f"contract intelligence run {run_id}",
    )

    emit(
        EventKind.RESULT,
        f"Feature 3 complete: {result.extracted} contracts extracted, "
        f"{result.needs_review} need review, {result.ocr_used} required OCR, "
        f"{result.amendments_resolved} amendments resolved",
        workspace_id=str(workspace_id),
        feature=3,
        severity=Severity.SUCCESS,
        run_id=run_id,
    )
    return result


async def _process_one(
    session: AsyncSession,
    *,
    workspace: Workspace,
    contract: Contract,
    run_id: str,
) -> ContractOutcome:
    """Parse, extract, verify and persist one contract."""
    outcome = ContractOutcome(document_name=contract.document_name)
    workspace_id = workspace.id

    raw = (
        await session.get(RawRecord, contract.raw_record_id)
        if contract.raw_record_id
        else None
    )
    if raw is None or not raw.storage_key:
        outcome.error = "no stored document bytes for this contract"
        return outcome

    try:
        content = vault.read_object(raw.storage_key)
    except Exception as exc:
        outcome.error = f"could not read the stored document: {exc}"
        return outcome

    # --- parse ---------------------------------------------------------
    try:
        parsed = parsing.parse_document(
            content, contract.document_name, workspace_id=str(workspace_id)
        )
    except parsing.DocumentError as exc:
        outcome.error = str(exc)
        contract.needs_human_review = True
        contract.review_reasons = [f"Document could not be parsed: {exc}"]
        await _queue_review(
            session,
            workspace_id=workspace_id,
            category="unreadable_contract",
            title=f"Unreadable contract: {contract.document_name}",
            detail=str(exc),
            severity=AnomalySeverity.HIGH,
            packet={"document": contract.document_name, "error": str(exc)},
        )
        return outcome

    outcome.parsed_chars = parsed.total_chars
    outcome.ocr_applied = parsed.ocr_applied
    contract.is_scanned = parsed.is_scanned
    contract.ocr_applied = parsed.ocr_applied
    contract.page_count = parsed.page_count

    clauses = parsing.segment_clauses(parsed)
    outcome.clauses = len(clauses)

    # --- extract -------------------------------------------------------
    extraction = await extract.extract_terms(
        parsed,
        clauses,
        filename=contract.document_name,
        workspace_id=str(workspace_id),
        run_id=run_id,
    )
    outcome.citations_total = len(extraction.citations)
    outcome.citations_verified = sum(1 for c in extraction.citations if c.verified)
    outcome.unknown_fields = extraction.unknown_fields
    outcome.needs_review = extraction.needs_review
    outcome.review_reasons = extraction.review_reasons

    if extraction.terms is None:
        contract.needs_human_review = True
        contract.review_reasons = extraction.review_reasons
        contract.unknown_fields = ["terms_not_extracted"]
        await _queue_review(
            session,
            workspace_id=workspace_id,
            category="unreadable_contract",
            title=f"Terms not extracted: {contract.document_name}",
            detail="; ".join(extraction.review_reasons) or "extraction produced nothing",
            severity=AnomalySeverity.HIGH,
            packet={"document": contract.document_name},
        )
        return outcome

    terms = extraction.terms

    # --- deterministic conversion --------------------------------------
    # The model read the words; code converts them. A misread magnitude cannot
    # enter the ledger through a plausible-looking number.
    currency = (terms.currency.value or workspace.base_currency or "INR").upper()[:3]
    recurring = extract.parse_amount(terms.recurring_amount.value, currency) or 0
    one_time = extract.parse_amount(terms.one_time_amount.value, currency) or 0
    start = extract.parse_date(terms.contract_start.value)
    end = extract.parse_date(terms.contract_end.value)
    frequency = extract.parse_frequency(terms.billing_frequency.value)

    allocation = allocate_to_period(
        recurring_minor=recurring,
        one_time_minor=one_time,
        frequency=frequency,
        contract_start=start,
        contract_end=end,
        period_start=workspace.reporting_period_start,
        period_end=workspace.reporting_period_end,
        currency=currency,
    )

    outcome.recurring_minor = recurring
    outcome.one_time_minor = one_time
    outcome.in_period_minor = allocation["in_period_minor"]
    outcome.future_period_minor = allocation["future_period_minor"]

    # --- persist -------------------------------------------------------
    contract.stated_customer_name = terms.customer_legal_name.value
    contract.start_date = start
    contract.end_date = end
    contract.billing_frequency = frequency
    contract.currency = currency
    contract.recurring_amount = recurring
    contract.one_time_amount = one_time
    contract.future_period_amount = allocation["future_period_minor"]
    contract.auto_renewal = _parse_bool(terms.auto_renewal.value)
    contract.termination_notice_days = _parse_int(terms.termination_notice_days.value)
    contract.refund_terms = terms.refund_terms.value
    contract.is_amendment = terms.is_amendment
    contract.effective_from = start
    contract.extraction_confidence = extraction.citation_rate
    contract.unknown_fields = extraction.unknown_fields
    contract.needs_human_review = extraction.needs_review
    contract.review_reasons = extraction.review_reasons
    outcome.contract_id = str(contract.id)

    # Replace citations so a re-run does not accumulate them.
    existing = (
        (
            await session.execute(
                select(Citation).where(Citation.contract_id == contract.id)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await session.delete(row)

    for citation in extraction.citations:
        session.add(
            Citation(
                workspace_id=workspace_id,
                contract_id=contract.id,
                field_name=citation.field_name,
                field_value=citation.value,
                page_number=citation.page,
                quote=citation.quote[:2000],
                quote_hash=citation.quote_hash,
                span_start=citation.span_start,
                span_end=citation.span_end,
                bbox=citation.bbox,
                verified=citation.verified,
                verification_note=citation.note or None,
            )
        )

    if extraction.needs_review:
        await _queue_review(
            session,
            workspace_id=workspace_id,
            category="clause_conflict",
            title=f"Contract needs review: {contract.document_name}",
            detail="; ".join(extraction.review_reasons),
            severity=AnomalySeverity.HIGH,
            packet={
                "document": contract.document_name,
                "reasons": extraction.review_reasons,
                "unknown_fields": extraction.unknown_fields,
                "contradiction": terms.contradiction_detail,
                "allocation": allocation["detail"],
            },
        )

    await session.flush()
    return outcome


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "1"}:
        return True
    if text in {"false", "no", "n", "0"}:
        return False
    return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    import re

    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


async def _queue_review(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    category: str,
    title: str,
    detail: str,
    severity: AnomalySeverity,
    packet: dict[str, Any],
) -> None:
    """Add a review item, deduplicated by title so re-runs do not pile up."""
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
            workspace_id=workspace_id,
            category=category,
            title=title[:300],
            detail=detail[:4000],
            severity=severity,
            evidence_packet=packet,
        )
    )
