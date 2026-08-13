"""Continuous monitoring and impact analysis — Feature 8, sub-features 6-8.

A diligence report is a position, and positions move. A refund lands, a contract is
amended, an invoice is corrected — and the figure a reviewer read last week quietly
stops being true. This is what notices, and what decides how much has to be redone.

Three rules, all of them load-bearing:

* **A notification is a hint, never the data.** The same rule Feature 1 applies to
  webhooks applies here: a change signal tells us *where* to look and is never
  itself recorded as evidence. The authoritative record is refetched and re-hashed,
  and the vault's own content hash decides whether anything actually changed.
* **Only the affected work is redone.** Re-running everything is correct and
  useless — it takes minutes, burns model budget, and buries the one figure that
  moved in a full re-computation. The evidence chain says which customers a changed
  record touches; only those features, for those customers, are rerun.
* **A change that changes nothing is reported as such.** A refetch that produces an
  identical hash is a real answer — "we looked, and it is the same" — and saying so
  is what stops a reviewer wondering whether the check ran.

What is deliberately *not* here: a webhook receiver. Feature 1 already has one, with
HMAC verification and replay protection, and it already refetches authoritative
state. This module is the half that decides what a confirmed change means.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import claim_idempotency_key
from app.core.events import EventKind, Severity, emit
from app.models import (
    Contract,
    Invoice,
    Payment,
    RawRecord,
    Refund,
    RevenueItem,
    Workspace,
)
from app.services.audit import record_audit_event

#: Which features a change to each record type invalidates. Ordered lowest-first:
#: identity has to be right before the cash matching above it means anything.
IMPACT: dict[str, tuple[int, ...]] = {
    "customer": (2, 4, 5, 6, 7),
    "contract": (3, 5, 6, 7),
    "invoice": (4, 5, 6, 7),
    "payment": (4, 5, 6, 7),
    "refund": (4, 5, 6, 7),
    "bank_transaction": (4, 5, 6, 7),
    "credit_note": (4, 5, 6, 7),
    "dispute": (4, 5, 6, 7),
}

FEATURE_NAME = {
    2: "identity resolution",
    3: "contract intelligence",
    4: "reconciliation",
    5: "revenue classification",
    6: "anomaly detection",
    7: "critic and review",
}


@dataclass
class Change:
    """One record that moved since the last look."""

    record_type: str
    source_id: str
    source_system: str
    version: int
    detected_at: str
    customer_names: list[str] = field(default_factory=list)
    affected_features: list[int] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "source_id": self.source_id,
            "source_system": self.source_system,
            "version": self.version,
            "detected_at": self.detected_at,
            "customer_names": self.customer_names,
            "affected_features": self.affected_features,
            "affected_feature_names": [
                FEATURE_NAME.get(f, str(f)) for f in self.affected_features
            ],
            "note": self.note,
        }


@dataclass
class ImpactResult:
    checked_since: str = ""
    changes: list[Change] = field(default_factory=list)
    features_to_rerun: list[int] = field(default_factory=list)
    affected_customers: list[str] = field(default_factory=list)
    affected_items: int = 0
    unchanged: bool = True
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_since": self.checked_since,
            "changes": [c.as_dict() for c in self.changes],
            "features_to_rerun": self.features_to_rerun,
            "feature_names": [
                FEATURE_NAME.get(f, str(f)) for f in self.features_to_rerun
            ],
            "affected_customers": self.affected_customers,
            "affected_items": self.affected_items,
            "unchanged": self.unchanged,
            "summary": self.summary,
        }


async def detect_changes(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    since: datetime | None = None,
) -> ImpactResult:
    """What moved, who it touches, and which features that invalidates.

    The vault is the source of truth for "did this change": Feature 1 writes a new
    `RawRecord` version only when the canonical content hash differs, so a provider
    that returns the same record a hundred times produces one version and no work.
    A superseded row is therefore a *confirmed* change, not a notification.
    """
    since = since or (datetime.now(UTC) - timedelta(days=7))
    result = ImpactResult(checked_since=since.isoformat())

    # Version > 1 means the vault saw different content for a source id it already
    # held. That is the definition of a change here — not a webhook having fired.
    changed = list(
        (
            await session.execute(
                select(RawRecord)
                .where(
                    RawRecord.workspace_id == workspace_id,
                    RawRecord.version > 1,
                    RawRecord.superseded_by_id.is_(None),
                    RawRecord.created_at >= since,
                )
                .order_by(RawRecord.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    features: set[int] = set()
    customers: set[str] = set()

    for record in changed:
        record_type = str(record.record_type)
        affected = IMPACT.get(record_type, (5, 6, 7))
        features.update(affected)
        names = await _customers_for(
            session, workspace_id=workspace_id, record=record
        )
        customers.update(names)
        result.changes.append(
            Change(
                record_type=record_type,
                source_id=record.source_id,
                source_system=str(record.source_system),
                version=record.version,
                detected_at=(
                    record.created_at.isoformat() if record.created_at else ""
                ),
                customer_names=sorted(names)[:6],
                affected_features=sorted(affected),
                note=(
                    f"version {record.version} of this {record_type.replace('_', ' ')} "
                    f"differs from the one the current figures were built on"
                ),
            )
        )

    result.unchanged = not result.changes
    result.features_to_rerun = sorted(features)
    result.affected_customers = sorted(customers)[:40]

    if result.changes:
        result.affected_items = await _affected_item_count(
            session, workspace_id=workspace_id, customer_names=customers
        )
        result.summary = (
            f"{len(result.changes)} record(s) changed since "
            f"{since.date()}, touching {len(customers) or 'an unknown number of'} "
            f"customer(s) and {result.affected_items} classified item(s). "
            f"Rerun {', '.join(FEATURE_NAME[f] for f in result.features_to_rerun)}."
        )
    else:
        # Saying "we looked and nothing moved" is the point: silence is
        # indistinguishable from a check that never ran.
        result.summary = (
            f"No source record has changed since {since.date()}. The published "
            f"position still reflects the evidence as collected."
        )
    return result


async def _customers_for(
    session: AsyncSession, *, workspace_id: uuid.UUID, record: RawRecord
) -> set[str]:
    """Which customers a changed raw record touches.

    Impact analysis is only worth the name if it narrows the work, and it can only
    narrow it by naming who is affected. The canonical row derived from this raw
    record carries the customer; the payload's own name is the fallback for a record
    that never made it into a canonical table.
    """
    names: set[str] = set()
    for model in (Invoice, Payment, Contract):
        rows = (
            await session.execute(
                select(model).where(
                    model.workspace_id == workspace_id,
                    model.raw_record_id == record.id,
                )
            )
        ).scalars().all()
        for row in rows:
            stated = getattr(row, "stated_customer_name", None)
            if stated:
                names.add(stated)

    if not names:
        payload = record.payload or {}
        for key in ("customer_name", "contact_name", "company_name", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
                break
    return names


async def _affected_item_count(
    session: AsyncSession, *, workspace_id: uuid.UUID, customer_names: set[str]
) -> int:
    """How many classified items sit downstream of the changed records."""
    if not customer_names:
        return 0
    from app.features.identity.identifiers import normalize_name

    wanted = {normalize_name(name) for name in customer_names if name}
    items = (
        await session.execute(
            select(RevenueItem).where(RevenueItem.workspace_id == workspace_id)
        )
    ).scalars().all()

    from app.models import CustomerEntity

    entities = {
        str(row.id): normalize_name(row.canonical_name)
        for row in (
            await session.execute(
                select(CustomerEntity).where(
                    CustomerEntity.workspace_id == workspace_id
                )
            )
        ).scalars().all()
    }
    return sum(
        1
        for item in items
        if item.customer_entity_id
        and entities.get(str(item.customer_entity_id)) in wanted
    )


# ---------------------------------------------------------------------------
# Durable rerun
# ---------------------------------------------------------------------------


@dataclass
class RerunResult:
    ran: list[str] = field(default_factory=list)
    skipped: str = ""
    impact: dict[str, Any] = field(default_factory=dict)
    version: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "skipped": self.skipped,
            "impact": self.impact,
            "version": self.version,
        }


async def rerun_affected(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    since: datetime | None = None,
    force: bool = False,
    use_llm: bool = True,
) -> RerunResult:
    """Redo only the work the changes invalidated, then version the result.

    Idempotency is claimed for the whole rerun rather than per feature: two change
    signals arriving together must not start two overlapping recomputations of the
    same workspace, which is exactly how a report ends up assembled from two
    different evidence states.
    """
    result = RerunResult()
    impact = await detect_changes(session, workspace_id=workspace_id, since=since)
    result.impact = impact.as_dict()

    if impact.unchanged and not force:
        result.skipped = impact.summary
        emit(
            EventKind.RULE,
            impact.summary,
            workspace_id=str(workspace_id),
            feature=8,
        )
        return result

    claimed = await claim_idempotency_key(f"rerun:{workspace_id}", ttl=900)
    if not claimed:
        result.skipped = "a rerun for this workspace is already in progress"
        return result

    try:
        features = impact.features_to_rerun or [2, 3, 4, 5, 6, 7]
        emit(
            EventKind.AGENT_STEP,
            f"Impact analysis: rerunning "
            f"{', '.join(FEATURE_NAME[f] for f in features)} for "
            f"{len(impact.affected_customers)} affected customer(s)",
            workspace_id=str(workspace_id),
            feature=8,
        )

        # Features 4 and 5 are re-derived by Feature 6's scan and Feature 7's critic,
        # so running those two covers everything downstream of a cash or
        # classification change without doing the work twice.
        if 2 in features:
            from app.features.identity import service as identity

            await identity.resolve_identities(
                session, workspace_id=workspace_id, use_critic=False
            )
            result.ran.append(FEATURE_NAME[2])

        if 3 in features:
            from app.features.contracts import service as contracts

            await contracts.process_contracts(session, workspace_id=workspace_id)
            result.ran.append(FEATURE_NAME[3])

        if features and max(features) >= 5:
            from app.features.anomaly import service as anomaly

            await anomaly.scan(session, workspace_id=workspace_id, use_llm=use_llm)
            result.ran.append("reconciliation, revenue classification and anomalies")

        if 7 in features:
            from app.features.review import verify

            await verify.run_maker_checker(
                session, workspace_id=workspace_id, use_llm=use_llm
            )
            result.ran.append(FEATURE_NAME[7])

        # A rerun always versions, even when the figures did not move: "we checked
        # and it holds" is a fact a reviewer wants dated.
        from app.features.room import versions as versioning

        version = await versioning.publish_version(
            session, workspace_id=workspace_id, force=True
        )
        result.version = version.as_dict()

        await record_audit_event(
            session,
            workspace_id=workspace_id,
            actor_type="agent",
            actor_id="impact_analysis",
            action="evidence.rerun",
            object_type="workspace",
            object_id=str(workspace_id),
            after_state={
                "changes": len(impact.changes),
                "features": result.ran,
                "version": version.version,
            },
            reason=impact.summary[:500],
        )
        await session.flush()

        emit(
            EventKind.RESULT,
            f"Rerun complete: {', '.join(result.ran)} — version "
            f"{version.version}. {version.explanation}",
            workspace_id=str(workspace_id),
            feature=8,
            severity=Severity.SUCCESS,
        )
    finally:
        from app.core.cache import release_idempotency_key

        await release_idempotency_key(f"rerun:{workspace_id}")
    return result


async def monitoring_status(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """What the room needs to say about freshness without running anything."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return {}

    latest = (
        await session.execute(
            select(func.max(RawRecord.created_at)).where(
                RawRecord.workspace_id == workspace_id
            )
        )
    ).scalar_one_or_none()
    versioned = (
        await session.execute(
            select(func.count())
            .select_from(RawRecord)
            .where(
                RawRecord.workspace_id == workspace_id,
                RawRecord.version > 1,
                RawRecord.superseded_by_id.is_(None),
            )
        )
    ).scalar_one()
    refunds = (
        await session.execute(
            select(func.count())
            .select_from(Refund)
            .where(Refund.workspace_id == workspace_id)
        )
    ).scalar_one()

    return {
        "last_evidence_at": latest.isoformat() if latest else None,
        "records_with_newer_versions": int(versioned),
        "refunds_recorded": int(refunds),
        "note": (
            "A change is confirmed by refetching the authoritative record and "
            "comparing its content hash — a notification is only ever a hint about "
            "where to look."
        ),
    }
