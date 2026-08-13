"""End-to-end API tests for the base app (Step 2a categories 1, 2, 4, 6, 8).

These run against the real PostgreSQL instance from docker-compose rather than a
mock, so they double as the integration reality-check: a passing run proves the
schema, row-level security, audit chain and HTTP layer actually work together.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport

from app.main import app


def unique_email(prefix: str = "user") -> str:
    # example.com is reserved for documentation and passes email validation;
    # .test is a special-use TLD that email-validator rejects.
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
async def client():
    """ASGI client with the real lifespan, so services connect exactly as in prod."""
    from app.core.db import dispose_engine

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # Ensure the schema exists without running the full startup banner.
        from app.core.schema_init import create_schema

        await create_schema()
        yield ac
    await dispose_engine()


@pytest.fixture
async def auth_client(client):
    """A client already carrying a bearer token for a fresh user."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email("founder"), "password": "diligence-2026", "full_name": "Founder"},
    )
    assert response.status_code == 201, response.text
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


VALID_WORKSPACE = {
    "company_name": "Northstar Technologies Private Limited",
    "legal_name": "Northstar Technologies Pvt Ltd",
    "reporting_period_start": "2026-04-01",
    "reporting_period_end": "2027-03-31",
    "base_currency": "INR",
    "claimed_revenue": "10000000.00",
    "claimed_arr": "10000000.00",
    "materiality_threshold_pct": 1.0,
    "accounting_method": "accrual",
}


# ---------------------------------------------------------------------------
# 1. Functional correctness
# ---------------------------------------------------------------------------


async def test_health_reports_every_service(client):
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "postgres" in body["services"]
    assert body["services"]["postgres"]["ok"] is True
    # Providers are reported honestly, so the UI can distinguish live from absent.
    assert set(body["providers"]) >= {"groq", "razorpay", "zoho_books", "google_drive"}


async def test_register_and_login_round_trip(client):
    email = unique_email("roundtrip")
    register = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "diligence-2026"}
    )
    assert register.status_code == 201

    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "diligence-2026"}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


async def test_create_workspace_converts_money_exactly(auth_client):
    response = await auth_client.post("/api/v1/workspaces", json=VALID_WORKSPACE)
    assert response.status_code == 201, response.text
    body = response.json()

    # ₹1,00,00,000.00 must be exactly 1_000_000_000 paise — no float drift.
    assert body["claimed_revenue"]["minor"] == 1_000_000_000
    assert body["claimed_revenue"]["decimal"] == "10000000.00"
    # Indian grouping: a rupee figure written "10,000,000" is read as one crore by
    # nobody in this product's audience, and the room already rendered it this way.
    assert body["claimed_revenue"]["display"] == "INR 1,00,00,000.00"
    assert body["base_currency"] == "INR"


async def test_workspace_summary_shape(auth_client):
    created = await auth_client.post("/api/v1/workspaces", json=VALID_WORKSPACE)
    workspace_id = created.json()["id"]

    summary = await auth_client.get(f"/api/v1/workspaces/{workspace_id}/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["evidence_counts"]["invoices"] == 0
    assert body["open_review_items"] == 0
    assert body["latest_run"] is None


# ---------------------------------------------------------------------------
# 2. Edge cases and boundary conditions
# ---------------------------------------------------------------------------


async def test_reporting_period_must_not_be_inverted(auth_client):
    payload = VALID_WORKSPACE | {
        "reporting_period_start": "2027-03-31",
        "reporting_period_end": "2026-04-01",
    }
    response = await auth_client.post("/api/v1/workspaces", json=payload)
    assert response.status_code == 422
    assert "reporting_period_end" in response.text


async def test_single_day_reporting_period_is_allowed(auth_client):
    payload = VALID_WORKSPACE | {
        "reporting_period_start": "2026-04-01",
        "reporting_period_end": "2026-04-01",
    }
    response = await auth_client.post("/api/v1/workspaces", json=payload)
    assert response.status_code == 201


async def test_absurdly_long_period_is_rejected(auth_client):
    payload = VALID_WORKSPACE | {
        "reporting_period_start": "2026-04-01",
        "reporting_period_end": "2206-04-01",
    }
    response = await auth_client.post("/api/v1/workspaces", json=payload)
    assert response.status_code == 422


async def test_zero_claim_is_valid(auth_client):
    """A founder may legitimately claim nothing and ask what the evidence shows."""
    payload = VALID_WORKSPACE | {"claimed_revenue": "0", "claimed_arr": "0"}
    response = await auth_client.post("/api/v1/workspaces", json=payload)
    assert response.status_code == 201
    assert response.json()["claimed_revenue"]["minor"] == 0


async def test_unicode_company_name_survives_round_trip(auth_client):
    name = "Ünïcode Tech 株式会社 ✓ Pvt Ltd"
    response = await auth_client.post(
        "/api/v1/workspaces", json=VALID_WORKSPACE | {"company_name": name}
    )
    assert response.status_code == 201
    assert response.json()["company_name"] == name


async def test_very_large_claim_stays_exact(auth_client):
    """₹10,000 crore must not lose precision anywhere in the stack."""
    payload = VALID_WORKSPACE | {"claimed_revenue": "100000000000.99"}
    response = await auth_client.post("/api/v1/workspaces", json=payload)
    assert response.status_code == 201
    assert response.json()["claimed_revenue"]["minor"] == 10_000_000_000_099


# ---------------------------------------------------------------------------
# 3. Negative / adversarial input
# ---------------------------------------------------------------------------


async def test_negative_claim_is_rejected(auth_client):
    response = await auth_client.post(
        "/api/v1/workspaces", json=VALID_WORKSPACE | {"claimed_revenue": "-500000"}
    )
    assert response.status_code == 422


async def test_invalid_currency_is_rejected(auth_client):
    response = await auth_client.post(
        "/api/v1/workspaces", json=VALID_WORKSPACE | {"base_currency": "RUPEE"}
    )
    assert response.status_code == 422


async def test_unknown_fields_are_rejected(auth_client):
    """extra='forbid' stops a typo silently becoming a default."""
    response = await auth_client.post(
        "/api/v1/workspaces", json=VALID_WORKSPACE | {"claimed_revenu": "999"}
    )
    assert response.status_code == 422


async def test_sql_injection_in_company_name_is_stored_as_data(auth_client):
    hostile = "Northstar'; DROP TABLE invoices;--"
    response = await auth_client.post(
        "/api/v1/workspaces", json=VALID_WORKSPACE | {"company_name": hostile}
    )
    assert response.status_code == 201
    assert response.json()["company_name"] == hostile

    # The table it tried to drop must still be queryable.
    summary = await auth_client.get(
        f"/api/v1/workspaces/{response.json()['id']}/summary"
    )
    assert summary.status_code == 200
    assert summary.json()["evidence_counts"]["invoices"] == 0


async def test_malformed_amount_is_rejected(auth_client):
    for bad in ["abc", "1.2.3", "NaN", ""]:
        response = await auth_client.post(
            "/api/v1/workspaces", json=VALID_WORKSPACE | {"claimed_revenue": bad}
        )
        assert response.status_code == 422, f"{bad!r} should be rejected"


# ---------------------------------------------------------------------------
# 4. Authorization — object-level, not just token validity (OWASP API1)
# ---------------------------------------------------------------------------


async def test_unauthenticated_requests_are_refused(client):
    client.headers.pop("Authorization", None)
    response = await client.get("/api/v1/workspaces")
    assert response.status_code == 401


async def test_invalid_token_is_refused(client):
    response = await client.get(
        "/api/v1/workspaces", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


async def test_other_users_workspace_is_not_reachable(client):
    """The core tenant-isolation test: a valid token must not open another tenant."""
    first = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email("owner"), "password": "diligence-2026"},
    )
    owner_token = first.json()["access_token"]
    created = await client.post(
        "/api/v1/workspaces",
        json=VALID_WORKSPACE,
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    workspace_id = created.json()["id"]

    second = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email("outsider"), "password": "diligence-2026"},
    )
    outsider_token = second.json()["access_token"]

    for path in ["", "/summary", "/audit", "/events"]:
        response = await client.get(
            f"/api/v1/workspaces/{workspace_id}{path}",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        # 404 rather than 403 — existence itself is not disclosed.
        assert response.status_code == 404, f"path {path!r} leaked to a non-member"


async def test_outsider_cannot_list_another_tenants_workspaces(client):
    owner = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email("owner2"), "password": "diligence-2026"},
    )
    await client.post(
        "/api/v1/workspaces",
        json=VALID_WORKSPACE,
        headers={"Authorization": f"Bearer {owner.json()['access_token']}"},
    )
    outsider = await client.post(
        "/api/v1/auth/register",
        json={"email": unique_email("outsider2"), "password": "diligence-2026"},
    )
    listing = await client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {outsider.json()['access_token']}"},
    )
    assert listing.status_code == 200
    assert listing.json() == []


async def test_duplicate_registration_is_rejected(client):
    email = unique_email("dupe")
    first = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "diligence-2026"}
    )
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": "different-pass"}
    )
    assert second.status_code == 409


async def test_wrong_password_is_refused(client):
    email = unique_email("pw")
    await client.post("/api/v1/auth/register", json={"email": email, "password": "diligence-2026"})
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 5. State, persistence and the audit chain
# ---------------------------------------------------------------------------


async def test_workspace_persists_and_audit_chain_verifies(auth_client):
    created = await auth_client.post("/api/v1/workspaces", json=VALID_WORKSPACE)
    workspace_id = created.json()["id"]

    # Re-read through a separate request: the value came from PostgreSQL, not memory.
    fetched = await auth_client.get(f"/api/v1/workspaces/{workspace_id}")
    assert fetched.json()["claimed_revenue"]["minor"] == 1_000_000_000

    audit = await auth_client.get(f"/api/v1/workspaces/{workspace_id}/audit")
    assert audit.status_code == 200
    body = audit.json()
    assert body["integrity"]["valid"] is True
    assert any(e["action"] == "workspace.created" for e in body["events"])


async def test_update_is_audited_with_before_and_after(auth_client):
    created = await auth_client.post("/api/v1/workspaces", json=VALID_WORKSPACE)
    workspace_id = created.json()["id"]

    updated = await auth_client.patch(
        f"/api/v1/workspaces/{workspace_id}", json={"claimed_revenue": "7200000.00"}
    )
    assert updated.status_code == 200
    assert updated.json()["claimed_revenue"]["minor"] == 720_000_000

    audit = await auth_client.get(f"/api/v1/workspaces/{workspace_id}/audit")
    body = audit.json()
    assert body["integrity"]["valid"] is True
    update_event = next(e for e in body["events"] if e["action"] == "workspace.updated")
    assert update_event["before_state"]["claimed_revenue"] == 1_000_000_000
    assert update_event["after_state"]["claimed_revenue"] == 720_000_000


async def test_audit_chain_sequences_are_contiguous(auth_client):
    created = await auth_client.post("/api/v1/workspaces", json=VALID_WORKSPACE)
    workspace_id = created.json()["id"]
    for amount in ["100.00", "200.00", "300.00"]:
        await auth_client.patch(
            f"/api/v1/workspaces/{workspace_id}", json={"claimed_revenue": amount}
        )

    audit = await auth_client.get(f"/api/v1/workspaces/{workspace_id}/audit")
    body = audit.json()
    assert body["integrity"]["valid"] is True
    sequences = sorted(e["sequence"] for e in body["events"])
    assert sequences == list(range(1, len(sequences) + 1))


# ---------------------------------------------------------------------------
# 6. Concurrency (lightweight) — Step 2a category 7
# ---------------------------------------------------------------------------


async def test_concurrent_workspace_creation_does_not_corrupt_audit(auth_client):
    """Several workspaces created at once must each get their own valid chain."""
    import asyncio

    responses = await asyncio.gather(
        *[
            auth_client.post(
                "/api/v1/workspaces",
                json=VALID_WORKSPACE | {"company_name": f"Concurrent Co {i}"},
            )
            for i in range(5)
        ]
    )
    assert all(r.status_code == 201 for r in responses)

    for response in responses:
        audit = await auth_client.get(
            f"/api/v1/workspaces/{response.json()['id']}/audit"
        )
        assert audit.json()["integrity"]["valid"] is True


# ---------------------------------------------------------------------------
# 7. Error handling — Step 2a category 3
# ---------------------------------------------------------------------------


async def test_nonexistent_workspace_returns_404(auth_client):
    response = await auth_client.get(f"/api/v1/workspaces/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_malformed_uuid_returns_422_not_500(auth_client):
    response = await auth_client.get("/api/v1/workspaces/not-a-uuid")
    assert response.status_code == 422
