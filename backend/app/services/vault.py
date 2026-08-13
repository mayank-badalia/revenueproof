"""Provenance vault — Feature 1, sub-feature 6.

Stores exactly what a provider returned, hashed, before anything interprets it.

Two hashes are kept, deliberately, per core_resoruces.md ("hash the original bytes
and canonical serialization separately and store algorithm/version"):

* `content_hash` — SHA-256 of the canonical JSON of the payload. Detects a *semantic*
  change even if the provider reorders keys or reformats whitespace.
* `file_hash` — SHA-256 of the original bytes for file-backed evidence. Detects an
  edited or replaced contract, which idea_features.md §18 lists as a case the system
  must handle safely.

A hash proves the content did not change; it does not prove who supplied it. That is
why `RawRecord` also carries source system, retrieval time, connector run ID and the
account identity the data came from — the W3C PROV triple of entity, activity and agent.

Versioning rather than mutation: when the same `source_id` comes back with different
content, the previous row is marked superseded and a new version is inserted. Nothing
in the vault is ever updated in place, so an earlier report can always resolve its
citations to the exact evidence a reviewer originally saw.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import sha256_bytes, sha256_json
from app.core.events import EventKind, Severity, emit
from app.models import RawRecord
from app.models.enums import RecordType, SourceSystem

# Hash algorithm recorded alongside every digest so a future migration to a
# different function can be detected rather than silently mismatching.
HASH_ALGORITHM = "sha256"
# Bumped when the canonical serialisation itself changes, which would otherwise
# make old and new hashes incomparable for identical content.
HASH_VERSION = 1


class VaultError(RuntimeError):
    pass


class StoreResult:
    """Outcome of one vault write."""

    __slots__ = ("record", "created", "superseded_version", "duplicate")

    def __init__(
        self,
        record: RawRecord,
        *,
        created: bool,
        superseded_version: int | None = None,
        duplicate: bool = False,
    ) -> None:
        self.record = record
        self.created = created
        self.superseded_version = superseded_version
        self.duplicate = duplicate

    @property
    def outcome(self) -> str:
        if self.duplicate:
            return "duplicate"
        return "new_version" if self.superseded_version else "created"


async def store_raw_record(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_system: SourceSystem,
    record_type: RecordType,
    source_id: str,
    payload: dict[str, Any],
    ingestion_run_id: str | None = None,
    retrieved_at: datetime | None = None,
    file_bytes: bytes | None = None,
    file_name: str | None = None,
    mime_type: str | None = None,
) -> StoreResult:
    """Store one source record idempotently.

    Three possible outcomes:
    * **duplicate** — identical content already stored; nothing is written. This is
      what makes a replayed webhook or a re-run backfill harmless.
    * **new_version** — this `source_id` exists but the content differs; the prior
      row is marked superseded and a new version is inserted.
    * **created** — first time this record has been seen.
    """
    retrieved_at = retrieved_at or datetime.now(UTC)
    content_hash = sha256_json(payload)

    # Exact content match => already have it. The unique constraint on
    # (workspace, source_system, source_id, content_hash) backs this at the DB level.
    existing_identical = (
        await session.execute(
            select(RawRecord).where(
                RawRecord.workspace_id == workspace_id,
                RawRecord.source_system == source_system,
                RawRecord.source_id == source_id,
                RawRecord.content_hash == content_hash,
            )
        )
    ).scalar_one_or_none()

    if existing_identical is not None:
        emit(
            EventKind.PERSISTENCE,
            f"Duplicate {record_type} {source_id} — already stored, skipping",
            workspace_id=str(workspace_id),
            severity=Severity.DEBUG,
            feature=1,
            run_id=ingestion_run_id,
            content_hash=content_hash[:12],
        )
        return StoreResult(existing_identical, created=False, duplicate=True)

    # Same identifier, different content => a genuine change in the source.
    latest = (
        await session.execute(
            select(RawRecord)
            .where(
                RawRecord.workspace_id == workspace_id,
                RawRecord.source_system == source_system,
                RawRecord.source_id == source_id,
                RawRecord.superseded_by_id.is_(None),
            )
            .order_by(RawRecord.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    version = (latest.version + 1) if latest else 1

    storage_key: str | None = None
    file_hash: str | None = None
    size: int | None = None
    if file_bytes is not None:
        file_hash = sha256_bytes(file_bytes)
        size = len(file_bytes)
        storage_key = _write_object(
            workspace_id=workspace_id,
            source_system=source_system,
            source_id=source_id,
            version=version,
            file_bytes=file_bytes,
            file_name=file_name,
        )

    record = RawRecord(
        workspace_id=workspace_id,
        source_system=source_system,
        record_type=record_type,
        source_id=source_id,
        payload=payload,
        content_hash=content_hash,
        retrieved_at=retrieved_at,
        version=version,
        ingestion_run_id=ingestion_run_id,
        storage_key=storage_key,
        file_hash=file_hash,
        file_size_bytes=size,
        mime_type=mime_type,
    )
    session.add(record)
    await session.flush()

    if latest is not None:
        # Point the old version forward rather than deleting it: a report published
        # before the change must still resolve to what was reviewed at the time.
        latest.superseded_by_id = record.id
        await session.flush()
        emit(
            EventKind.PERSISTENCE,
            f"Source changed: {record_type} {source_id} → version {version}",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=1,
            run_id=ingestion_run_id,
            previous_hash=latest.content_hash[:12],
            new_hash=content_hash[:12],
        )
        return StoreResult(record, created=True, superseded_version=latest.version)

    emit(
        EventKind.PERSISTENCE,
        f"Vaulted {record_type} {source_id}",
        workspace_id=str(workspace_id),
        severity=Severity.DEBUG,
        feature=1,
        run_id=ingestion_run_id,
        content_hash=content_hash[:12],
        file_hash=file_hash[:12] if file_hash else None,
    )
    return StoreResult(record, created=True)


def _write_object(
    *,
    workspace_id: uuid.UUID,
    source_system: SourceSystem,
    source_id: str,
    version: int,
    file_bytes: bytes,
    file_name: str | None,
) -> str:
    """Persist file bytes to versioned object storage.

    Local filesystem here; the key layout mirrors an S3 prefix scheme so swapping in
    S3 + KMS + Object Lock (the production path in core_resoruces.md) is a driver
    change rather than a redesign. Content is never overwritten: the version is part
    of the key.
    """
    safe_source = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source_id)[:120]
    suffix = Path(file_name).suffix[:10] if file_name else ""
    key = f"{workspace_id}/{source_system}/{safe_source}.v{version}{suffix}"

    destination = Path(settings.evidence_storage_path) / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise VaultError(f"refusing to overwrite existing evidence object: {key}")
    destination.write_bytes(file_bytes)
    return key


def read_object(storage_key: str) -> bytes:
    """Read evidence bytes back, refusing to escape the storage root."""
    root = Path(settings.evidence_storage_path).resolve()
    target = (root / storage_key).resolve()
    # A storage_key containing ../ would otherwise read arbitrary files.
    if not target.is_relative_to(root):
        raise VaultError(f"storage key escapes the evidence root: {storage_key!r}")
    if not target.exists():
        raise VaultError(f"evidence object not found: {storage_key}")
    return target.read_bytes()


def verify_object(storage_key: str, expected_hash: str) -> bool:
    """Confirm stored bytes still match the hash recorded at ingestion."""
    return sha256_bytes(read_object(storage_key)) == expected_hash


async def get_current_records(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    record_type: RecordType | None = None,
    source_system: SourceSystem | None = None,
) -> list[RawRecord]:
    """Latest non-superseded version of each record."""
    query = select(RawRecord).where(
        RawRecord.workspace_id == workspace_id,
        RawRecord.superseded_by_id.is_(None),
    )
    if record_type is not None:
        query = query.where(RawRecord.record_type == record_type)
    if source_system is not None:
        query = query.where(RawRecord.source_system == source_system)
    return list((await session.execute(query)).scalars().all())


async def lineage_for(
    session: AsyncSession, *, workspace_id: uuid.UUID, source_id: str
) -> list[dict[str, Any]]:
    """Full version history for one source record, oldest first.

    This is the W3C PROV derivation chain the UI shows when a reviewer asks why a
    figure changed between report versions.
    """
    rows = (
        (
            await session.execute(
                select(RawRecord)
                .where(
                    RawRecord.workspace_id == workspace_id,
                    RawRecord.source_id == source_id,
                )
                .order_by(RawRecord.version.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "version": row.version,
            "content_hash": row.content_hash,
            "file_hash": row.file_hash,
            "hash_algorithm": HASH_ALGORITHM,
            "hash_version": HASH_VERSION,
            "retrieved_at": row.retrieved_at.isoformat(),
            "source_system": row.source_system,
            "ingestion_run_id": row.ingestion_run_id,
            "superseded": row.superseded_by_id is not None,
            "storage_key": row.storage_key,
        }
        for row in rows
    ]
