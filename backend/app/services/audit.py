"""Append-only, hash-chained audit log.

idea_features.md §17 requires every tool call, classification, review, export and
override to be logged, and §7 requires a human override to carry a reason that stays
visible. This module is the single writer for that log.

The chain: each event hashes (previous_hash + its own content). Removing or editing
any historical event breaks every subsequent hash, which `verify_chain` detects.
That is enough for a platform with one trusted writer — core_resoruces.md is
explicit that blockchain would add cost without solving a real problem here.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import chain_hash, sha256_json
from app.core.events import EventKind, Severity, emit
from app.models import AuditEvent


async def record_audit_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_type: str,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    reason: str | None = None,
    run_id: uuid.UUID | None = None,
    policy_version: str | None = None,
) -> AuditEvent:
    """Append one event. Flushes but does not commit — it joins the caller's transaction.

    Sharing the caller's transaction is deliberate: an audit row must not survive a
    rolled-back change, and a committed change must not lose its audit row.
    """
    previous = (
        await session.execute(
            select(AuditEvent)
            .where(AuditEvent.workspace_id == workspace_id)
            .order_by(AuditEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    sequence = (previous.sequence + 1) if previous else 1
    previous_hash = previous.event_hash if previous else None

    content = {
        "sequence": sequence,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "before_state": before_state,
        "after_state": after_state,
        "reason": reason,
    }

    event = AuditEvent(
        workspace_id=workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before_state=before_state,
        after_state=after_state,
        before_hash=sha256_json(before_state) if before_state is not None else None,
        after_hash=sha256_json(after_state) if after_state is not None else None,
        reason=reason,
        sequence=sequence,
        previous_hash=previous_hash,
        event_hash=chain_hash(previous_hash, content),
        run_id=run_id,
        policy_version=policy_version,
    )
    session.add(event)
    await session.flush()

    emit(
        EventKind.PERSISTENCE,
        f"Audit #{sequence}: {action} on {object_type}",
        workspace_id=str(workspace_id),
        severity=Severity.DEBUG,
        actor=f"{actor_type}:{actor_id}",
        object_id=object_id,
        reason=reason,
    )
    return event


async def verify_chain(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    """Recompute every hash and report the first break, if any."""
    events = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == workspace_id)
                .order_by(AuditEvent.sequence.asc())
            )
        )
        .scalars()
        .all()
    )

    previous_hash: str | None = None
    for index, event in enumerate(events):
        expected_sequence = index + 1
        if event.sequence != expected_sequence:
            return {
                "valid": False,
                "checked": index,
                "error": f"sequence gap: expected {expected_sequence}, found {event.sequence}",
            }
        content = {
            "sequence": event.sequence,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "action": event.action,
            "object_type": event.object_type,
            "object_id": event.object_id,
            "before_state": event.before_state,
            "after_state": event.after_state,
            "reason": event.reason,
        }
        if chain_hash(previous_hash, content) != event.event_hash:
            return {
                "valid": False,
                "checked": index,
                "error": f"hash mismatch at sequence {event.sequence}",
            }
        previous_hash = event.event_hash

    return {"valid": True, "checked": len(events), "error": None}
