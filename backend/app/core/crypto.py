"""Hashing, HMAC verification and encryption of provider tokens.

Three separate jobs live here because they share a threat model — everything in
this module protects evidence integrity or a secret:

* `sha256_*` produce the provenance hashes that make an edited source file
  detectable (idea_features.md §17).
* `verify_webhook_signature` performs constant-time HMAC comparison over the *raw*
  request body, which is the one detail webhook integrations most often get wrong.
* `TokenCipher` encrypts OAuth tokens at rest so a database dump does not hand over
  live access to a founder's accounting system.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


# --------------------------------------------------------------------------
# Hashing / provenance
# --------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json(value: Any) -> str:
    """Deterministic JSON so the same logical payload always hashes identically.

    Sorted keys and no incidental whitespace: without this, a provider reordering
    its response fields would look like tampered evidence.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def chain_hash(previous_hash: str | None, payload: Any) -> str:
    """Link one audit event to its predecessor, making deletions detectable."""
    return sha256_text(f"{previous_hash or ''}|{canonical_json(payload)}")


def account_fingerprint(account_number: str, workspace_id: str) -> str:
    """Stable pseudonym for a bank account.

    Salted with the workspace so the same account in two workspaces does not
    produce a matching fingerprint — cross-tenant correlation is not a feature.
    """
    normalised = "".join(ch for ch in str(account_number) if ch.isalnum()).upper()
    return sha256_text(f"{workspace_id}:{normalised}")


# --------------------------------------------------------------------------
# Webhook signature verification
# --------------------------------------------------------------------------


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 check against the raw, unparsed request body.

    Must be given the exact bytes received. Re-serialising parsed JSON changes
    whitespace and key order, which silently breaks verification — a bug that
    typically gets "fixed" by disabling the check.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


# --------------------------------------------------------------------------
# Token encryption at rest
# --------------------------------------------------------------------------


class TokenCipher:
    """Fernet encryption for stored OAuth credentials."""

    def __init__(self, key: str | None = None) -> None:
        raw = key or settings.token_encryption_key
        if raw:
            self._fernet = Fernet(self._coerce_key(raw))
        else:
            # Local development without a configured key: derive a deterministic
            # one from the JWT secret so tokens are still not stored in plaintext.
            # Production must set TOKEN_ENCRYPTION_KEY explicitly.
            derived = hashlib.sha256(settings.jwt_secret.encode()).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    @staticmethod
    def _coerce_key(raw: str) -> bytes:
        try:
            candidate = raw.encode() if isinstance(raw, str) else raw
            Fernet(candidate)
            return candidate
        except (ValueError, TypeError):
            # Accept an arbitrary passphrase by hashing it to Fernet's key length.
            return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str | None) -> str | None:
        if ciphertext is None:
            return None
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            # A rotated key must not crash the app; the connection is simply
            # treated as needing re-authorisation.
            return None


_cipher: TokenCipher | None = None


def get_cipher() -> TokenCipher:
    global _cipher
    if _cipher is None:
        _cipher = TokenCipher()
    return _cipher


# --------------------------------------------------------------------------
# Redaction before LLM calls (idea_features.md §17)
# --------------------------------------------------------------------------

_SENSITIVE_KEYS = {
    "access_token", "refresh_token", "password", "secret", "api_key",
    "authorization", "client_secret", "webhook_secret", "hashed_password",
    "account_number", "card", "cvv", "pan",
}


def redact(value: Any, _depth: int = 0) -> Any:
    """Strip secrets from a payload before it is logged or sent to a model.

    Applied at the LLM boundary because contract and payment payloads routinely
    carry tokens and account numbers that a model has no need to see.
    """
    if _depth > 12:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if key.lower() in _SENSITIVE_KEYS else redact(val, _depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    return value
