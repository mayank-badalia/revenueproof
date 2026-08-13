"""Rate limiter tests.

These pin behaviour learned the hard way from real Groq 429s. The free tier allows
8,000-12,000 tokens per minute depending on model, and a naive client turns one
rejection into a sustained cascade.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.rate_limit import (
    RateLimiter,
    _parse_retry_after,
    estimate_tokens,
)


async def test_requests_within_budget_do_not_wait():
    limiter = RateLimiter()
    started = time.perf_counter()
    await limiter.acquire("m", 100)
    assert time.perf_counter() - started < 0.2


async def test_a_provider_block_is_waited_out_not_overridden():
    """The single most important behaviour: never send into an explicit 429 window.

    Sending anyway guarantees another rejection, which extends the block — one
    rejection becomes a cascade.
    """
    limiter = RateLimiter()
    limiter.penalise("m", "2")
    started = time.perf_counter()
    await limiter.acquire("m", 100)
    waited = time.perf_counter() - started
    assert 1.5 < waited < 5.0, f"did not respect the provider block (waited {waited:.1f}s)"


async def test_exceeding_the_local_budget_paces_the_next_request():
    limiter = RateLimiter()
    bucket = limiter._bucket("m")
    bucket.tokens_per_minute = 1000  # budget becomes 850

    await limiter.acquire("m", 800)
    started = time.perf_counter()
    # Second request cannot fit; it must wait rather than sail through.
    await asyncio.wait_for(limiter.acquire("m", 800), timeout=95)
    assert time.perf_counter() - started > 0.05


def test_provider_limit_header_overrides_the_default():
    limiter = RateLimiter()
    limiter.observe_headers("m", {"x-ratelimit-limit-tokens": "12000"})
    assert limiter._bucket("m").tokens_per_minute == 12000


def test_near_empty_remaining_triggers_a_block():
    limiter = RateLimiter()
    limiter.observe_headers(
        "m",
        {
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "50",
            "x-ratelimit-reset-tokens": "4",
        },
    )
    assert limiter.snapshot()["m"]["blocked_for"] > 0


def test_healthy_remaining_does_not_block():
    limiter = RateLimiter()
    limiter.observe_headers(
        "m", {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "7000"}
    )
    assert limiter.snapshot()["m"]["blocked_for"] == 0


@pytest.mark.parametrize(
    ("header", "expected"),
    [("5", 5.0), ("7.5s", 7.5), ("2.5", 2.5), (None, 12.0), ("garbage", 12.0), ("999", 60.0)],
)
def test_retry_after_parsing(header, expected):
    assert _parse_retry_after(header) == expected


def test_limits_are_tracked_per_model():
    """Quotas are per model; one model's exhaustion must not stall another."""
    limiter = RateLimiter()
    limiter.observe_headers("a", {"x-ratelimit-limit-tokens": "8000"})
    limiter.observe_headers("b", {"x-ratelimit-limit-tokens": "12000"})
    snapshot = limiter.snapshot()
    assert snapshot["a"]["tokens_per_minute"] == 8000
    assert snapshot["b"]["tokens_per_minute"] == 12000


def test_token_estimation_is_roughly_proportional():
    assert estimate_tokens("") >= 1
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("x" * 4000) > estimate_tokens("x" * 400)
