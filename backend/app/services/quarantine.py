"""Data Quality Agent — Feature 1, sub-feature 7.

PROJECT_WORKFLOW.md states the handoff rule plainly: *quarantined evidence never
enters identity, contract or cash processing*. This module is that gate.

The design choice that matters: a malformed record is **quarantined, not dropped and
not fixed**. Dropping it makes the totals quietly wrong — a reviewer sees a clean
report with 12 invoices silently missing. Guessing a repair is worse. Quarantining
keeps it visible and countable, so "we could not read 12 of your 50 invoices" becomes
a finding on the dashboard rather than an invisible gap in the evidence.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import EventKind, Severity, emit
from app.models import QuarantinedRecord
from app.models.enums import QuarantineReason, RecordType, SourceSystem
from app.schemas.canonical import RECORD_MODEL, CanonicalBase


class ValidationOutcome:
    """Either a validated canonical record, or the reason it was rejected."""

    __slots__ = ("record", "reason", "detail", "errors")

    def __init__(
        self,
        record: CanonicalBase | None = None,
        *,
        reason: QuarantineReason | None = None,
        detail: str = "",
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.record = record
        self.reason = reason
        self.detail = detail
        self.errors = errors or []

    @property
    def ok(self) -> bool:
        return self.record is not None


# Maps a Pydantic error location/message onto a specific quarantine reason, so the
# dashboard can say "3 invalid currencies" rather than "3 validation errors".
def _classify(errors: list[dict[str, Any]]) -> tuple[QuarantineReason, str]:
    joined = " ".join(
        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', '')}"
        for err in errors
    ).lower()

    if "missing" in joined or "required" in joined:
        return QuarantineReason.MISSING_REQUIRED_FIELD, "required field absent"
    if "currency" in joined:
        return QuarantineReason.INVALID_CURRENCY, "currency code not valid ISO-4217"
    if "amount" in joined or "total" in joined or "decimal" in joined or "minor" in joined:
        return QuarantineReason.INVALID_AMOUNT, "amount could not be parsed or is inconsistent"
    if "date" in joined or "time" in joined:
        return QuarantineReason.INVALID_DATE, "date could not be parsed"
    return QuarantineReason.SCHEMA_INVALID, "record does not match the canonical schema"


def validate_record(record_type: RecordType, payload: dict[str, Any]) -> ValidationOutcome:
    """Validate one normalised payload against its canonical schema."""
    model = RECORD_MODEL.get(record_type)
    if model is None:
        return ValidationOutcome(
            reason=QuarantineReason.SCHEMA_INVALID,
            detail=f"no canonical schema registered for record type {record_type!r}",
        )
    try:
        return ValidationOutcome(model.model_validate(payload))
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        reason, detail = _classify(exc.errors())
        return ValidationOutcome(reason=reason, detail=detail, errors=errors)


async def quarantine(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_system: SourceSystem,
    record_type: RecordType,
    reason: QuarantineReason,
    detail: str,
    payload: dict[str, Any],
    source_id: str | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> QuarantinedRecord:
    """Record a rejection so it stays visible and countable."""
    entry = QuarantinedRecord(
        workspace_id=workspace_id,
        source_system=source_system,
        record_type=record_type,
        source_id=source_id,
        reason=reason,
        detail=detail,
        validation_errors=errors or [],
        payload=payload,
    )
    session.add(entry)
    await session.flush()

    emit(
        EventKind.ERROR,
        f"Quarantined {record_type} {source_id or '<no id>'}: {reason} — {detail}",
        workspace_id=str(workspace_id),
        severity=Severity.WARNING,
        feature=1,
        source_system=str(source_system),
        errors=(errors or [])[:5],
    )
    return entry


async def quarantine_summary(
    session: AsyncSession, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """Counts by reason and by source, for the dashboard and the report's gaps list."""
    rows = (
        await session.execute(
            select(
                QuarantinedRecord.reason,
                QuarantinedRecord.source_system,
                QuarantinedRecord.record_type,
                func.count().label("count"),
            )
            .where(
                QuarantinedRecord.workspace_id == workspace_id,
                QuarantinedRecord.resolved_at.is_(None),
            )
            .group_by(
                QuarantinedRecord.reason,
                QuarantinedRecord.source_system,
                QuarantinedRecord.record_type,
            )
        )
    ).all()

    by_reason: dict[str, int] = {}
    by_source: dict[str, int] = {}
    total = 0
    for reason, source_system, _record_type, count in rows:
        by_reason[str(reason)] = by_reason.get(str(reason), 0) + count
        by_source[str(source_system)] = by_source.get(str(source_system), 0) + count
        total += count

    return {"total": total, "by_reason": by_reason, "by_source": by_source}


# ---------------------------------------------------------------------------
# Cross-record quality checks
# ---------------------------------------------------------------------------


def detect_near_duplicates(
    records: list[CanonicalBase], *, amount_attr: str = "amount_minor"
) -> list[tuple[str, str, str]]:
    """Flag records that look like the same transaction recorded twice.

    Returns `(source_id_a, source_id_b, reason)`. This is a *data quality* signal
    only — genuinely identical amounts on the same day are common and legitimate
    (a customer on a monthly plan). Feature 6 decides whether a duplicate is
    suspicious; here it only marks records worth a second look.
    """
    findings: list[tuple[str, str, str]] = []
    buckets: dict[tuple, list[CanonicalBase]] = {}

    for record in records:
        amount = getattr(record, amount_attr, None)
        if amount is None:
            continue
        timestamp = (
            getattr(record, "payment_time", None)
            or getattr(record, "transaction_date", None)
            or getattr(record, "issue_date", None)
        )
        day = timestamp.date() if hasattr(timestamp, "date") else timestamp
        customer = (
            getattr(record, "customer_source_id", None)
            or getattr(record, "counterparty", None)
            or getattr(record, "customer_name", None)
        )
        buckets.setdefault((amount, day, customer), []).append(record)

    for (amount, day, customer), group in buckets.items():
        if len(group) < 2 or customer is None:
            continue
        for index in range(1, len(group)):
            findings.append(
                (
                    group[0].source_id,
                    group[index].source_id,
                    f"same amount ({amount} minor units), same date ({day}) "
                    f"and same counterparty ({customer})",
                )
            )
    return findings
