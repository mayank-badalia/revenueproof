"""Ingestion pipeline — Feature 1 end to end.

Wires the sub-features into the workflow `core_resoruces.md` specifies:

    provider fetch → raw immutable vault + hashes → deterministic normalisation
    → schema/data-quality validation → canonical PostgreSQL records

Two properties are enforced here rather than left to callers:

* **Idempotency.** A record already in the vault with identical content produces no
  new canonical row. Replaying a webhook, re-running a backfill or clicking sync
  twice therefore cannot create a second canonical fact — the requirement stated in
  the Feature 1 workflow.
* **Quarantine before handoff.** A record that fails validation is written to the
  quarantine table and never reaches the canonical tables, so Features 2–4 only ever
  see evidence that parsed cleanly.

Canonical writes use PostgreSQL upserts keyed on `(workspace, source_system,
source_id)`. That makes re-ingestion converge on the same state rather than
accumulating duplicates, which is what allows a failed run to simply be re-run.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors import normalize
from app.connectors.base import Connector, ConnectorError, FetchedRecord
from app.connectors.providers import CONNECTOR_REGISTRY
from app.core.cache import claim_idempotency_key
from app.core.config import settings
from app.core.events import EventKind, Severity, emit
from app.models import (
    BankTransaction,
    Contract,
    CreditNote,
    CustomerEntity,
    Invoice,
    Payment,
    ProviderConnection,
    Refund,
)
from app.models.enums import QuarantineReason, RecordType, SourceSystem
from app.schemas.canonical import (
    CanonicalBankTransaction,
    CanonicalContractDocument,
    CanonicalCreditNote,
    CanonicalCrmAccount,
    CanonicalCustomer,
    CanonicalInvoice,
    CanonicalPayment,
    CanonicalRefund,
)
from app.services import quarantine as quarantine_service
from app.services import vault
from app.services.audit import record_audit_event

# Which normaliser handles each (source, record type) pair. Keeping this as data
# rather than a chain of if-statements makes an unhandled combination an explicit
# gap rather than a silent no-op.
NORMALIZERS = {
    (SourceSystem.RAZORPAY, RecordType.PAYMENT): normalize.razorpay_payment,
    (SourceSystem.RAZORPAY, RecordType.REFUND): normalize.razorpay_refund,
    (SourceSystem.RAZORPAY, RecordType.DISPUTE): normalize.razorpay_dispute,
    (SourceSystem.ZOHO_BOOKS, RecordType.CUSTOMER): normalize.zoho_contact,
    (SourceSystem.ZOHO_BOOKS, RecordType.INVOICE): normalize.zoho_invoice,
    (SourceSystem.ZOHO_BOOKS, RecordType.CREDIT_NOTE): normalize.zoho_credit_note,
    (SourceSystem.ZOHO_BOOKS, RecordType.PAYMENT): normalize.zoho_payment,
    (SourceSystem.GOOGLE_DRIVE, RecordType.CONTRACT): normalize.drive_file,
    (SourceSystem.HUBSPOT, RecordType.CRM_ACCOUNT): normalize.hubspot_company,
}


@dataclass
class IngestionStats:
    fetched: int = 0
    vaulted: int = 0
    duplicates: int = 0
    new_versions: int = 0
    normalized: int = 0
    quarantined: int = 0
    canonical_written: int = 0
    skipped_no_normalizer: int = 0
    errors: list[str] = field(default_factory=list)
    by_type: dict[str, int] = field(default_factory=dict)
    is_synthetic: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "vaulted": self.vaulted,
            "duplicates": self.duplicates,
            "new_versions": self.new_versions,
            "normalized": self.normalized,
            "quarantined": self.quarantined,
            "canonical_written": self.canonical_written,
            "skipped_no_normalizer": self.skipped_no_normalizer,
            "errors": self.errors[:20],
            "by_type": self.by_type,
            "is_synthetic": self.is_synthetic,
        }


async def ingest_source(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_system: SourceSystem,
    connection: ProviderConnection | None = None,
    run_id: str | None = None,
    access_token: str | None = None,
    force_synthetic: bool = False,
) -> IngestionStats:
    """Fetch, vault, normalise, validate and persist one evidence source."""
    run_id = run_id or uuid.uuid4().hex[:12]
    stats = IngestionStats()

    connector_class = CONNECTOR_REGISTRY.get(source_system)
    if connector_class is None:
        stats.errors.append(f"no connector registered for {source_system}")
        return stats

    kwargs: dict[str, Any] = {
        "cursor": (connection.sync_cursor if connection else {}),
        "force_synthetic": force_synthetic,
    }
    # Only the OAuth connectors accept a token; Razorpay uses static key auth.
    if source_system in {
        SourceSystem.ZOHO_BOOKS,
        SourceSystem.GOOGLE_DRIVE,
        SourceSystem.HUBSPOT,
    }:
        kwargs["access_token"] = access_token

    connector: Connector = connector_class(str(workspace_id), **kwargs)

    emit(
        EventKind.AGENT_STEP,
        f"Connector Agent starting: {connector.display_name}",
        workspace_id=str(workspace_id),
        feature=1,
        run_id=run_id,
    )

    try:
        fetch_result = await connector.fetch()
    except ConnectorError as exc:
        stats.errors.append(str(exc))
        emit(
            EventKind.ERROR,
            f"{connector.display_name} fetch failed: {exc}",
            workspace_id=str(workspace_id),
            severity=Severity.ERROR,
            feature=1,
            run_id=run_id,
        )
        if connection is not None:
            connection.last_sync_status = "failed"
            connection.last_sync_error = str(exc)[:1000]
            connection.last_sync_at = datetime.now(UTC)
        return stats

    stats.fetched = len(fetch_result.records)
    stats.is_synthetic = fetch_result.is_synthetic
    stats.errors.extend(fetch_result.errors)

    for record in fetch_result.records:
        await _process_record(
            session,
            workspace_id=workspace_id,
            source_system=source_system,
            record=record,
            stats=stats,
            run_id=run_id,
        )

    if connection is not None:
        connection.sync_cursor = fetch_result.cursor
        connection.last_sync_at = datetime.now(UTC)
        connection.last_sync_status = "partial" if stats.errors else "ok"
        connection.last_sync_error = "; ".join(stats.errors[:3])[:1000] or None
        connection.is_synthetic = fetch_result.is_synthetic
        connection.records_imported = (connection.records_imported or 0) + stats.canonical_written

    await record_audit_event(
        session,
        workspace_id=workspace_id,
        actor_type="agent",
        actor_id=f"connector:{source_system}",
        action="evidence.ingested",
        object_type="provider_connection",
        object_id=str(source_system),
        after_state=stats.as_dict(),
        reason=f"ingestion run {run_id}",
    )

    emit(
        EventKind.RESULT,
        f"{connector.display_name}: {stats.canonical_written} canonical records, "
        f"{stats.duplicates} duplicates skipped, {stats.quarantined} quarantined",
        workspace_id=str(workspace_id),
        severity=Severity.SUCCESS,
        feature=1,
        run_id=run_id,
        **stats.as_dict(),
    )
    return stats


async def _process_record(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_system: SourceSystem,
    record: FetchedRecord,
    stats: IngestionStats,
    run_id: str,
) -> None:
    """Vault → normalise → validate → persist, for one record."""
    # 1. Vault the raw payload first, so evidence survives even if normalisation
    #    later fails. A record we could not interpret is still a record we received.
    store = await vault.store_raw_record(
        session,
        workspace_id=workspace_id,
        source_system=source_system,
        record_type=record.record_type,
        source_id=record.source_id,
        payload=record.payload,
        ingestion_run_id=run_id,
        file_bytes=record.file_bytes,
        file_name=record.file_name,
        mime_type=record.mime_type,
    )

    if store.duplicate:
        stats.duplicates += 1
        return  # identical content already processed; nothing further to do
    stats.vaulted += 1
    if store.superseded_version:
        stats.new_versions += 1

    # 2. Normalise into canonical form.
    normalizer = NORMALIZERS.get((source_system, record.record_type))
    if normalizer is None:
        # Settlements are vaulted for Feature 4 but have no canonical table yet.
        stats.skipped_no_normalizer += 1
        return

    try:
        canonical = normalizer(record.payload)
    except normalize.NormalizationError as exc:
        stats.quarantined += 1
        await quarantine_service.quarantine(
            session,
            workspace_id=workspace_id,
            source_system=source_system,
            record_type=record.record_type,
            reason=QuarantineReason.SCHEMA_INVALID,
            detail=str(exc),
            payload=record.payload,
            source_id=record.source_id,
        )
        return
    except Exception as exc:  # unexpected shape — still must not kill the run
        stats.quarantined += 1
        await quarantine_service.quarantine(
            session,
            workspace_id=workspace_id,
            source_system=source_system,
            record_type=record.record_type,
            reason=QuarantineReason.SCHEMA_INVALID,
            detail=f"{type(exc).__name__}: {exc}",
            payload=record.payload,
            source_id=record.source_id,
        )
        return

    # 3. Re-validate the normalised object against its canonical schema. The
    #    normaliser builds the model, but this catches cross-field rules (an over-
    #    refunded payment, an amount_due above the total) with a classified reason.
    outcome = quarantine_service.validate_record(
        record.record_type, canonical.model_dump(mode="json")
    )
    if not outcome.ok:
        stats.quarantined += 1
        await quarantine_service.quarantine(
            session,
            workspace_id=workspace_id,
            source_system=source_system,
            record_type=record.record_type,
            reason=outcome.reason or QuarantineReason.SCHEMA_INVALID,
            detail=outcome.detail,
            payload=record.payload,
            source_id=record.source_id,
            errors=outcome.errors,
        )
        return

    stats.normalized += 1

    # 4. Persist the canonical record.
    written = await _persist(
        session,
        workspace_id=workspace_id,
        canonical=canonical,
        raw_record_id=store.record.id,
    )
    if written:
        stats.canonical_written += 1
        key = str(record.record_type)
        stats.by_type[key] = stats.by_type.get(key, 0) + 1


async def _persist(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    canonical: Any,
    raw_record_id: uuid.UUID,
) -> bool:
    """Upsert one canonical record. Returns False for types with no table yet."""
    common = {"workspace_id": workspace_id, "raw_record_id": raw_record_id}

    if isinstance(canonical, CanonicalCustomer):
        # Customers are NOT upserted into a shared identity here. Feature 2 decides
        # which source records represent the same real customer; merging them at
        # ingestion would destroy the evidence that decision depends on.
        normalized_name = _normalize_company_name(canonical.display_name)
        existing = (
            await session.execute(
                select(CustomerEntity).where(
                    CustomerEntity.workspace_id == workspace_id,
                    CustomerEntity.normalized_name == normalized_name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                CustomerEntity(
                    workspace_id=workspace_id,
                    canonical_name=canonical.display_name,
                    legal_name=canonical.legal_name,
                    normalized_name=normalized_name,
                    domains=[canonical.email_domain] if canonical.email_domain else [],
                    tax_identifiers=canonical.tax_identifiers,
                    known_aliases=[canonical.display_name],
                    email_addresses=[canonical.email] if canonical.email else [],
                    addresses=[canonical.billing_address] if canonical.billing_address else [],
                )
            )
            await session.flush()
        return True

    if isinstance(canonical, CanonicalInvoice):
        await _upsert(
            session,
            Invoice,
            index_elements=["workspace_id", "source_system", "source_id"],
            values={
                **common,
                "source_system": canonical.source_system,
                "source_id": canonical.source_id,
                "invoice_number": canonical.invoice_number,
                "stated_customer_name": canonical.customer_name,
                "issue_date": canonical.issue_date,
                "due_date": canonical.due_date,
                "currency": canonical.currency,
                "subtotal": canonical.subtotal_minor,
                "tax": canonical.tax_minor,
                "total": canonical.total_minor,
                "amount_due": canonical.amount_due_minor,
                "status": canonical.status,
                "line_items": [item.model_dump(mode="json") for item in canonical.line_items],
                "has_one_time_items": canonical.has_one_time_items,
            },
        )
        return True

    if isinstance(canonical, CanonicalCreditNote):
        await _upsert(
            session,
            CreditNote,
            index_elements=["workspace_id", "source_system", "source_id"],
            values={
                **common,
                "source_system": canonical.source_system,
                "source_id": canonical.source_id,
                "credit_note_number": canonical.credit_note_number,
                "issue_date": canonical.issue_date,
                "currency": canonical.currency,
                "total": canonical.total_minor,
                "reason": canonical.reason,
            },
        )
        return True

    if isinstance(canonical, CanonicalPayment):
        await _upsert(
            session,
            Payment,
            index_elements=["workspace_id", "source_system", "source_id"],
            values={
                **common,
                "source_system": canonical.source_system,
                "source_id": canonical.source_id,
                "stated_customer_name": canonical.customer_name,
                "contact_email": canonical.email,
                "contact_phone": canonical.phone,
                "currency": canonical.currency,
                "amount": canonical.amount_minor,
                "fee": canonical.fee_minor,
                "tax": canonical.tax_minor,
                "amount_refunded": canonical.amount_refunded_minor,
                "status": canonical.status,
                "payment_time": canonical.payment_time,
                "method": canonical.method,
                "description": canonical.description,
                "reference": canonical.reference,
                "settlement_id": canonical.settlement_id,
                "settled_at": canonical.settled_at,
            },
        )
        return True

    if isinstance(canonical, CanonicalRefund):
        await _upsert(
            session,
            Refund,
            index_elements=["workspace_id", "source_system", "source_id"],
            values={
                **common,
                "source_system": canonical.source_system,
                "source_id": canonical.source_id,
                "source_payment_id": canonical.payment_source_id,
                "currency": canonical.currency,
                "amount": canonical.amount_minor,
                "status": canonical.status,
                "refund_time": canonical.refund_time,
                "reason": canonical.reason,
                "is_chargeback": canonical.is_chargeback,
            },
        )
        return True

    if isinstance(canonical, CanonicalBankTransaction):
        await _upsert(
            session,
            BankTransaction,
            index_elements=["workspace_id", "source_system", "source_id"],
            values={
                **common,
                "source_system": canonical.source_system,
                "source_id": canonical.source_id,
                "account_fingerprint": canonical.account_fingerprint,
                "transaction_date": canonical.transaction_date,
                "value_date": canonical.value_date,
                "currency": canonical.currency,
                "amount": canonical.amount_minor,
                "direction": canonical.direction,
                "counterparty": canonical.counterparty,
                "reference": canonical.reference,
                "narration": canonical.narration,
                "balance_after": canonical.balance_after_minor,
            },
        )
        return True

    if isinstance(canonical, CanonicalContractDocument):
        # Only the document record is created here. Terms stay empty until
        # Feature 3 reads the file — an unparsed contract must not look like a
        # contract with zero value.
        existing = (
            await session.execute(
                select(Contract).where(
                    Contract.workspace_id == workspace_id,
                    Contract.document_name == canonical.file_name,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                Contract(
                    workspace_id=workspace_id,
                    raw_record_id=raw_record_id,
                    document_name=canonical.file_name,
                    needs_human_review=False,
                    unknown_fields=["terms_not_yet_extracted"],
                )
            )
            await session.flush()
        return True

    if isinstance(canonical, CanonicalCrmAccount):
        # CRM records live in the vault as a matching signal for Feature 2; they are
        # not financial evidence and get no canonical table of their own.
        return True

    return False


async def _upsert(
    session: AsyncSession, model: Any, *, index_elements: list[str], values: dict[str, Any]
) -> None:
    """INSERT ... ON CONFLICT DO UPDATE, so re-ingestion converges rather than duplicating."""
    updatable = {k: v for k, v in values.items() if k not in index_elements}
    statement = (
        pg_insert(model)
        .values(**values)
        .on_conflict_do_update(index_elements=index_elements, set_=updatable)
    )
    await session.execute(statement)


def _normalize_company_name(name: str) -> str:
    """Strip legal suffixes and punctuation for blocking/matching.

    Feature 2 does the real identity work; this only produces a stable key so that
    two spellings of the same string do not create two rows before matching runs.
    """
    import re

    text = name.lower().strip()
    text = re.sub(
        r"\b(private limited|pvt\.? ?ltd\.?|pvt|private|limited|ltd\.?|llp|inc\.?|"
        r"corp\.?|corporation|co\.?|company|technologies|technology)\b",
        "",
        text,
    )
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def ingest_contract_files(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    files: list[tuple[str, bytes]],
    run_id: str | None = None,
) -> dict[str, Any]:
    """Vault uploaded contract PDFs so Feature 3 reads them like any other contract.

    "Upload your own records" offered a bank statement *and contracts as PDFs*, and
    only the statement had anywhere to go — the button scrolled to the CSV input and
    nothing accepted a PDF at all. A workspace could therefore be built from a
    founder's own bank statement while every contract stayed unread, which produces
    the one outcome this product is supposed to make impossible: an ARR figure with
    no contract behind it and nothing on screen saying so.

    Files land in the same vault, under the same hashes, and are read by the same
    extractor as a Drive-sourced contract. Nothing downstream knows the difference,
    which is the point — an uploaded contract is evidence on exactly the same terms.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for filename, content in files:
        problem = _reject_contract_upload(filename, content)
        if problem:
            rejected.append({"filename": filename, "reason": problem})
            await quarantine_service.quarantine(
                session,
                workspace_id=workspace_id,
                source_system=SourceSystem.GOOGLE_DRIVE,
                record_type=RecordType.CONTRACT,
                reason=QuarantineReason.UNSAFE_FILE,
                detail=problem,
                payload={"filename": filename, "size_bytes": len(content)},
                source_id=filename,
            )
            continue

        # Keyed on the file's own content hash, so uploading the same contract twice
        # is a duplicate rather than a second copy competing for the same customer.
        digest = hashlib.sha256(content).hexdigest()[:24]
        source_id = f"upload_{digest}"
        result = await vault.store_raw_record(
            session,
            workspace_id=workspace_id,
            source_system=SourceSystem.GOOGLE_DRIVE,
            record_type=RecordType.CONTRACT,
            source_id=source_id,
            payload={
                "id": source_id,
                "name": filename,
                "mimeType": "application/pdf",
                "size": len(content),
                "folderPath": "uploaded",
            },
            ingestion_run_id=run_id,
            file_bytes=content,
            file_name=filename,
            mime_type="application/pdf",
        )
        accepted.append({
            "filename": filename,
            "size_bytes": len(content),
            "outcome": result.outcome,
        })
        if result.outcome != "duplicate":
            await _persist(
                session,
                workspace_id=workspace_id,
                canonical=normalize.drive_file(result.record.payload),
                raw_record_id=result.record.id,
            )

    await session.flush()
    emit(
        EventKind.RESULT,
        f"Contract upload: {len(accepted)} accepted, {len(rejected)} rejected"
        + (f" — {rejected[0]['reason']}" if rejected else ""),
        workspace_id=str(workspace_id),
        severity=Severity.SUCCESS if accepted else Severity.WARNING,
        feature=1,
        run_id=run_id,
    )
    return {
        "run_id": run_id,
        "accepted": accepted,
        "rejected": rejected,
        "vaulted": len(accepted),
    }


#: A contract PDF above this is refused rather than parsed. Feature 3 caps pages
#: too; this is the cheaper check that happens before anything is read.
MAX_CONTRACT_BYTES = 25 * 1024 * 1024


def _reject_contract_upload(filename: str, content: bytes) -> str | None:
    """Why this upload cannot be accepted, or None. Checked before any parsing."""
    if not content:
        return "the file is empty"
    if len(content) > MAX_CONTRACT_BYTES:
        return (
            f"the file is {len(content) // (1024 * 1024)} MB; the limit is "
            f"{MAX_CONTRACT_BYTES // (1024 * 1024)} MB"
        )
    # Magic bytes, not the extension. A .pdf that is really a zip is the oldest
    # upload trick there is, and the extension is chosen by whoever sends the file.
    if not content.startswith(b"%PDF-"):
        return "the file is not a PDF (its first bytes are not %PDF-)"
    if b"\x00" in content[:1024] and not content.startswith(b"%PDF-"):
        return "the file contains NUL bytes in its header"
    return None


async def ingest_bank_csv(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    content: bytes,
    filename: str,
    currency: str = "INR",
    run_id: str | None = None,
    is_synthetic: bool = False,
) -> IngestionStats:
    """Import a bank statement, vaulting the file itself alongside its rows.

    `is_synthetic` must be set by the caller. A statement generated from the §15
    demonstration dataset and one a founder actually uploaded are indistinguishable
    once parsed, and labelling the former "live" in the UI is precisely the
    confusion the badge exists to prevent.
    """
    from app.connectors.bank_csv import BankCsvError, import_csv

    run_id = run_id or uuid.uuid4().hex[:12]
    stats = IngestionStats()
    stats.is_synthetic = is_synthetic

    try:
        parsed = import_csv(
            content, filename, workspace_id=str(workspace_id), currency=currency
        )
    except BankCsvError as exc:
        stats.errors.append(str(exc))
        await quarantine_service.quarantine(
            session,
            workspace_id=workspace_id,
            source_system=SourceSystem.BANK_CSV,
            record_type=RecordType.BANK_TRANSACTION,
            reason=QuarantineReason.UNSAFE_FILE,
            detail=str(exc),
            payload={"filename": filename, "size_bytes": len(content)},
            source_id=filename,
        )
        return stats

    stats.fetched = parsed.total_rows

    # Vault the original file so a reviewer can always see the statement as supplied.
    await vault.store_raw_record(
        session,
        workspace_id=workspace_id,
        source_system=SourceSystem.BANK_CSV,
        record_type=RecordType.BANK_TRANSACTION,
        source_id=f"statement:{filename}",
        payload={
            "filename": filename,
            "rows": parsed.total_rows,
            "accepted": parsed.accepted,
            "rejected": len(parsed.rejected),
            "columns": parsed.detected_columns,
        },
        ingestion_run_id=run_id,
        file_bytes=content,
        file_name=filename,
        mime_type="text/csv",
    )

    for transaction in parsed.transactions:
        store = await vault.store_raw_record(
            session,
            workspace_id=workspace_id,
            source_system=SourceSystem.BANK_CSV,
            record_type=RecordType.BANK_TRANSACTION,
            source_id=transaction.source_id,
            payload=transaction.model_dump(mode="json"),
            ingestion_run_id=run_id,
        )
        if store.duplicate:
            stats.duplicates += 1
            continue
        stats.vaulted += 1
        stats.normalized += 1
        await _persist(
            session,
            workspace_id=workspace_id,
            canonical=transaction,
            raw_record_id=store.record.id,
        )
        stats.canonical_written += 1

    for rejection in parsed.rejected:
        stats.quarantined += 1
        await quarantine_service.quarantine(
            session,
            workspace_id=workspace_id,
            source_system=SourceSystem.BANK_CSV,
            record_type=RecordType.BANK_TRANSACTION,
            reason=QuarantineReason.SCHEMA_INVALID,
            detail=rejection["error"],
            payload=rejection.get("data", {}),
            source_id=f"row_{rejection['row']}",
        )

    stats.by_type["bank_transaction"] = stats.canonical_written
    emit(
        EventKind.RESULT,
        f"Bank CSV imported: {stats.canonical_written} transactions, "
        f"{stats.duplicates} duplicates, {stats.quarantined} rejected",
        workspace_id=str(workspace_id),
        severity=Severity.SUCCESS,
        feature=1,
        run_id=run_id,
        **stats.as_dict(),
    )
    return stats


#: Everything a demonstration dataset creates, in the order a foreign key allows it
#: to be removed: the analysis built on the evidence, then the evidence, then the
#: raw records behind it.
_SYNTHETIC_TEARDOWN = (
    "revenue_items", "critic_decisions", "anomalies", "allocations",
    "citations", "review_items",
    "refunds", "credit_notes", "payments", "invoices", "bank_transactions",
    "contracts", "customer_entities", "entity_match_proposals",
    "quarantined_records", "raw_records",
)


async def _clear_synthetic_evidence(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> int:
    """Remove this workspace's evidence so a fresh dataset replaces it.

    Scoped to one workspace and nothing else — the tenant filter is on every
    statement, not on the first one.

    Two things are deliberately *not* removed. `report_versions` are immutable by
    design and describe a position that genuinely was published at the time, so
    deleting them would rewrite history to make the new dataset look like it had
    always been there. `audit_events` are hash-chained; removing a link is exactly
    the tampering the chain exists to detect, and the clearing is itself recorded as
    an event rather than hidden by one.

    Returns the number of rows removed, so the caller can say what it did instead of
    silently discarding a dataset the user may not have meant to lose.
    """
    from sqlalchemy import text

    removed = 0
    for table in _SYNTHETIC_TEARDOWN:
        try:
            result = await session.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": str(workspace_id)},
            )
            removed += result.rowcount or 0
        except Exception:  # noqa: BLE001
            # A table that does not exist in this schema version must not stop the
            # reload; the alternative is a workspace that can never be reset.
            await session.rollback()
    await session.flush()

    # The vault refuses to overwrite a stored object — deliberately, because
    # evidence bytes are immutable and a silent overwrite is how a contract gets
    # swapped without anyone noticing. Clearing the rows without clearing the
    # objects therefore left the workspace unable to load a second dataset at all:
    # the next run died on `refusing to overwrite existing evidence object`. The
    # files belong to the rows that were just removed, so they go with them.
    removed += _clear_evidence_objects(workspace_id)
    return removed


def _clear_evidence_objects(workspace_id: uuid.UUID) -> int:
    """Delete this workspace's stored evidence files. Never leaves its own folder."""
    import shutil
    from pathlib import Path

    from app.core.config import settings

    root = Path(settings.evidence_storage_path).resolve()
    target = (root / str(workspace_id)).resolve()
    # A workspace id is a UUID and cannot traverse, but the check is cheap and the
    # consequence of being wrong is deleting someone else's evidence.
    if not target.is_relative_to(root) or target == root or not target.exists():
        return 0
    files = sum(1 for path in target.rglob("*") if path.is_file())
    shutil.rmtree(target, ignore_errors=True)
    return files


async def ingest_all(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    sources: list[SourceSystem] | None = None,
    run_id: str | None = None,
    include_bank_sample: bool = True,
    force_synthetic: bool = False,
    dataset_seed: int | str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    """Run every configured connector, then the bank statement.

    A webhook-triggered or scheduled re-run is safe: the idempotency claim below
    stops two runs for the same workspace overlapping, and every write is an upsert.

    `dataset_seed` swaps the demonstration roster for a generated one of the same
    shape. It changes only *who* the companies are — every amount, and therefore
    every ground-truth total the tests assert, is untouched. A run without a seed
    gets the §15 template, so existing behaviour is unchanged by default.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    sources = sources or list(CONNECTOR_REGISTRY.keys())

    # Guard against two simultaneous ingestions for one workspace racing on the
    # same source_ids. A second caller is told to wait rather than corrupting state.
    claimed = await claim_idempotency_key(f"ingest:{workspace_id}", ttl=600)
    if not claimed:
        emit(
            EventKind.ERROR,
            "Ingestion already running for this workspace",
            workspace_id=str(workspace_id),
            severity=Severity.WARNING,
            feature=1,
        )
        return {"error": "an ingestion run is already in progress", "run_id": run_id}

    emit(
        EventKind.AGENT_STEP,
        f"Feature 1: ingestion run {run_id} starting across {len(sources)} sources",
        workspace_id=str(workspace_id),
        feature=1,
        run_id=run_id,
        sources=[str(s) for s in sources],
    )

    # The generated roster is bound for this run only, so two workspaces ingesting
    # at once cannot see each other's companies.
    roster_customers = None
    if dataset_seed is not None:
        from app.connectors.synthetic.generator import generate_roster

        roster_customers = generate_roster(dataset_seed)
        emit(
            EventKind.SYSTEM,
            f"Demonstration data generated from seed {dataset_seed!r}: "
            f"{len(roster_customers)} companies, none of them the built-in ones",
            workspace_id=str(workspace_id),
            feature=1,
            run_id=run_id,
        )

    # A demonstration dataset *replaces* the previous one; a live connector adds to
    # what it has already collected. Both were upserts keyed on `source_id`, so
    # loading a second generated roster left the first one behind: 20 customers
    # became 31, 234 records became 536, and every count measured over them — the
    # customer concentration above all — was computed across two unrelated
    # companies' books. "Generate demonstration data" reads as a replacement to
    # anyone who presses it, so that is what it now is.
    if replace_existing or dataset_seed is not None or force_synthetic:
        cleared = await _clear_synthetic_evidence(session, workspace_id=workspace_id)
        if cleared:
            emit(
                EventKind.SYSTEM,
                f"Cleared the previous demonstration dataset — {cleared} records "
                f"removed, so this one replaces it rather than being added to it",
                workspace_id=str(workspace_id),
                feature=1,
                run_id=run_id,
            )

    from app.connectors.synthetic import customers as roster

    results: dict[str, Any] = {}
    with roster.use_roster(roster_customers):
        try:
            connections = {
                connection.source_system: connection
                for connection in (
                    await session.execute(
                        select(ProviderConnection).where(
                            ProviderConnection.workspace_id == workspace_id
                        )
                    )
                ).scalars()
            }

            for source in sources:
                connection = connections.get(source)
                if connection is None:
                    connection = ProviderConnection(
                        workspace_id=workspace_id,
                        source_system=source,
                        display_name=str(source),
                        is_active=True,
                    )
                    session.add(connection)
                    await session.flush()

                token = None
                if connection.encrypted_access_token:
                    from app.core.crypto import get_cipher

                    token = get_cipher().decrypt(connection.encrypted_access_token)
                if not token:
                    # No per-workspace token stored: fall back to the deployment-wide
                    # credential and mint a fresh access token. A hand-pasted token
                    # expires within the hour, so minting is what makes a connection
                    # survive past its first run.
                    from app.connectors.auth import access_token_for

                    token = await access_token_for(source, workspace_id=str(workspace_id))

                stats = await ingest_source(
                    session,
                    workspace_id=workspace_id,
                    source_system=source,
                    connection=connection,
                    run_id=run_id,
                    access_token=token,
                    force_synthetic=force_synthetic,
                )
                results[str(source)] = stats.as_dict()

            # Which sources actually served live data? A run where any connector
            # reached a real account must never have demonstration evidence mixed
            # into it — see the guard below.
            live_sources = [
                name
                for name, stats_dict in results.items()
                if not stats_dict.get("is_synthetic", False)
                and stats_dict.get("canonical_written", 0)
            ]

            if include_bank_sample:
                from app.connectors.bank_csv import synthetic_csv_bytes

                existing_bank = (
                    await session.execute(
                        select(BankTransaction).where(
                            BankTransaction.workspace_id == workspace_id
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if existing_bank is not None:
                    results["bank_csv"] = {
                        "skipped": "a bank statement is already loaded",
                        "canonical_written": 0,
                    }
                elif live_sources:
                    # The demonstration statement describes twenty invented companies.
                    # Seeding it beside a real Zoho ledger put fabricated bank rows
                    # into a workspace built from a founder's own books — and because
                    # no live payment can match an invented counterparty, every
                    # receipt failed MISSING_BANK_CONFIRMATION and the published
                    # total came out at zero. The wrong number was the smaller harm:
                    # this product exists to say where a figure came from, and it was
                    # quietly manufacturing the evidence it then failed to find.
                    emit(
                        EventKind.SYSTEM,
                        f"{len(live_sources)} source(s) returned live data "
                        f"({', '.join(sorted(live_sources))}), so the demonstration "
                        "bank statement was NOT loaded — real books must not be mixed "
                        "with invented evidence. Upload the bank statement covering "
                        "this period to let receipts reach bank-confirmed status; "
                        "until then every payment stops at 'verified by the processor'.",
                        workspace_id=str(workspace_id),
                        severity=Severity.WARNING,
                        feature=1,
                        run_id=run_id,
                    )
                    results["bank_csv"] = {
                        "skipped": "live evidence present; no bank statement connected",
                        "canonical_written": 0,
                    }
                else:
                    bank_stats = await ingest_bank_csv(
                        session,
                        workspace_id=workspace_id,
                        content=synthetic_csv_bytes(),
                        filename="synthetic_bank_statement.csv",
                        run_id=run_id,
                        # This is the demonstration dataset, not a real statement.
                        is_synthetic=True,
                    )
                    results["bank_csv"] = bank_stats.as_dict()

            await session.commit()
        finally:
            from app.core.cache import release_idempotency_key

            await release_idempotency_key(f"ingest:{workspace_id}")

    total = sum(r.get("canonical_written", 0) for r in results.values())
    emit(
        EventKind.RESULT,
        f"Feature 1 complete: {total} canonical records across {len(results)} sources",
        workspace_id=str(workspace_id),
        severity=Severity.SUCCESS,
        feature=1,
        run_id=run_id,
    )
    return {"run_id": run_id, "sources": results, "total_canonical": total}


# ---------------------------------------------------------------------------
# Deployment-wide connections
# ---------------------------------------------------------------------------


async def seed_deployment_connections(
    session: AsyncSession, *, workspace_id: uuid.UUID
) -> list[str]:
    """Attach every provider this deployment already holds credentials for.

    The credentials live in the environment, not in a workspace, so a new workspace
    on a configured deployment is *already* able to reach those systems — the
    ingestion path falls back to the deployment credential and mints a token. What
    was missing was any record saying so, and without one the UI showed four
    providers as "demo data" and disabled the option to use the real ones. A person
    running a demonstration then had no way to reach their own connected accounts
    without pasting credentials the server already had.

    So the record is created up front and marked live. A workspace on a deployment
    with no credentials still gets nothing, and still correctly reads as synthetic.
    """
    status = settings.provider_status()
    connectable = [
        source
        for source in (
            SourceSystem.RAZORPAY,
            SourceSystem.ZOHO_BOOKS,
            SourceSystem.GOOGLE_DRIVE,
            SourceSystem.HUBSPOT,
        )
        if status.get(str(source))
    ]
    if not connectable:
        return []

    existing = {
        connection.source_system
        for connection in (
            await session.execute(
                select(ProviderConnection).where(
                    ProviderConnection.workspace_id == workspace_id
                )
            )
        ).scalars()
    }

    attached: list[str] = []
    for source in connectable:
        if source in existing:
            continue
        session.add(
            ProviderConnection(
                workspace_id=workspace_id,
                source_system=source,
                display_name=str(source),
                is_active=True,
                # Live, because the deployment can genuinely reach it. Nothing here
                # asserts the *data* is real — a run may still be told to serve the
                # demonstration dataset, and it is labelled synthetic when it is.
                is_synthetic=False,
                is_test_mode=True,
            )
        )
        attached.append(str(source))

    if attached:
        await session.flush()
        emit(
            EventKind.SYSTEM,
            f"Connected {len(attached)} provider(s) from the deployment's own "
            f"credentials: {', '.join(attached)} — no sign-in needed",
            workspace_id=str(workspace_id),
            feature=1,
        )
    return attached
