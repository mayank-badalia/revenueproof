"""Password hashing.

Uses the `bcrypt` library directly rather than passlib. Passlib 1.7.4 reads
`bcrypt.__about__.__version__`, which was removed in bcrypt 4.1+, so the pair
raises on every hash; passlib has also been effectively unmaintained since 2020.
Build instructions Step 1b asks for current, maintained libraries, and bcrypt's
own API is small enough that the compatibility wrapper bought nothing.
"""

from __future__ import annotations

import bcrypt

# bcrypt truncates silently at 72 bytes. Rejecting longer input is safer than
# letting two different long passwords authenticate the same account.
MAX_PASSWORD_BYTES = 72

# Cost factor. 12 is the current sensible default: ~0.3s per hash on this class of
# hardware, which is slow enough to matter for offline cracking.
ROUNDS = 12


class PasswordError(ValueError):
    pass


def hash_password(plain: str) -> str:
    encoded = plain.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordError(
            f"password exceeds {MAX_PASSWORD_BYTES} bytes; bcrypt would silently truncate it"
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verification that never raises on malformed stored hashes."""
    try:
        encoded = plain.encode("utf-8")
        if len(encoded) > MAX_PASSWORD_BYTES:
            return False
        return bcrypt.checkpw(encoded, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        # A corrupted or legacy hash must read as "wrong password", not a 500.
        return False
