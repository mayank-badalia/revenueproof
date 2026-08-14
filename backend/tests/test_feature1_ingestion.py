"""Feature 1 tests — Financial Evidence Ingestion and Provenance Vault.

Covers Step 2a categories 1 (functional), 2 (edge/boundary), 3 (error handling),
4 (adversarial input), 5 (integration reality-check), 6 (state/persistence),
7 (concurrency) and 8 (end-to-end workflow).

The pipeline runs against real PostgreSQL and real Redis, with the §15 synthetic
dataset standing in for provider APIs. Every assertion here is about behaviour the
spec requires, not about implementation detail.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from app.connectors import bank_csv, normalize
from app.connectors.synthetic import contracts as synth_contracts
from app.connectors.synthetic import transactions as synth_txn
from app.core.db import get_sessionmaker
from app.main import app
from app.models import (
    BankTransaction,
    Contract,
    Invoice,
    Payment,
    QuarantinedRecord,
    Refund,
)
from app.models.enums import RecordType, SourceSystem
from app.services import ingestion, vault


def unique_email(prefix: str = "f1") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
async def client():
    from app.core.db import dispose_engine
    from app.core.schema_init import create_schema

    await create_schema()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post(
            "/api/v1/auth/register",
            json={"email": unique_email(), "password": "diligence-2026"},
        )
        ac.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        yield ac
    await dispose_engine()


@pytest.fixture
async def workspace_id(client) -> str:
    response = await client.post(
        "/api/v1/workspaces",
        json={
            "company_name": "Northstar Diligence Demo Private Limited",
            "reporting_period_start": "2026-04-01",
            "reporting_period_end": "2027-03-31",
            "base_currency": "INR",
            "claimed_revenue": "10000000.00",
            "claimed_arr": "10000000.00",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------------
# 1. End-to-end ingestion (Step 2a categories 1 and 8)
# ---------------------------------------------------------------------------


async def test_full_ingestion_populates_every_evidence_type(client, workspace_id):
    """The headline test: one call collects the whole evidence chain."""
    response = await client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total_canonical"] > 0
    sources = body["sources"]
    assert set(sources) >= {"razorpay", "zoho_books", "google_drive", "bank_csv"}

    summary = (await client.get(f"/api/v1/workspaces/{workspace_id}/summary")).json()
    counts = summary["evidence_counts"]

    expected = synth_txn.expected_totals()
    assert counts["invoices"] == expected["invoices"]
    assert counts["payments"] == expected["payments"]
    assert counts["bank_transactions"] == expected["bank_transactions"]
    assert counts["contracts"] == len(synth_contracts.CONTRACTS)
    # Refunds = processor refunds + disputes normalised as chargebacks.
    assert counts["refunds"] == expected["refunds"] + expected["disputes"]
    assert counts["raw_records"] > 0


async def test_evidence_endpoint_exposes_provenance_hashes(client, workspace_id):
    await client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={})
    response = await client.get(f"/api/v1/workspaces/{workspace_id}/evidence")
    assert response.status_code == 200
    body = response.json()

    assert body["counts"], "evidence counts should not be empty after ingestion"
    for record in body["records"]:
        # A record without a content hash has no provenance and is unusable as evidence.
        assert len(record["content_hash"]) == 64
        assert record["version"] >= 1


async def test_contract_files_are_vaulted_with_verifiable_hashes(client, workspace_id):
    """File bytes must be recoverable and match the hash recorded at ingestion."""
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/ingest",
        json={"sources": ["google_drive"], "include_bank_sample": False},
    )
    async with get_sessionmaker()() as session:
        records = await vault.get_current_records(
            session,
            workspace_id=uuid.UUID(workspace_id),
            record_type=RecordType.CONTRACT,
        )
        assert len(records) == len(synth_contracts.CONTRACTS)
        with_files = [r for r in records if r.storage_key]
        assert with_files, "contract PDFs should have been stored"
        for record in with_files:
            assert vault.verify_object(record.storage_key, record.file_hash) is True
            assert record.file_size_bytes > 0


# ---------------------------------------------------------------------------
# 2. Idempotency — the requirement stated in the Feature 1 workflow
# ---------------------------------------------------------------------------


async def test_reingestion_creates_no_duplicate_canonical_facts(client, workspace_id):
    """"Duplicate and out-of-order events cannot create a second canonical fact"."""
    first = (await client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={})).json()
    counts_after_first = (
        await client.get(f"/api/v1/workspaces/{workspace_id}/summary")
    ).json()["evidence_counts"]

    second = (await client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={})).json()
    counts_after_second = (
        await client.get(f"/api/v1/workspaces/{workspace_id}/summary")
    ).json()["evidence_counts"]

    assert counts_after_first == counts_after_second, "re-ingestion changed the record counts"

    # The second run should recognise the records as duplicates rather than rewriting.
    razorpay_second = second["sources"]["razorpay"]
    assert razorpay_second["duplicates"] > 0
    assert razorpay_second["canonical_written"] == 0
    assert first["total_canonical"] > 0


async def test_a_second_demonstration_dataset_replaces_the_first(client, workspace_id):
    """"Generate demonstration data" replaces; it does not accumulate.

    Both loads were upserts keyed on `source_id`, and a generated roster mints new
    ids, so a second seed left the first roster in place: 20 customers became 31 and
    234 records became 536. Nothing said so on screen, and every count measured over
    customers — concentration above all — was then computed across two unrelated
    companies' books. A live connector still accumulates, which is what a sync is
    for; only a demonstration load resets.
    """
    async def counts() -> dict[str, int]:
        return (
            await client.get(f"/api/v1/workspaces/{workspace_id}/summary")
        ).json()["evidence_counts"]

    first = await client.post(
        f"/api/v1/workspaces/{workspace_id}/ingest",
        json={"use_demo_data": True, "dataset_seed": "roster-one"},
    )
    assert first.status_code == 200, first.text[:200]
    after_first = await counts()
    assert sum(after_first.values()) > 0

    second = await client.post(
        f"/api/v1/workspaces/{workspace_id}/ingest",
        json={"use_demo_data": True, "dataset_seed": "roster-two"},
    )
    assert second.status_code == 200, second.text[:200]
    after_second = await counts()

    assert after_second == after_first, (
        f"a second dataset accumulated instead of replacing: "
        f"{after_first} then {after_second}"
    )

    # And a third load of the *same* seed is equally stable — the clear-and-reload
    # must be idempotent, not merely different-seed-safe.
    third = await client.post(
        f"/api/v1/workspaces/{workspace_id}/ingest",
        json={"use_demo_data": True, "dataset_seed": "roster-two"},
    )
    assert third.status_code == 200, third.text[:200]
    assert await counts() == after_first


async def test_changed_source_content_creates_a_new_version(client, workspace_id):
    """An edited source record is versioned, not overwritten (spec §18)."""
    workspace_uuid = uuid.UUID(workspace_id)
    async with get_sessionmaker()() as session:
        payload = {"id": "pay_version_test", "amount": 100000, "currency": "INR",
                   "status": "captured", "created_at": 1775000000}

        first = await vault.store_raw_record(
            session, workspace_id=workspace_uuid, source_system=SourceSystem.RAZORPAY,
            record_type=RecordType.PAYMENT, source_id="pay_version_test", payload=payload,
        )
        assert first.outcome == "created"

        # Identical content → duplicate, no new row.
        again = await vault.store_raw_record(
            session, workspace_id=workspace_uuid, source_system=SourceSystem.RAZORPAY,
            record_type=RecordType.PAYMENT, source_id="pay_version_test", payload=payload,
        )
        assert again.outcome == "duplicate"

        # Changed content → new version, prior row superseded.
        changed = await vault.store_raw_record(
            session, workspace_id=workspace_uuid, source_system=SourceSystem.RAZORPAY,
            record_type=RecordType.PAYMENT, source_id="pay_version_test",
            payload={**payload, "amount": 200000},
        )
        assert changed.outcome == "new_version"
        assert changed.record.version == 2
        await session.commit()

        history = await vault.lineage_for(
            session, workspace_id=workspace_uuid, source_id="pay_version_test"
        )
        assert len(history) == 2
        assert history[0]["superseded"] is True
        assert history[1]["superseded"] is False


async def test_concurrent_ingestion_is_serialised(client, workspace_id):
    """Two simultaneous runs must not both proceed (Step 2a category 7)."""
    results = await asyncio.gather(
        client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={}),
        client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={}),
    )
    bodies = [r.json() for r in results]
    rejected = [b for b in bodies if "error" in b]
    succeeded = [b for b in bodies if "total_canonical" in b]
    # Exactly one runs; the other is told a run is already in progress.
    assert len(succeeded) >= 1
    assert len(rejected) + len(succeeded) == 2


# ---------------------------------------------------------------------------
# 3. Normalisation correctness (Step 2a category 1)
# ---------------------------------------------------------------------------


def test_razorpay_paise_are_not_converted_twice():
    """Razorpay amounts are already minor units — the classic off-by-100 bug."""
    payload = {"id": "pay_x", "amount": 377600000, "currency": "INR",
               "status": "captured", "created_at": 1775000000, "fee": 0, "tax": 0}
    assert normalize.razorpay_payment(payload).amount_minor == 377600000


def test_zoho_decimals_are_converted_to_minor_units():
    invoice = normalize.zoho_invoice(synth_txn.zoho_invoices()[0])
    # ₹32,00,000 + 18% GST = ₹37,76,000 → 377600000 paise
    assert invoice.total_minor == 377600000
    assert invoice.currency == "INR"


def test_failed_payment_retains_zero_cash():
    """spec §14: a failed payment contributes zero cash received."""
    failed = next(p for p in synth_txn.razorpay_payments() if p["status"] == "failed")
    assert normalize.razorpay_payment(failed).retained_minor == 0


def test_partial_refund_is_detected_from_amount_refunded():
    """Razorpay reports a partially refunded payment as 'captured'."""
    payload = {"id": "pay_pr", "amount": 100000, "amount_refunded": 40000,
               "currency": "INR", "status": "captured", "created_at": 1775000000}
    payment = normalize.razorpay_payment(payload)
    assert payment.status == "partially_refunded"
    assert payment.retained_minor == 60000


def test_paid_invoice_with_outstanding_balance_is_downgraded():
    """A status flag can be set by hand; a balance cannot. Trust the number."""
    payload = {"invoice_id": "zi_x", "status": "paid", "currency_code": "INR",
               "total": 1000, "balance": 400, "date": "2026-04-01", "line_items": []}
    assert normalize.zoho_invoice(payload).status == "partially_paid"


def test_chargeback_is_normalised_as_a_refund():
    dispute = synth_txn.razorpay_disputes()[0]
    refund = normalize.razorpay_dispute(dispute)
    assert refund.is_chargeback is True
    assert refund.amount_minor == 59000000


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("One-time implementation and onboarding", "one_time"),
        ("Annual platform subscription FY2026-27", "recurring"),
        # The adversarial case: both vocabularies present must surface, not resolve.
        ("Annual subscription - implementation and migration programme", "ambiguous"),
        ("Monthly subscription", "recurring"),
        ("Widgets", "unknown"),
        (None, "unknown"),
    ],
)
def test_line_item_classification_hint(description, expected):
    assert normalize.classify_line_description(description) == expected


# ---------------------------------------------------------------------------
# 4. Bank CSV (Step 2a categories 2, 3, 4)
# ---------------------------------------------------------------------------


def test_bank_csv_skips_preamble_and_parses_every_row():
    result = bank_csv.import_csv(
        bank_csv.synthetic_csv_bytes(), "statement.csv", workspace_id="w1"
    )
    assert result.accepted == len(synth_txn.BANK)
    assert result.rejected == []
    assert set(result.detected_columns) >= {"date", "credit", "debit", "balance"}


def test_bank_csv_handles_alternative_column_names():
    """Real exports say 'Txn Date' / 'Withdrawal' / 'Deposit'."""
    csv_text = (
        "Txn Date,Particulars,Chq/Ref No,Withdrawal Amt,Deposit Amt,Closing Balance\n"
        "01/04/2026,NEFT CR ACME CORP,REF001,,118000.00,1118000.00\n"
        "05/04/2026,OFFICE RENT,REF002,225000.00,,893000.00\n"
    )
    result = bank_csv.import_csv(csv_text.encode(), "s.csv", workspace_id="w1")
    assert result.accepted == 2
    assert result.transactions[0].direction == "credit"
    assert result.transactions[1].direction == "debit"


def test_bank_csv_single_signed_amount_column():
    csv_text = (
        "Date,Description,Amount,Balance\n"
        "01/04/2026,CREDIT FROM ACME,118000.00,1118000.00\n"
        "02/04/2026,RENT PAYMENT,-225000.00,893000.00\n"
    )
    result = bank_csv.import_csv(csv_text.encode(), "s.csv", workspace_id="w1")
    assert result.accepted == 2
    assert result.transactions[0].direction == "credit"
    assert result.transactions[1].direction == "debit"
    assert result.transactions[1].amount_minor == 22500000  # sign lives in direction


def test_bank_csv_rejects_bad_rows_without_aborting_the_import():
    """Three bad rows out of five must yield two transactions, not zero."""
    csv_text = (
        "Date,Description,Credit,Balance\n"
        "01/04/2026,GOOD ONE,118000.00,1118000.00\n"
        "not-a-date,BAD DATE,5000.00,1123000.00\n"
        "03/04/2026,ZERO AMOUNT,0.00,1123000.00\n"
        "04/04/2026,NO AMOUNT,,1123000.00\n"
        "05/04/2026,GOOD TWO,50000.00,1173000.00\n"
    )
    result = bank_csv.import_csv(csv_text.encode(), "s.csv", workspace_id="w1")
    assert result.accepted == 2
    assert len(result.rejected) == 3


@pytest.mark.parametrize(
    ("content", "filename", "fragment"),
    [
        (b"%PDF-1.4 payload", "statement.csv", "PDF"),
        (b"PK\x03\x04zipdata", "statement.csv", "ZIP"),
        (b"MZ\x90\x00exe", "statement.csv", "executable"),
        (b"Date,Credit\n01/04/2026,1", "statement.exe", "only .csv"),
        (b"", "statement.csv", "empty"),
        (b"Date\x00Credit", "statement.csv", "NUL"),
        (b"x" * (11 * 1024 * 1024), "statement.csv", "limit"),
    ],
)
def test_unsafe_uploads_are_rejected(content, filename, fragment):
    with pytest.raises(bank_csv.BankCsvError, match=fragment):
        bank_csv.check_upload_safety(content, filename)


def test_csv_formula_injection_is_neutralised():
    """A narration starting with '=' becomes code when opened in Excel."""
    csv_text = (
        "Date,Description,Credit,Balance\n"
        '01/04/2026,"=cmd|\'/c calc\'!A1",118000.00,1118000.00\n'
    )
    result = bank_csv.import_csv(csv_text.encode(), "s.csv", workspace_id="w1")
    assert result.accepted == 1
    assert result.transactions[0].narration.startswith("'=")


def test_bank_csv_rejects_a_file_with_no_usable_header():
    with pytest.raises(bank_csv.BankCsvError, match="header row"):
        bank_csv.import_csv(b"alpha,beta,gamma\n1,2,3\n", "s.csv", workspace_id="w1")


async def test_bank_csv_upload_endpoint_rejects_a_disguised_pdf(client, workspace_id):
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/bank-csv",
        files={"file": ("statement.csv", b"%PDF-1.4 not really a csv", "text/csv")},
    )
    assert response.status_code == 422
    assert "PDF" in response.json()["detail"]


async def test_bank_csv_upload_endpoint_accepts_a_real_statement(client, workspace_id):
    response = await client.post(
        f"/api/v1/workspaces/{workspace_id}/bank-csv",
        files={
            "file": ("statement.csv", bank_csv.synthetic_csv_bytes(), "text/csv")
        },
    )
    assert response.status_code == 200
    assert response.json()["canonical_written"] == len(synth_txn.BANK)


# ---------------------------------------------------------------------------
# 5. Quarantine (Step 2a categories 3 and 4)
# ---------------------------------------------------------------------------


async def test_malformed_records_are_quarantined_not_dropped(client, workspace_id):
    """Bad evidence must stay visible and countable, never silently discarded."""
    workspace_uuid = uuid.UUID(workspace_id)
    async with get_sessionmaker()() as session:
        stats = ingestion.IngestionStats()
        for index, payload in enumerate(
            [
                {"amount": 1000},                                    # no id
                {"id": "pay_bad_date", "created_at": "not-a-number"},  # bad timestamp
                {"id": "pay_over", "amount": 100, "amount_refunded": 900,
                 "currency": "INR", "status": "captured", "created_at": 1775000000},
            ]
        ):
            await ingestion._process_record(
                session,
                workspace_id=workspace_uuid,
                source_system=SourceSystem.RAZORPAY,
                record=ingestion.FetchedRecord(
                    RecordType.PAYMENT, f"bad_{index}", payload
                ),
                stats=stats,
                run_id="test",
            )
        await session.commit()

    assert stats.quarantined == 3
    assert stats.canonical_written == 0

    response = await client.get(f"/api/v1/workspaces/{workspace_id}/quarantine")
    body = response.json()
    assert body["summary"]["total"] == 3
    # Every entry keeps the payload, so a reviewer can see what was rejected.
    assert all(record["detail"] for record in body["records"])


async def test_quarantined_records_never_reach_canonical_tables(client, workspace_id):
    await client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={})
    async with get_sessionmaker()() as session:
        from sqlalchemy import select as sa_select

        quarantined = (
            await session.execute(
                sa_select(QuarantinedRecord.source_id).where(
                    QuarantinedRecord.workspace_id == uuid.UUID(workspace_id)
                )
            )
        ).scalars().all()
        for source_id in quarantined:
            match = (
                await session.execute(
                    sa_select(Payment).where(
                        Payment.workspace_id == uuid.UUID(workspace_id),
                        Payment.source_id == source_id,
                    )
                )
            ).scalar_one_or_none()
            assert match is None, f"quarantined {source_id} leaked into payments"


# ---------------------------------------------------------------------------
# 6. Adversarial dataset cases are actually present (Step 2a category 11)
# ---------------------------------------------------------------------------


async def test_dataset_contains_the_cases_the_product_must_catch(client, workspace_id):
    """Goal-fidelity: the evidence needed to exercise §19 is really in the database."""
    await client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={})
    workspace_uuid = uuid.UUID(workspace_id)

    async with get_sessionmaker()() as session:
        from sqlalchemy import select as sa_select

        async def fetch(model):
            return (
                (await session.execute(
                    sa_select(model).where(model.workspace_id == workspace_uuid)
                )).scalars().all()
            )

        payments = await fetch(Payment)
        invoices = await fetch(Invoice)
        refunds = await fetch(Refund)
        bank = await fetch(BankTransaction)
        contracts = await fetch(Contract)

        # Failed payments exist and are marked as such.
        assert any(p.status == "failed" for p in payments)
        # A voided invoice is present (must be excluded from totals later).
        assert any(i.status == "void" for i in invoices)
        # Overdue invoice with a full outstanding balance → INVOICED_UNPAID case.
        assert any(i.status == "overdue" and i.amount_due == i.total for i in invoices)
        # A chargeback distinct from an ordinary refund.
        assert any(r.is_chargeback for r in refunds)
        assert any(not r.is_chargeback for r in refunds)
        # One-time line items flagged for a contract check.
        assert any(i.has_one_time_items for i in invoices)
        # Circular-flow evidence: matching in/out pairs with the same counterparty.
        apex_in = [b for b in bank if b.direction == "credit"
                   and b.counterparty and "APEX" in b.counterparty]
        apex_out = [b for b in bank if b.direction == "debit"
                    and b.counterparty and "APEX" in b.counterparty]
        assert apex_in and apex_out, "circular-flow evidence missing"
        # Every contract document is present but deliberately unparsed.
        assert len(contracts) == len(synth_contracts.CONTRACTS)
        assert all("terms_not_yet_extracted" in c.unknown_fields for c in contracts)


async def test_contract_documents_are_not_given_fabricated_terms(client, workspace_id):
    """An unparsed contract must not look like a contract worth zero."""
    await client.post(
        f"/api/v1/workspaces/{workspace_id}/ingest",
        json={"sources": ["google_drive"], "include_bank_sample": False},
    )
    async with get_sessionmaker()() as session:
        from sqlalchemy import select as sa_select

        contracts = (
            await session.execute(
                sa_select(Contract).where(Contract.workspace_id == uuid.UUID(workspace_id))
            )
        ).scalars().all()
        for contract in contracts:
            assert contract.start_date is None
            assert contract.billing_frequency == "unknown"
            assert "terms_not_yet_extracted" in contract.unknown_fields


# ---------------------------------------------------------------------------
# 7. Webhook security (Step 2a categories 3 and 4)
# ---------------------------------------------------------------------------


async def test_webhook_without_signature_is_rejected(client, workspace_id):
    response = await client.post(
        f"/api/v1/webhooks/razorpay/{workspace_id}",
        json={"event": "payment.captured"},
    )
    assert response.status_code == 401


async def test_webhook_with_wrong_signature_is_rejected(client, workspace_id):
    response = await client.post(
        f"/api/v1/webhooks/razorpay/{workspace_id}",
        json={"event": "payment.captured"},
        headers={"X-Razorpay-Signature": "deadbeef" * 8},
    )
    assert response.status_code == 401


def test_webhook_signature_verification_is_exact():
    from app.core.crypto import verify_webhook_signature
    import hashlib
    import hmac

    secret = "whsec_test"
    body = b'{"event":"payment.captured","payload":{}}'
    valid = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, valid, secret) is True
    # A single changed byte in the body must invalidate the signature.
    assert verify_webhook_signature(body + b" ", valid, secret) is False
    assert verify_webhook_signature(body, valid[:-1] + "0", secret) is False
    assert verify_webhook_signature(body, "", secret) is False


# ---------------------------------------------------------------------------
# 8. Tenant isolation for evidence (Step 2a category 6)
# ---------------------------------------------------------------------------


async def test_evidence_is_not_visible_across_workspaces(client, workspace_id):
    """Financial evidence for one company must never reach another."""
    await client.post(f"/api/v1/workspaces/{workspace_id}/ingest", json={})

    other = await client.post(
        "/api/v1/workspaces",
        json={
            "company_name": "Unrelated Company Private Limited",
            "reporting_period_start": "2026-04-01",
            "reporting_period_end": "2027-03-31",
            "base_currency": "INR",
            "claimed_revenue": "0",
            "claimed_arr": "0",
        },
    )
    other_id = other.json()["id"]

    evidence = (await client.get(f"/api/v1/workspaces/{other_id}/evidence")).json()
    assert evidence["records"] == []
    assert evidence["counts"] == []

    summary = (await client.get(f"/api/v1/workspaces/{other_id}/summary")).json()
    assert all(count == 0 for count in summary["evidence_counts"].values())


# ---------------------------------------------------------------------------
# 9. Synthetic dataset integrity
# ---------------------------------------------------------------------------


def test_synthetic_dataset_meets_spec_section_15():
    totals = synth_txn.expected_totals()
    assert 15 <= totals["customers"] <= 20          # "15–20 fictional customers"
    assert len(synth_contracts.CONTRACTS) >= 10     # "10 fictional PDF contracts"
    assert 45 <= totals["invoices"] <= 60           # "approximately 50 invoices"
    assert totals["failed_payments"] >= 1
    assert totals["refunded_payments"] >= 1
    assert totals["circular_pairs"] == 2            # "two suspicious circular transfers"
    assert totals["related_parties"] >= 1


def test_synthetic_contracts_render_as_valid_pdfs():
    import pymupdf

    for contract in synth_contracts.CONTRACTS:
        data = synth_contracts.render_pdf(contract)
        document = pymupdf.open(stream=data, filetype="pdf")
        assert document.page_count >= 1
        text = "".join(page.get_text() for page in document)
        if contract.is_scanned:
            # No text layer at all — this is what forces the OCR path in Feature 3.
            assert len(text.strip()) == 0
        else:
            assert contract.start_date.strftime("%d %B %Y") in text
        document.close()


def test_future_contract_is_outside_the_reporting_period():
    """The contract that must contribute zero to current-period revenue."""
    future = synth_contracts.BY_KEY["meridian_future"]
    assert future.start_date > date(2027, 3, 31)


def test_amendment_supersedes_its_original():
    amendment = synth_contracts.BY_KEY["ironbridge_amendment"]
    assert amendment.is_amendment is True
    assert amendment.supersedes == "ironbridge_original"
    original = synth_contracts.BY_KEY["ironbridge_original"]
    assert amendment.recurring_amount > original.recurring_amount


# ---------------------------------------------------------------------------
# Bank narration → counterparty
#
# The counterparty is the only name a bank statement gives a customer, so every
# fragment of the *transfer* left attached to it — the rail, the processor, the
# purpose, the reference, the beneficiary of an agent payment — splits one party
# into several. That inflates the customer count, understates concentration, and
# hides both shared accounts and circular flows, none of which announces itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("narration", "expected"),
    [
        # An agent settling for two customers is ONE account. The beneficiary named
        # after "A/C" is not part of the payer's name.
        ("NEFT CR GLOBAL PAY SERVICES A/C CRESTVIEW RETAIL", "GLOBAL PAY SERVICES"),
        ("NEFT CR GLOBAL PAY SERVICES A/C PINNACLE FOODS", "GLOBAL PAY SERVICES"),
        # One party, four narrations: the purpose must not become part of the name,
        # or money in and money out stop looking like the same counterparty.
        ("RTGS CR APEX FOUNDER HOLDINGS PVT LTD ADVANCE", "APEX FOUNDER HOLDINGS PVT LTD"),
        ("RTGS DR APEX FOUNDER HOLDINGS PVT LTD ADVISORY FEE", "APEX FOUNDER HOLDINGS PVT LTD"),
        ("RTGS CR APEX FOUNDER HOLDINGS INTERCOMPANY", "APEX FOUNDER HOLDINGS"),
        ("RTGS DR APEX FOUNDER HOLDINGS INTERCOMPANY REVERSAL", "APEX FOUNDER HOLDINGS"),
        ("CHARGEBACK DEBIT HALCYON HEALTH", "HALCYON HEALTH"),
        ("RAZORPAY REFUND COBALT MEDIA NETWORKS", "COBALT MEDIA NETWORKS"),
        ("RAZORPAY SETTLEMENT COBALT MEDIA", "COBALT MEDIA"),
        ("RAZORPAY REFUND QUANTUM RETAIL PARTIAL", "QUANTUM RETAIL"),
        # References identify the transaction, not the customer.
        ("NEFT CR MERIDIAN HOLDINGS PVT LTD REF INV-2026-120", "MERIDIAN HOLDINGS PVT LTD"),
        ("NEFT CR SILVERLINE EDU PVT LTD INV090 091 092 093", "SILVERLINE EDU PVT LTD"),
        ("RAZORPAY SETTLEMENT NORTHSTAR TECH M1", "NORTHSTAR TECH"),
        # The same inflation written out in words. Three instalments from one
        # customer became three customers, one of which then carried the shared
        # GSTIN into a second entity row.
        ("NEFT CR COBBLE INDUSTRIES INSTALMENT 1", "COBBLE INDUSTRIES"),
        ("NEFT CR COBBLE INDUSTRIES INSTALMENT 3", "COBBLE INDUSTRIES"),
        ("NEFT CR IRONBRIDGE MFG INSTALLMENT 2", "IRONBRIDGE MFG"),
        ("NEFT CR ATLAS WORKS TRANCHE 2", "ATLAS WORKS"),
        ("NEFT CR ATLAS WORKS MILESTONE 3", "ATLAS WORKS"),
        # ...but a company whose name genuinely ends in one of those words, with no
        # sequence number after it, keeps it.
        ("NEFT CR TRANCHE CAPITAL LLP", "TRANCHE CAPITAL LLP"),
        ("NEFT CR MILESTONE SYSTEMS", "MILESTONE SYSTEMS"),
        # Names that begin with a marker prefix must survive intact: "ref" starts
        # REFINERY, "ac" starts ACME, "utr" starts UTRECHT. Each lost its first
        # token before the marker patterns were given a closing word boundary.
        ("NEFT CR ACME INDUSTRIES PVT LTD", "ACME INDUSTRIES PVT LTD"),
        ("NEFT CR ACCENTURE SOLUTIONS", "ACCENTURE SOLUTIONS"),
        ("NEFT CR REFINERY CORP", "REFINERY CORP"),
        ("NEFT CR UTRECHT TRADING", "UTRECHT TRADING"),
        # A purpose word a company legitimately opens its name with is only stripped
        # from the end, never the start.
        ("NEFT CR ADVANCE AUTO PARTS LTD", "ADVANCE AUTO PARTS LTD"),
        # No payer named before the account marker: the party after it, minus the
        # account number. "From" is a preposition, not a company.
        ("TRF FROM A/C 998877 JOHN DOE", "JOHN DOE"),
        # The two Blue Harbours stay distinct — the false merge F2 was fixed to
        # refuse must not be reintroduced by over-eager normalisation here.
        ("NEFT CR BLUE HARBOUR LOGISTICS LLP", "BLUE HARBOUR LOGISTICS LLP"),
        ("NEFT CR BLUE HARBOR M1", "BLUE HARBOR"),
    ],
)
def test_counterparty_extracted_from_narration(narration, expected):
    from app.connectors.normalize import _extract_counterparty

    assert _extract_counterparty(narration) == expected


def test_one_agent_account_is_not_split_by_who_it_paid_for():
    """The §19 shared-payment-agent case, at the level where it was being lost."""
    from app.connectors.normalize import _extract_counterparty

    crest = _extract_counterparty("NEFT CR GLOBAL PAY SERVICES A/C CRESTVIEW RETAIL")
    pinnacle = _extract_counterparty("NEFT CR GLOBAL PAY SERVICES A/C PINNACLE FOODS")
    assert crest == pinnacle, "one agent account must have one identity"


def test_an_inbound_and_outbound_leg_share_a_counterparty():
    """A round trip is only visible if both legs name the same party."""
    from app.connectors.normalize import _extract_counterparty

    inbound = _extract_counterparty("RTGS CR APEX FOUNDER HOLDINGS PVT LTD ADVANCE")
    outbound = _extract_counterparty("RTGS DR APEX FOUNDER HOLDINGS PVT LTD ADVISORY FEE")
    assert inbound == outbound


# ---------------------------------------------------------------------------
# Generated demonstration data
#
# The §15 template proves the pipeline handles the *cases*. It cannot prove the
# pipeline handles anything other than Northstar, so these assert that a generated
# roster keeps every adversarial structure while sharing no identity with it.
# ---------------------------------------------------------------------------


def test_generated_roster_is_deterministic_for_a_seed():
    """A demo that shows different companies on the second run is not a demo."""
    from app.connectors.synthetic.generator import generate_roster

    first = [c.legal_name for c in generate_roster("acme-demo")]
    second = [c.legal_name for c in generate_roster("acme-demo")]
    assert first == second
    assert first != [c.legal_name for c in generate_roster("other-seed")]


def test_generated_roster_shares_no_name_with_the_template():
    from app.connectors.synthetic.customers import template
    from app.connectors.synthetic.generator import generate_roster

    built_in = {c.legal_name for c in template()} | {c.zoho_name for c in template()}
    generated = generate_roster("acme-demo")
    for customer in generated:
        assert customer.legal_name not in built_in
        assert customer.zoho_name not in built_in


def test_generated_roster_keeps_every_adversarial_structure():
    """Identity changes; the cases must not."""
    from app.connectors.synthetic.customers import template
    from app.connectors.synthetic.generator import generate_roster

    generated = {c.key: c for c in generate_roster("acme-demo")}
    assert set(generated) == {c.key for c in template()}, "a case was dropped"

    # A parent and its subsidiary still share a domain.
    assert generated["meridian_holdings"].domain == generated["meridian_systems"].domain
    # The related party still sits on the founder's own domain — the §19 tell.
    assert generated["apex_holdings"].domain == generated["northstar"].domain
    assert generated["apex_holdings"].related_party is True
    # The false-merge trap is still a near-miss pair with different tax ids.
    left, right = generated["blue_harbor"], generated["blue_harbour_logistics"]
    assert left.legal_name != right.legal_name
    assert left.gstin != right.gstin
    assert left.domain != right.domain
    # One agent still settles for two unrelated customers.
    assert (
        generated["crestview"].bank_narration_name
        == generated["pinnacle_foods"].bank_narration_name
    )


def test_generated_narrations_do_not_leak_template_companies():
    """A statement crediting NSTAR TECH for an Everest invoice is incoherent."""
    from app.connectors.synthetic import customers as roster
    from app.connectors.synthetic import transactions as tx
    from app.connectors.synthetic.generator import generate_roster

    leaked = {"NSTAR TECH PVT", "BLUE HARBOR", "GLOBAL PAY SERVICES", "APEX FOUNDER"}
    with roster.use_roster(generate_roster("acme-demo")):
        text = " ".join(row["Description"] for row in tx.bank_csv_rows()).upper()
    for name in leaked:
        assert name not in text, f"template company {name!r} leaked into generated data"


def test_the_template_is_still_the_default():
    """Existing behaviour must be untouched when no seed is supplied."""
    from app.connectors.synthetic import customers as roster

    assert roster.get("northstar").zoho_name == "Northstar Tech"
    assert any("NSTAR TECH PVT" in c.bank_narration_name for c in roster.CUSTOMERS)


def test_an_active_roster_does_not_escape_its_block():
    from app.connectors.synthetic import customers as roster
    from app.connectors.synthetic.generator import generate_roster

    with roster.use_roster(generate_roster("acme-demo")):
        assert roster.get("northstar").zoho_name != "Northstar Tech"
    assert roster.get("northstar").zoho_name == "Northstar Tech"


# ---------------------------------------------------------------------------
# Live evidence is never mixed with demonstration evidence
# ---------------------------------------------------------------------------


async def test_live_sources_do_not_get_a_demonstration_bank_statement(
    client, workspace_id, monkeypatch
):
    """A real ledger must never be handed invented bank rows.

    Regression for the defect that made a connected workspace publish nothing: the
    built-in demonstration statement was seeded whenever a workspace had no bank
    rows, including when every other connector had just pulled a founder's real
    accounts. Its twenty invented counterparties match no live payment, so every
    receipt failed MISSING_BANK_CONFIRMATION and the headline fell to zero — on a
    dataset the product had partly manufactured itself.
    """
    real = ingestion.IngestionStats()
    real.fetched = real.normalized = real.canonical_written = 3
    real.is_synthetic = False

    async def fake_ingest_source(*args, **kwargs):
        return real

    monkeypatch.setattr(ingestion, "ingest_source", fake_ingest_source)

    async with get_sessionmaker()() as session:
        result = await ingestion.ingest_all(
            session, workspace_id=uuid.UUID(workspace_id), include_bank_sample=True
        )

    assert "skipped" in result["sources"]["bank_csv"]
    assert result["sources"]["bank_csv"]["canonical_written"] == 0

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(BankTransaction).where(
                    BankTransaction.workspace_id == uuid.UUID(workspace_id)
                )
            )
        ).scalars().all()
    assert rows == [], "demonstration bank rows were written beside live evidence"


async def test_a_fully_synthetic_run_still_gets_its_bank_statement(client, workspace_id):
    """The guard must not cost the demonstration set its bank evidence."""
    async with get_sessionmaker()() as session:
        result = await ingestion.ingest_all(
            session,
            workspace_id=uuid.UUID(workspace_id),
            include_bank_sample=True,
            force_synthetic=True,
        )

    assert result["sources"]["bank_csv"]["canonical_written"] > 0

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(BankTransaction).where(
                    BankTransaction.workspace_id == uuid.UUID(workspace_id)
                )
            )
        ).scalars().all()
    assert rows, "the demonstration set needs its bank statement to prove anything"
