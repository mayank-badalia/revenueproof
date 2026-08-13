"""Shared pytest fixtures.

pytest-asyncio runs each test in a fresh event loop, but the database engine and
Redis client are module-level singletons that cache connections bound to whichever
loop created them. Reusing one across tests raises "Event loop is closed".

Production keeps a single long-lived loop, so the singletons are correct there; the
fix belongs in the test harness rather than in application code.
"""

from __future__ import annotations

import pytest

#: Provider credentials are withheld from the whole test session.
#
# Once real sandbox credentials landed in `.env`, every end-to-end test began
# ingesting from the live Zoho and HubSpot accounts instead of the §15 dataset —
# and the ground-truth assertions (₹5,31,000 outstanding, ₹16,52,000 refunded)
# started failing because they were being checked against whatever happened to be
# in an external account that afternoon. A test whose result depends on the state
# of someone else's server is not a regression test.
#
# The live paths keep their own coverage through httpx MockTransport, and the real
# integration is exercised by `scripts/seed_providers.py` plus a live ingestion run,
# neither of which belongs in `pytest -q`.
_PROVIDER_CREDENTIAL_FIELDS = (
    "razorpay_key_id",
    "razorpay_key_secret",
    "razorpay_webhook_secret",
    "zoho_client_id",
    "zoho_client_secret",
    "zoho_refresh_token",
    "zoho_organization_id",
    "google_client_id",
    "google_client_secret",
    "google_service_account_file",
    "hubspot_client_id",
    "hubspot_client_secret",
    "hubspot_access_token",
)


@pytest.fixture(autouse=True, scope="session")
def isolate_from_live_providers():
    """Force every connector into synthetic mode for the duration of the suite."""
    from app.core.config import settings

    saved = {name: getattr(settings, name) for name in _PROVIDER_CREDENTIAL_FIELDS}
    for name in _PROVIDER_CREDENTIAL_FIELDS:
        setattr(settings, name, None)
    yield
    for name, value in saved.items():
        setattr(settings, name, value)


@pytest.fixture(autouse=True)
async def reset_connection_singletons():
    """Dispose pooled connections after every test so the next loop starts clean."""
    yield
    from app.core import cache, graph_db
    from app.core.db import dispose_engine

    await dispose_engine()
    await cache.close_client()
    await graph_db.close_driver()
