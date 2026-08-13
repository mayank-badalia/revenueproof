"""Provider connectors — Feature 1, sub-features 2 and 3.

Each class talks to the real API using the request/response shapes from the ranked
documentation in `core_resoruces.md`, and falls back to the §15 synthetic dataset
when no credential is configured.

The rule that shapes all four: **webhooks are hints, APIs are authoritative.**
core_resoruces.md's second-pass merge downgraded webhooks from "source data" to
change notifications. A webhook tells us *something changed*; the connector then
refetches the record from the API and vaults that. This is why a duplicated or
out-of-order webhook delivery cannot corrupt the evidence — it only ever triggers a
re-read of current state.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import httpx

from app.connectors.base import Connector, FetchedRecord, FetchResult, paginate
from app.connectors.synthetic import contracts as synthetic_contracts
from app.connectors.synthetic import transactions as synthetic_txn
from app.core.config import settings
from app.core.events import EventKind, Severity, emit
from app.models.enums import RecordType, SourceSystem

# Zoho's API host differs per data-centre; the token from one region is invalid at
# another, which presents as a confusing 401 rather than a routing error.
ZOHO_HOSTS = {
    "in": "https://www.zohoapis.in",
    "com": "https://www.zohoapis.com",
    "eu": "https://www.zohoapis.eu",
    "au": "https://www.zohoapis.com.au",
}

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class RazorpayConnector(Connector):
    """Payment Connector Agent — payments, refunds, disputes and settlements."""

    source_system = SourceSystem.RAZORPAY
    display_name = "Razorpay"

    def has_credentials(self) -> bool:
        return bool(settings.razorpay_key_id and settings.razorpay_key_secret)

    def _auth_header(self) -> dict[str, str]:
        raw = f"{settings.razorpay_key_id}:{settings.razorpay_key_secret}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    async def fetch_live(self) -> FetchResult:
        result = FetchResult()
        headers = self._auth_header()
        # Resume from the last seen creation time rather than refetching history.
        since = int(self.cursor.get("payments_from") or 0)
        params: dict[str, Any] = {"count": 100}
        if since:
            params["from"] = since

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            # --- payments -------------------------------------------------
            response = await client.get(
                "https://api.razorpay.com/v1/payments", headers=headers, params=params
            )
            response.raise_for_status()
            payments = response.json().get("items", [])
            latest = since
            for payment in payments:
                result.records.append(
                    FetchedRecord(RecordType.PAYMENT, payment["id"], payment)
                )
                latest = max(latest, int(payment.get("created_at") or 0))

            # --- refunds --------------------------------------------------
            refunds = await client.get(
                "https://api.razorpay.com/v1/refunds", headers=headers, params={"count": 100}
            )
            if refunds.status_code == 200:
                for refund in refunds.json().get("items", []):
                    result.records.append(
                        FetchedRecord(RecordType.REFUND, refund["id"], refund)
                    )

            # --- disputes -------------------------------------------------
            # A refund-only integration misses chargebacks entirely, and a
            # chargeback removes cash exactly as a refund does.
            disputes = await client.get(
                "https://api.razorpay.com/v1/disputes", headers=headers, params={"count": 100}
            )
            if disputes.status_code == 200:
                for dispute in disputes.json().get("items", []):
                    result.records.append(
                        FetchedRecord(RecordType.DISPUTE, dispute["id"], dispute)
                    )
            elif disputes.status_code not in (403, 404):
                result.errors.append(f"disputes: HTTP {disputes.status_code}")

            # --- settlements ----------------------------------------------
            # Closes the processor-to-bank gap: "captured" is not "in the bank".
            settlements = await client.get(
                "https://api.razorpay.com/v1/settlements",
                headers=headers,
                params={"count": 100},
            )
            if settlements.status_code == 200:
                for settlement in settlements.json().get("items", []):
                    result.records.append(
                        FetchedRecord(RecordType.SETTLEMENT, settlement["id"], settlement)
                    )

        result.cursor = {"payments_from": latest + 1 if latest else None}
        result.has_more = len(payments) >= 100
        return result

    def fetch_synthetic(self) -> FetchResult:
        result = FetchResult()
        for payment in synthetic_txn.razorpay_payments():
            result.records.append(FetchedRecord(RecordType.PAYMENT, payment["id"], payment))
        for refund in synthetic_txn.razorpay_refunds():
            result.records.append(FetchedRecord(RecordType.REFUND, refund["id"], refund))
        for dispute in synthetic_txn.razorpay_disputes():
            result.records.append(FetchedRecord(RecordType.DISPUTE, dispute["id"], dispute))
        result.cursor = {"synthetic": True, "generated_at": datetime.now(UTC).isoformat()}
        return result

    @staticmethod
    def verify_webhook(raw_body: bytes, signature: str) -> bool:
        """HMAC-SHA256 over the raw body, per Razorpay's webhook documentation."""
        from app.core.crypto import verify_webhook_signature

        if not settings.razorpay_webhook_secret:
            return False
        return verify_webhook_signature(raw_body, signature, settings.razorpay_webhook_secret)


class ZohoBooksConnector(Connector):
    """Accounting Connector Agent — contacts, invoices, credit notes, payments."""

    source_system = SourceSystem.ZOHO_BOOKS
    display_name = "Zoho Books"

    def __init__(
        self,
        workspace_id: str,
        *,
        cursor=None,
        access_token: str | None = None,
        force_synthetic: bool = False,
    ):
        super().__init__(workspace_id, cursor=cursor, force_synthetic=force_synthetic)
        self.access_token = access_token

    def has_credentials(self) -> bool:
        # The token is minted per run from the refresh token (connectors/auth.py),
        # so its presence is the only live signal that matters here.
        return bool(self.access_token and settings.zoho_organization_id)

    @property
    def _base(self) -> str:
        return ZOHO_HOSTS.get(settings.zoho_region, ZOHO_HOSTS["in"])

    async def fetch_live(self) -> FetchResult:
        result = FetchResult()
        headers = {"Authorization": f"Zoho-oauthtoken {self.access_token}"}
        params: dict[str, Any] = {
            "organization_id": settings.zoho_organization_id,
            "per_page": 200,
        }
        # Incremental: only records modified since the last successful sync.
        if self.cursor.get("last_modified"):
            params["last_modified_time"] = self.cursor["last_modified"]

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            for endpoint, items_key, record_type, id_field in (
                ("contacts", "contacts", RecordType.CUSTOMER, "contact_id"),
                ("invoices", "invoices", RecordType.INVOICE, "invoice_id"),
                ("creditnotes", "creditnotes", RecordType.CREDIT_NOTE, "creditnote_id"),
                ("customerpayments", "customerpayments", RecordType.PAYMENT, "payment_id"),
            ):
                try:
                    items = await paginate(
                        client,
                        f"{self._base}/books/v3/{endpoint}",
                        headers=headers,
                        params=params,
                        items_key=items_key,
                        workspace_id=self.workspace_id,
                        label=f"Zoho {endpoint}",
                    )
                except httpx.HTTPStatusError as exc:
                    # One inaccessible endpoint (a scope the user did not grant)
                    # must not discard the endpoints that did work.
                    result.errors.append(f"{endpoint}: HTTP {exc.response.status_code}")
                    continue
                for item in items:
                    identifier = item.get(id_field)
                    if identifier:
                        result.records.append(
                            FetchedRecord(record_type, str(identifier), item)
                        )

        result.cursor = {"last_modified": datetime.now(UTC).isoformat()}
        return result

    def fetch_synthetic(self) -> FetchResult:
        result = FetchResult()
        for contact in synthetic_txn.zoho_contacts():
            result.records.append(
                FetchedRecord(RecordType.CUSTOMER, contact["contact_id"], contact)
            )
        for invoice in synthetic_txn.zoho_invoices():
            result.records.append(
                FetchedRecord(RecordType.INVOICE, invoice["invoice_id"], invoice)
            )
        for note in synthetic_txn.zoho_credit_notes():
            result.records.append(
                FetchedRecord(RecordType.CREDIT_NOTE, note["creditnote_id"], note)
            )
        result.cursor = {"synthetic": True}
        return result


class GoogleDriveConnector(Connector):
    """Document Collector Agent — locates and downloads contract files."""

    source_system = SourceSystem.GOOGLE_DRIVE
    display_name = "Google Drive"

    # Only formats the contract reader can actually process.
    CONTRACT_MIMES = (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.google-apps.document",
    )
    # Refuse to pull a file large enough to exhaust memory during parsing.
    MAX_FILE_BYTES = 50 * 1024 * 1024

    def __init__(
        self,
        workspace_id: str,
        *,
        cursor=None,
        access_token: str | None = None,
        force_synthetic: bool = False,
    ):
        super().__init__(workspace_id, cursor=cursor, force_synthetic=force_synthetic)
        self.access_token = access_token

    def has_credentials(self) -> bool:
        # A service-account token is as real as a user-consented one; requiring
        # google_client_id would reject the credential shape this app actually uses.
        return bool(self.access_token)

    async def fetch_live(self) -> FetchResult:
        result = FetchResult()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        mime_filter = " or ".join(f"mimeType='{m}'" for m in self.CONTRACT_MIMES)
        query = f"({mime_filter}) and trashed=false"

        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            page_token = self.cursor.get("page_token")
            while True:
                params: dict[str, Any] = {
                    "q": query,
                    "fields": (
                        "nextPageToken, files(id, name, mimeType, size, "
                        "modifiedTime, webViewLink, md5Checksum)"
                    ),
                    "pageSize": 100,
                }
                if page_token:
                    params["pageToken"] = page_token

                response = await client.get(
                    "https://www.googleapis.com/drive/v3/files",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                body = response.json()

                for meta in body.get("files", []):
                    size = int(meta.get("size") or 0)
                    if size > self.MAX_FILE_BYTES:
                        result.errors.append(
                            f"{meta.get('name')}: {size} bytes exceeds the "
                            f"{self.MAX_FILE_BYTES} byte limit; skipped"
                        )
                        continue
                    content = await self._download(client, headers, meta)
                    result.records.append(
                        FetchedRecord(
                            RecordType.CONTRACT,
                            meta["id"],
                            meta,
                            file_bytes=content,
                            file_name=meta.get("name"),
                            mime_type=meta.get("mimeType"),
                        )
                    )

                page_token = body.get("nextPageToken")
                if not page_token:
                    break

        result.cursor = {"synced_at": datetime.now(UTC).isoformat()}
        return result

    async def _download(
        self, client: httpx.AsyncClient, headers: dict[str, str], meta: dict[str, Any]
    ) -> bytes | None:
        """Fetch file bytes, exporting Google-native documents to PDF first."""
        file_id = meta["id"]
        try:
            if meta.get("mimeType") == "application/vnd.google-apps.document":
                response = await client.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                    headers=headers,
                    params={"mimeType": "application/pdf"},
                )
            else:
                response = await client.get(
                    f"https://www.googleapis.com/drive/v3/files/{file_id}",
                    headers=headers,
                    params={"alt": "media"},
                )
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            # Metadata is still worth keeping: the reviewer learns the document
            # exists and that we could not read it.
            emit(
                EventKind.ERROR,
                f"Drive download failed for {meta.get('name')}: {exc}",
                workspace_id=self.workspace_id,
                severity=Severity.WARNING,
                feature=1,
            )
            return None

    def fetch_synthetic(self) -> FetchResult:
        result = FetchResult()
        for index, contract in enumerate(synthetic_contracts.CONTRACTS, start=1):
            pdf = synthetic_contracts.render_pdf(contract)
            meta = {
                "id": f"drive_{index:04d}",
                "name": contract.file_name,
                "mimeType": "application/pdf",
                "size": str(len(pdf)),
                "modifiedTime": f"{contract.start_date.isoformat()}T09:00:00Z",
                "webViewLink": f"https://drive.google.com/file/d/drive_{index:04d}/view",
                "folderPath": "/Contracts/FY2026-27",
            }
            result.records.append(
                FetchedRecord(
                    RecordType.CONTRACT,
                    meta["id"],
                    meta,
                    file_bytes=pdf,
                    file_name=contract.file_name,
                    mime_type="application/pdf",
                )
            )
        result.cursor = {"synthetic": True}
        return result


class HubSpotConnector(Connector):
    """Optional CRM context — a supporting identity signal, never proof."""

    source_system = SourceSystem.HUBSPOT
    display_name = "HubSpot"

    def __init__(
        self,
        workspace_id: str,
        *,
        cursor=None,
        access_token: str | None = None,
        force_synthetic: bool = False,
    ):
        super().__init__(workspace_id, cursor=cursor, force_synthetic=force_synthetic)
        self.access_token = access_token

    def has_credentials(self) -> bool:
        return bool(self.access_token)

    async def fetch_live(self) -> FetchResult:
        result = FetchResult()
        headers = {"Authorization": f"Bearer {self.access_token}"}
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            after = self.cursor.get("after")
            while True:
                params: dict[str, Any] = {
                    "limit": 100,
                    "properties": "name,domain,city,country,lifecyclestage,hubspot_owner_id",
                }
                if after:
                    params["after"] = after
                response = await client.get(
                    "https://api.hubapi.com/crm/v3/objects/companies",
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                body = response.json()
                for company in body.get("results", []):
                    result.records.append(
                        FetchedRecord(RecordType.CRM_ACCOUNT, company["id"], company)
                    )
                after = (body.get("paging") or {}).get("next", {}).get("after")
                if not after:
                    break
        result.cursor = {"synced_at": datetime.now(UTC).isoformat()}
        return result

    def fetch_synthetic(self) -> FetchResult:
        result = FetchResult()
        for company in synthetic_txn.hubspot_companies():
            result.records.append(
                FetchedRecord(RecordType.CRM_ACCOUNT, company["id"], company)
            )
        result.cursor = {"synthetic": True}
        return result


CONNECTOR_REGISTRY: dict[SourceSystem, type[Connector]] = {
    SourceSystem.RAZORPAY: RazorpayConnector,
    SourceSystem.ZOHO_BOOKS: ZohoBooksConnector,
    SourceSystem.GOOGLE_DRIVE: GoogleDriveConnector,
    SourceSystem.HUBSPOT: HubSpotConnector,
}
