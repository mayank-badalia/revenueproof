"""Access-token minting for the OAuth-style connectors — Feature 1, sub-feature 2.

The connectors take a bearer token and ask no questions about where it came from.
That is the right boundary, but it left a gap: nothing in the app could *produce*
one. A token pasted in by hand dies within the hour, which is fine for a probe and
useless for a product.

Three credential shapes, three flows, one interface:

* **Zoho** holds a long-lived refresh token and exchanges it for an access token.
* **Google** holds a service-account private key and signs a JWT assertion, which
  Google exchanges for an access token. This avoids a browser consent screen
  entirely — the service account reads only what has been explicitly shared with it,
  which is a tighter grant than a user-scoped OAuth token, not a looser one.
* **HubSpot** issues a static service key that needs no exchange at all.

Tokens are cached in-process until shortly before they expire. Re-minting on every
call would work, but it turns one ingestion run into dozens of pointless round trips
to an auth server that rate-limits.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.core.events import EventKind, Severity, emit
from app.models.enums import SourceSystem

# Re-mint this many seconds before the provider's stated expiry, so a token cannot
# expire mid-run between the check and the call that uses it.
_EXPIRY_MARGIN_SECONDS = 120

ZOHO_ACCOUNTS_HOSTS = {
    "in": "https://accounts.zoho.in",
    "com": "https://accounts.zoho.com",
    "eu": "https://accounts.zoho.eu",
    "au": "https://accounts.zoho.com.au",
}

GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class TokenError(RuntimeError):
    """A credential exists but could not be turned into an access token."""


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    @property
    def usable(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at - _EXPIRY_MARGIN_SECONDS


_cache: dict[str, _CachedToken] = {}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Zoho — refresh token grant
# ---------------------------------------------------------------------------


def zoho_accounts_host() -> str:
    return ZOHO_ACCOUNTS_HOSTS.get(settings.zoho_region, ZOHO_ACCOUNTS_HOSTS["in"])


def zoho_configured() -> bool:
    return bool(
        settings.zoho_client_id
        and settings.zoho_client_secret
        and settings.zoho_refresh_token
        and settings.zoho_organization_id
    )


async def zoho_access_token() -> str:
    cached = _cache.get("zoho")
    if cached and cached.usable:
        return cached.value
    if not zoho_configured():
        raise TokenError("Zoho credentials incomplete")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{zoho_accounts_host()}/oauth/v2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": settings.zoho_client_id,
                "client_secret": settings.zoho_client_secret,
                "refresh_token": settings.zoho_refresh_token,
            },
        )
    body: dict[str, Any] = response.json() if response.content else {}
    token = body.get("access_token")
    if not token:
        # Zoho answers 200 with an error body, so status alone is not enough.
        raise TokenError(f"Zoho refresh failed: {json.dumps(body)[:200]}")

    _cache["zoho"] = _CachedToken(token, time.time() + float(body.get("expires_in", 3600)))
    return token


# ---------------------------------------------------------------------------
# Google — service account JWT assertion
# ---------------------------------------------------------------------------


def google_service_account_info() -> dict[str, Any] | None:
    path = settings.google_service_account_file
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    try:
        return json.loads(file.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def google_configured() -> bool:
    info = google_service_account_info()
    return bool(info and info.get("client_email") and info.get("private_key"))


async def google_access_token() -> str:
    cached = _cache.get("google")
    if cached and cached.usable:
        return cached.value

    info = google_service_account_info()
    if not info:
        raise TokenError("Google service-account file missing or unreadable")

    # Imported here so a workspace with no Google credential never pays for the
    # cryptography import at module load.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(
        json.dumps(
            {
                "iss": info["client_email"],
                "scope": GOOGLE_DRIVE_SCOPE,
                "aud": info["token_uri"],
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    assertion = (
        f"{header}.{claims}."
        f"{_b64(key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))}"
    )

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            info["token_uri"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
    body = response.json() if response.content else {}
    token = body.get("access_token")
    if not token:
        raise TokenError(f"Google token exchange failed: {json.dumps(body)[:200]}")

    _cache["google"] = _CachedToken(token, time.time() + float(body.get("expires_in", 3600)))
    return token


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def access_token_for(source: SourceSystem, workspace_id: str = "_system") -> str | None:
    """Mint an access token for a source, or return None when unconfigured.

    Returning None rather than raising is deliberate: an unconfigured source falls
    back to the synthetic dataset, which is a supported state. A *configured* source
    whose token cannot be minted is a different matter and raises, because silently
    serving synthetic data under a real credential would misreport the run.
    """
    try:
        if source is SourceSystem.ZOHO_BOOKS:
            return await zoho_access_token() if zoho_configured() else None
        if source is SourceSystem.GOOGLE_DRIVE:
            return await google_access_token() if google_configured() else None
        if source is SourceSystem.HUBSPOT:
            return settings.hubspot_access_token or None
    except TokenError as exc:
        emit(
            EventKind.ERROR,
            f"{source}: credentials are configured but no access token could be "
            f"obtained — {exc}",
            workspace_id=workspace_id,
            severity=Severity.ERROR,
            feature=1,
        )
        raise
    return None
