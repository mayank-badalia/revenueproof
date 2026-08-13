"""Token-budget rate limiting for LLM calls.

Groq's free tier caps `openai/gpt-oss-120b` at **8,000 tokens per minute**. Feature 2
issues one critic call per material identity link; a run with ~126 of them at ~700
tokens each needs ~88,000 tokens and produced 35 rate-limit rejections in testing.

Three mechanisms work together here:

* **Proactive pacing.** A sliding-window token bucket waits *before* sending when the
  next request would exceed the budget. Being told "no" by the provider and retrying
  wastes a round trip and, on a busy run, cascades.
* **Provider truth over local estimates.** Every response carries
  `x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens`; those are believed
  over the local count, which can only ever approximate prompt tokenisation.
* **Honouring `retry-after`.** When a 429 does arrive, the provider says exactly how
  long to wait. Guessing a backoff instead is how a client turns one rejection into
  a sustained overload.

The limiter is per model, because quotas are per model.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from collections import deque
from dataclasses import dataclass, field

from app.core.events import EventKind, Severity, emit

# Conservative default matching Groq's free tier for the larger models. Overridden
# by the provider's own headers as soon as the first response arrives.
DEFAULT_TOKENS_PER_MINUTE = 8_000
#: Cerebras' free tier allows 60,000 input tokens/minute; its binding constraint is
#: requests per minute, which `RequestPacer` handles separately.
CEREBRAS_TOKENS_PER_MINUTE = 60_000
#: Models served by Cerebras in this deployment. Matched by id, because the limiter
#: only ever sees a model name.
_CEREBRAS_MODELS = frozenset({"gpt-oss-120b", "gemma-4-31b", "zai-glm-4.7"})


def _default_budget_for(model: str) -> int:
    """The per-minute token budget this model's provider actually grants."""
    return (
        CEREBRAS_TOKENS_PER_MINUTE
        if model in _CEREBRAS_MODELS
        else DEFAULT_TOKENS_PER_MINUTE
    )
WINDOW_SECONDS = 60.0
# Leave headroom: token counts are estimated before the call, and a slight
# under-estimate should not push the request over the line.
SAFETY_MARGIN = 0.85
# A sliding one-minute window inherently needs to wait up to a full minute for
# capacity to free up, so the cap must exceed WINDOW_SECONDS or the limiter gives up
# exactly when waiting would have worked.
MAX_WAIT_SECONDS = 90.0
# Below this many tokens left, wait for the provider reset rather than risking a 429.
_NEAR_EMPTY_TOKENS = 900


@dataclass
class _Bucket:
    tokens_per_minute: int = DEFAULT_TOKENS_PER_MINUTE
    # (timestamp, tokens) pairs inside the sliding window.
    spent: deque[tuple[float, int]] = field(default_factory=deque)
    # Set from a 429's retry-after; no request goes out before this time.
    blocked_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _prune(self, now: float) -> None:
        while self.spent and now - self.spent[0][0] > WINDOW_SECONDS:
            self.spent.popleft()

    def used(self, now: float) -> int:
        self._prune(now)
        return sum(tokens for _, tokens in self.spent)

    @property
    def budget(self) -> int:
        return int(self.tokens_per_minute * SAFETY_MARGIN)


class RateLimiter:
    """Per-model sliding-window token limiter."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def _bucket(self, model: str) -> _Bucket:
        if model not in self._buckets:
            # The default here was sized against Groq's free tier. Applying an
            # 8,000 token/minute budget to a provider that allows 60,000 throttles
            # a run to a seventh of its speed for no reason — twenty calls took
            # 179 seconds and five still failed, all of it self-inflicted. Each
            # provider's own budget is used, and a real `x-ratelimit` header still
            # overrides whatever is assumed here.
            self._buckets[model] = _Bucket(
                tokens_per_minute=_default_budget_for(model)
            )
        return self._buckets[model]

    async def acquire(
        self, model: str, estimated_tokens: int, *, workspace_id: str = "_system"
    ) -> float:
        """Wait until `estimated_tokens` can be spent. Returns seconds waited."""
        bucket = self._bucket(model)
        waited = 0.0

        async with bucket.lock:
            while True:
                now = time.monotonic()

                # A 429 told us explicitly to hold off. This is not a local
                # estimate to be overridden — it is the provider's instruction, and
                # sending anyway guarantees another rejection.
                if now < bucket.blocked_until:
                    await asyncio.sleep(min(bucket.blocked_until - now + 0.2, 30.0))
                    waited += min(bucket.blocked_until - now + 0.2, 30.0)
                    continue
                if bucket.used(now) + estimated_tokens <= bucket.budget:
                    bucket.spent.append((now, estimated_tokens))
                    return waited
                else:
                    # Wait just long enough for the oldest spend to leave the window.
                    oldest = bucket.spent[0][0] if bucket.spent else now
                    delay = max(0.1, min(WINDOW_SECONDS - (now - oldest) + 0.2,
                                         MAX_WAIT_SECONDS))

                if waited + delay > MAX_WAIT_SECONDS:
                    # Local-window pressure only — the provider has not told us to
                    # stop. Proceed and let the 429 path handle it if the estimate
                    # was wrong. (A provider block is handled above and never
                    # reaches here, because sending into one is a guaranteed
                    # rejection that then blocks us for even longer.)
                    bucket.spent.append((time.monotonic(), estimated_tokens))
                    emit(
                        EventKind.SYSTEM,
                        f"Rate limiter: local budget exhausted for {model} after "
                        f"{waited:.0f}s; sending anyway",
                        workspace_id=workspace_id,
                        severity=Severity.DEBUG,
                    )
                    return waited

                emit(
                    EventKind.SYSTEM,
                    f"Rate limit pacing: waiting {delay:.1f}s for {model} "
                    f"({bucket.used(time.monotonic())}/{bucket.budget} tokens used)",
                    workspace_id=workspace_id,
                    severity=Severity.DEBUG,
                )
                await asyncio.sleep(delay)
                waited += delay

    def observe_headers(self, model: str, headers: dict[str, str]) -> None:
        """Believe the provider's accounting over the local estimate."""
        bucket = self._bucket(model)

        limit = headers.get("x-ratelimit-limit-tokens")
        if limit:
            try:
                bucket.tokens_per_minute = int(limit)
            except ValueError:
                pass

        # `remaining` is deliberately NOT used to rewrite the local window. Doing so
        # re-stamps already-elapsed provider usage as if it were spent right now,
        # so it decays over a further full minute — the local window and the
        # provider's then double-count the same tokens and the client throttles
        # itself far below the real limit.
        #
        # It is used only for the case it answers unambiguously: the budget is
        # genuinely almost gone, so wait for the provider's own reset.
        remaining = headers.get("x-ratelimit-remaining-tokens")
        reset = headers.get("x-ratelimit-reset-tokens")
        if remaining:
            try:
                if int(remaining) < _NEAR_EMPTY_TOKENS:
                    bucket.blocked_until = time.monotonic() + _parse_retry_after(
                        reset, default=5.0
                    )
            except ValueError:
                pass

    def penalise(self, model: str, retry_after: str | None) -> float:
        """Record a 429. Returns the number of seconds to wait before retrying."""
        bucket = self._bucket(model)
        delay = _parse_retry_after(retry_after)
        bucket.blocked_until = time.monotonic() + delay
        return delay

    def snapshot(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        return {
            model: {
                "tokens_per_minute": bucket.tokens_per_minute,
                "used_in_window": bucket.used(now),
                "budget": bucket.budget,
                "blocked_for": max(0.0, bucket.blocked_until - now),
            }
            for model, bucket in self._buckets.items()
        }


def _parse_retry_after(value: str | None, default: float = 12.0) -> float:
    """Parse a `retry-after` header, which may be seconds or a duration like '7.5s'."""
    if not value:
        return default
    text = str(value).strip().lower().removesuffix("s")
    try:
        return max(0.5, min(float(text), 60.0))
    except ValueError:
        return default


def estimate_tokens(text: str) -> int:
    """Rough prompt-token estimate.

    ~4 characters per token is close enough for pacing; the provider's headers
    correct any drift on the very next response. A tokeniser dependency would buy
    accuracy that the sliding window does not need.
    """
    return max(1, len(text) // 4)


limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Request pacing and bounded concurrency
#
# The token budget above was written against Groq, whose free tier binds on tokens
# per minute. Cerebras binds on *requests* per minute instead — twelve simultaneous
# calls returned ten 429s with `retry-after: 60` while the token budget was barely
# touched. Pacing on the wrong axis is the same as not pacing at all, so both are
# tracked, and concurrency is capped so a burst cannot form in the first place.
# ---------------------------------------------------------------------------

import asyncio as _asyncio
from collections import deque as _deque


class RequestPacer:
    """A sliding window over request *count*, plus a concurrency ceiling.

    Being told "no" and retrying costs a whole round trip and, on a busy run, turns
    one rejection into a cascade — so the wait happens here, before sending.
    """

    def __init__(self, *, per_minute: int, max_concurrent: int) -> None:
        self._per_minute = max(1, per_minute)
        self._times: _deque[float] = _deque()
        self._lock = _asyncio.Lock()
        self._slots = _asyncio.Semaphore(max(1, max_concurrent))

    def configure(self, *, per_minute: int, max_concurrent: int) -> None:
        self._per_minute = max(1, per_minute)
        self._slots = _asyncio.Semaphore(max(1, max_concurrent))

    def observe(self, headers: dict[str, str]) -> None:
        """Learn the real request budget from the provider's own headers.

        Guessing this is how a run ends up throttled or rejected: the published
        figure for this tier was 30 requests/minute and the account actually
        allows 5. A header is the provider stating its own limit, so it wins over
        anything configured here, and the ceiling is adopted immediately rather
        than after the first rejection.
        """
        raw = headers.get("x-ratelimit-limit-requests-minute")
        if not raw:
            return
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            return
        if limit > 0 and limit != self._per_minute:
            self._per_minute = limit
            # Never hold more requests in flight than a minute's entire allowance.
            self._slots = _asyncio.Semaphore(max(1, min(limit, self._slots._value or limit)))

    async def acquire(self) -> None:
        await self._slots.acquire()
        try:
            while True:
                async with self._lock:
                    now = time.monotonic()
                    while self._times and now - self._times[0] >= 60.0:
                        self._times.popleft()
                    if len(self._times) < self._per_minute:
                        self._times.append(now)
                        return
                    wait = 60.0 - (now - self._times[0]) + 0.05
                await asyncio.sleep(min(wait, 60.0))
        except BaseException:
            self._slots.release()
            raise

    def release(self) -> None:
        self._slots.release()

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        recent = sum(1 for t in self._times if now - t < 60.0)
        return {"requests_last_minute": recent, "per_minute": self._per_minute}


pacer = RequestPacer(per_minute=28, max_concurrent=6)
