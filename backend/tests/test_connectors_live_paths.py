"""Live-API path tests using httpx MockTransport.

These close the part of Step 2a's integration reality-check that does not require
credentials. Real keys prove a call reaches the provider; these prove the code
*around* that call is right — that it sends correct auth and parameters, walks
pagination, extracts the documented fields, and degrades sensibly on 401/429/500.

Response bodies are modelled on the shapes in the ranked documentation cited in
`core_resoruces.md`. What remains untestable without credentials is whether a
provider's real response differs from its published schema.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.connectors.base import ConnectorError
from app.connectors.providers import (
    GoogleDriveConnector,
    HubSpotConnector,
    RazorpayConnector,
    ZohoBooksConnector,
)
from app.core.config import settings
from app.models.enums import RecordType


def mock_client_factory(handler):
    """Patch httpx.AsyncClient so connectors talk to `handler` instead of the network."""
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    return factory


@pytest.fixture
def razorpay_credentials(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "rzp_test_secret")


# ---------------------------------------------------------------------------
# Razorpay
# ---------------------------------------------------------------------------


async def test_razorpay_live_fetch_collects_all_four_record_types(
    monkeypatch, razorpay_credentials
):
    """Payments, refunds, disputes and settlements must all be retrieved.

    A refund-only integration misses chargebacks, and a payments-only integration
    never learns whether money actually reached the bank.
    """
    seen_paths: list[str] = []
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        seen_auth.append(request.headers.get("Authorization", ""))
        bodies = {
            "/v1/payments": {
                "count": 1,
                "items": [
                    {
                        "id": "pay_LiveTest001",
                        "entity": "payment",
                        "amount": 11800000,
                        "currency": "INR",
                        "status": "captured",
                        "invoice_id": "inv_Live001",
                        "method": "netbanking",
                        "amount_refunded": 0,
                        "captured": True,
                        "description": "Annual subscription",
                        "email": "ap@example.com",
                        "contact": "+919999999999",
                        "fee": 236000,
                        "tax": 36000,
                        "created_at": 1775000000,
                    }
                ],
            },
            "/v1/refunds": {
                "count": 1,
                "items": [
                    {
                        "id": "rfnd_LiveTest001",
                        "entity": "refund",
                        "amount": 5000000,
                        "currency": "INR",
                        "payment_id": "pay_LiveTest001",
                        "status": "processed",
                        "notes": {"reason": "partial cancellation"},
                        "created_at": 1775600000,
                    }
                ],
            },
            "/v1/disputes": {
                "count": 1,
                "items": [
                    {
                        "id": "disp_LiveTest001",
                        "entity": "dispute",
                        "payment_id": "pay_LiveTest001",
                        "amount": 11800000,
                        "currency": "INR",
                        "reason_code": "chargeback_fraud",
                        "phase": "chargeback",
                        "status": "open",
                        "created_at": 1776000000,
                    }
                ],
            },
            "/v1/settlements": {
                "count": 1,
                "items": [
                    {
                        "id": "setl_LiveTest001",
                        "entity": "settlement",
                        "amount": 11564000,
                        "status": "processed",
                        "created_at": 1775200000,
                    }
                ],
            },
        }
        return httpx.Response(200, json=bodies.get(request.url.path, {"items": []}))

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await RazorpayConnector("w1").fetch()

    assert result.is_synthetic is False, "credentials present — must use the live path"
    types = {record.record_type for record in result.records}
    assert types == {
        RecordType.PAYMENT,
        RecordType.REFUND,
        RecordType.DISPUTE,
        RecordType.SETTLEMENT,
    }

    # All four documented endpoints were actually called.
    assert {"/v1/payments", "/v1/refunds", "/v1/disputes", "/v1/settlements"} <= set(seen_paths)

    # HTTP Basic auth with key id and secret, as Razorpay documents.
    expected = base64.b64encode(b"rzp_test_key:rzp_test_secret").decode()
    assert all(auth == f"Basic {expected}" for auth in seen_auth)


async def test_razorpay_live_response_normalises_to_correct_amounts(
    monkeypatch, razorpay_credentials
):
    """The live payload must survive normalisation with amounts intact."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/payments":
            return httpx.Response(200, json={"items": [{
                "id": "pay_X", "amount": 11800000, "currency": "INR",
                "status": "captured", "amount_refunded": 2000000,
                "fee": 236000, "tax": 36000, "created_at": 1775000000,
                "email": "a@example.com", "description": "sub",
            }]})
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await RazorpayConnector("w1").fetch()

    from app.connectors import normalize

    payment = normalize.razorpay_payment(result.records[0].payload)
    assert payment.amount_minor == 11800000          # paise passed through, not ×100
    assert payment.status == "partially_refunded"    # derived from amount_refunded
    assert payment.retained_minor == 9800000         # gross less refund
    assert payment.net_amount_minor == 11528000      # gross less fee and tax


async def test_razorpay_advances_its_cursor_for_incremental_sync(
    monkeypatch, razorpay_credentials
):
    """A second run must resume after the newest record, not refetch everything."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/payments":
            return httpx.Response(200, json={"items": [
                {"id": "pay_A", "amount": 100, "currency": "INR",
                 "status": "captured", "created_at": 1775000000},
                {"id": "pay_B", "amount": 200, "currency": "INR",
                 "status": "captured", "created_at": 1775999999},
            ]})
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await RazorpayConnector("w1").fetch()
    assert result.cursor["payments_from"] == 1776000000  # newest + 1


async def test_razorpay_sends_the_cursor_as_a_from_filter(
    monkeypatch, razorpay_credentials
):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/payments":
            captured.update(dict(request.url.params))
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    await RazorpayConnector("w1", cursor={"payments_from": 1775000000}).fetch()
    assert captured.get("from") == "1775000000"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
async def test_razorpay_http_failures_raise_connector_error(
    monkeypatch, razorpay_credentials, status
):
    """An expired key or a rate limit must surface clearly, not look like zero revenue."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/payments":
            return httpx.Response(status, json={"error": {"description": "denied"}})
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    with pytest.raises(ConnectorError, match=str(status)):
        await RazorpayConnector("w1").fetch()


async def test_razorpay_tolerates_missing_dispute_permission(
    monkeypatch, razorpay_credentials
):
    """Disputes need an extra permission; without it the rest must still import."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/disputes":
            return httpx.Response(403, json={"error": "not enabled"})
        if request.url.path == "/v1/payments":
            return httpx.Response(200, json={"items": [
                {"id": "pay_A", "amount": 100, "currency": "INR",
                 "status": "captured", "created_at": 1775000000}
            ]})
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await RazorpayConnector("w1").fetch()
    assert any(r.record_type == RecordType.PAYMENT for r in result.records)


async def test_razorpay_network_failure_raises_connector_error(
    monkeypatch, razorpay_credentials
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    with pytest.raises(ConnectorError, match="unreachable"):
        await RazorpayConnector("w1").fetch()


# ---------------------------------------------------------------------------
# Zoho Books
# ---------------------------------------------------------------------------


@pytest.fixture
def zoho_credentials(monkeypatch):
    monkeypatch.setattr(settings, "zoho_client_id", "zoho_client")
    monkeypatch.setattr(settings, "zoho_organization_id", "60000000")
    monkeypatch.setattr(settings, "zoho_region", "in")


async def test_zoho_live_fetch_paginates_and_sends_org_id(monkeypatch, zoho_credentials):
    """Zoho paginates via page_context.has_more_page; all pages must be collected."""
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        page = int(request.url.params.get("page", 1))
        if request.url.path.endswith("/invoices"):
            if page == 1:
                return httpx.Response(200, json={
                    "invoices": [{"invoice_id": "zi_1", "invoice_number": "INV-1",
                                  "status": "paid", "date": "2026-04-01",
                                  "currency_code": "INR", "total": 118000.0,
                                  "balance": 0.0, "line_items": []}],
                    "page_context": {"has_more_page": True},
                })
            return httpx.Response(200, json={
                "invoices": [{"invoice_id": "zi_2", "invoice_number": "INV-2",
                              "status": "sent", "date": "2026-05-01",
                              "currency_code": "INR", "total": 59000.0,
                              "balance": 59000.0, "line_items": []}],
                "page_context": {"has_more_page": False},
            })
        return httpx.Response(200, json={request.url.path.rsplit("/", 1)[-1]: [],
                                         "page_context": {"has_more_page": False}})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await ZohoBooksConnector("w1", access_token="zoho_token").fetch()

    invoices = [r for r in result.records if r.record_type == RecordType.INVOICE]
    assert len(invoices) == 2, "second page was not collected"

    first = requests_seen[0]
    assert first.headers["Authorization"] == "Zoho-oauthtoken zoho_token"
    assert first.url.params["organization_id"] == "60000000"
    assert "zohoapis.in" in str(first.url), "wrong regional host for region 'in'"


async def test_zoho_region_selects_the_correct_host(monkeypatch, zoho_credentials):
    """A token issued in one data centre is invalid in another."""
    monkeypatch.setattr(settings, "zoho_region", "eu")
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json={"invoices": [],
                                         "page_context": {"has_more_page": False}})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    await ZohoBooksConnector("w1", access_token="t").fetch()
    assert all(host == "www.zohoapis.eu" for host in hosts)


async def test_zoho_one_forbidden_endpoint_does_not_lose_the_others(
    monkeypatch, zoho_credentials
):
    """A scope the user did not grant must not discard the data that did arrive."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/creditnotes"):
            return httpx.Response(403, json={"message": "no permission"})
        if request.url.path.endswith("/invoices"):
            return httpx.Response(200, json={
                "invoices": [{"invoice_id": "zi_1", "status": "paid",
                              "date": "2026-04-01", "currency_code": "INR",
                              "total": 100.0, "balance": 0.0, "line_items": []}],
                "page_context": {"has_more_page": False},
            })
        return httpx.Response(200, json={request.url.path.rsplit("/", 1)[-1]: [],
                                         "page_context": {"has_more_page": False}})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await ZohoBooksConnector("w1", access_token="t").fetch()

    assert any(r.record_type == RecordType.INVOICE for r in result.records)
    assert any("creditnotes" in error for error in result.errors)


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


@pytest.fixture
def google_credentials(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "google_client")


async def test_drive_lists_filters_downloads_and_paginates(monkeypatch, google_credentials):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.path}?{request.url.params.get('alt', '')}")
        if request.url.path == "/drive/v3/files":
            if not request.url.params.get("pageToken"):
                return httpx.Response(200, json={
                    "nextPageToken": "page2",
                    "files": [{"id": "f1", "name": "MSA.pdf",
                               "mimeType": "application/pdf", "size": "2048",
                               "modifiedTime": "2026-04-01T09:00:00Z",
                               "webViewLink": "https://drive.google.com/file/d/f1/view"}],
                })
            return httpx.Response(200, json={"files": [
                {"id": "f2", "name": "Addendum.gdoc",
                 "mimeType": "application/vnd.google-apps.document",
                 "modifiedTime": "2026-05-01T09:00:00Z"}
            ]})
        if request.url.path.endswith("/export"):
            return httpx.Response(200, content=b"%PDF-1.7 exported google doc")
        return httpx.Response(200, content=b"%PDF-1.7 downloaded binary")

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await GoogleDriveConnector("w1", access_token="ya29.token").fetch()

    assert len(result.records) == 2, "pagination did not collect the second page"
    # A binary PDF is downloaded with alt=media; a Google-native doc is exported.
    assert any("alt=media" in call or "media" in call for call in calls)
    assert any("/export" in call for call in calls)
    assert all(record.file_bytes for record in result.records)
    assert result.records[0].file_bytes.startswith(b"%PDF")


async def test_drive_query_restricts_to_contract_mime_types(monkeypatch, google_credentials):
    """Least privilege in practice: do not pull every file in someone's Drive."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files":
            captured.update(dict(request.url.params))
        return httpx.Response(200, json={"files": []})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    await GoogleDriveConnector("w1", access_token="t").fetch()

    query = captured.get("q", "")
    assert "application/pdf" in query
    assert "trashed=false" in query


async def test_drive_skips_oversized_files_and_reports_them(monkeypatch, google_credentials):
    """A 200 MB file must not be pulled into memory during parsing."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {"id": "big", "name": "Huge.pdf", "mimeType": "application/pdf",
                 "size": str(200 * 1024 * 1024)}
            ]})
        return httpx.Response(200, content=b"should never be fetched")

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await GoogleDriveConnector("w1", access_token="t").fetch()

    assert result.records == []
    assert any("exceeds" in error for error in result.errors)


async def test_drive_keeps_metadata_when_a_download_fails(monkeypatch, google_credentials):
    """A reviewer should learn the document exists even if we could not read it."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {"id": "f1", "name": "MSA.pdf", "mimeType": "application/pdf",
                 "size": "2048"}
            ]})
        return httpx.Response(500, text="download failed")

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await GoogleDriveConnector("w1", access_token="t").fetch()

    assert len(result.records) == 1
    assert result.records[0].file_bytes is None
    assert result.records[0].payload["name"] == "MSA.pdf"


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------


async def test_hubspot_paginates_via_after_cursor(monkeypatch):
    monkeypatch.setattr(settings, "hubspot_client_id", "hs_client")
    seen_after: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        seen_after.append(after)
        if after is None:
            return httpx.Response(200, json={
                "results": [{"id": "1", "properties": {"name": "Acme Corp",
                                                       "domain": "acme.com"}}],
                "paging": {"next": {"after": "cursor2"}},
            })
        return httpx.Response(200, json={
            "results": [{"id": "2", "properties": {"name": "Beta Ltd",
                                                   "domain": "beta.io"}}]
        })

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await HubSpotConnector("w1", access_token="pat-token").fetch()

    assert len(result.records) == 2
    assert seen_after == [None, "cursor2"]


# ---------------------------------------------------------------------------
# Synthetic fallback boundary
# ---------------------------------------------------------------------------


async def test_connectors_fall_back_to_synthetic_without_credentials(monkeypatch):
    """No credential must mean synthetic data clearly labelled, never a silent zero."""
    monkeypatch.setattr(settings, "razorpay_key_id", None)
    monkeypatch.setattr(settings, "razorpay_key_secret", None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call may be made without credentials")

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await RazorpayConnector("w1").fetch()

    assert result.is_synthetic is True
    assert len(result.records) > 0


async def test_credentialed_connector_never_uses_synthetic_data(
    monkeypatch, razorpay_credentials
):
    """The inverse guard: real keys must not silently serve fixtures."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(httpx, "AsyncClient", mock_client_factory(handler))
    result = await RazorpayConnector("w1").fetch()

    assert result.is_synthetic is False
    assert result.records == []


def test_webhook_verification_requires_a_configured_secret(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None)
    assert RazorpayConnector.verify_webhook(b"{}", "anything") is False

    monkeypatch.setattr(settings, "razorpay_webhook_secret", "whsec")
    import hashlib
    import hmac

    body = json.dumps({"event": "payment.captured"}).encode()
    signature = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert RazorpayConnector.verify_webhook(body, signature) is True
