"""Redis: idempotency keys, job state and transient run status.

Boundary that matters (PROJECT_WORKFLOW.md): Redis holds *hints and locks*, never
financial truth. If Redis is flushed, no revenue figure changes — the system
re-derives everything from PostgreSQL. What is lost is only deduplication history
and in-flight progress.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.events import EventKind, Severity, emit

_client: aioredis.Redis | None = None

# Long enough that a provider retrying a webhook for hours still deduplicates,
# short enough that keys do not accumulate indefinitely.
IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 3600


def get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_keepalive=True,
        )
    return _client


async def claim_idempotency_key(key: str, ttl: int = IDEMPOTENCY_TTL_SECONDS) -> bool:
    """Atomically claim a key. Returns True on first sight, False for a replay.

    `SET NX` is atomic, so two workers processing a duplicated webhook delivery
    simultaneously cannot both conclude they are first.
    """
    client = get_client()
    acquired = await client.set(f"idem:{key}", "1", nx=True, ex=ttl)
    return bool(acquired)


async def release_idempotency_key(key: str) -> None:
    """Release a claim so a genuinely failed attempt can be retried."""
    await get_client().delete(f"idem:{key}")


async def set_json(key: str, value: Any, ttl: int | None = None) -> None:
    await get_client().set(key, json.dumps(value, default=str), ex=ttl)


async def get_json(key: str) -> Any | None:
    raw = await get_client().get(key)
    return json.loads(raw) if raw else None


async def delete_prefix(prefix: str) -> int:
    """Remove every key under a prefix, using SCAN rather than KEYS."""
    client = get_client()
    removed = 0
    async for key in client.scan_iter(match=f"{prefix}*", count=200):
        await client.delete(key)
        removed += 1
    return removed


class DistributedLock:
    """Simple lock guarding a workspace's reconciliation from concurrent runs.

    Two verification runs allocating the same payment at once is the concrete race
    Step 2a category 7 asks about; serialising per workspace removes it.
    """

    def __init__(self, name: str, ttl: int = 300) -> None:
        self.key = f"lock:{name}"
        self.ttl = ttl
        self._held = False

    async def __aenter__(self) -> DistributedLock:
        acquired = await get_client().set(self.key, "1", nx=True, ex=self.ttl)
        self._held = bool(acquired)
        if not self._held:
            raise RuntimeError(f"could not acquire lock {self.key}; another run is active")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._held:
            await get_client().delete(self.key)


async def healthcheck() -> dict[str, Any]:
    try:
        pong = await get_client().ping()
        return {"ok": bool(pong), "url": settings.redis_url}
    except Exception as exc:
        emit(
            EventKind.ERROR,
            f"Redis health check failed: {exc}",
            severity=Severity.ERROR,
        )
        return {"ok": False, "error": str(exc)[:200]}


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
