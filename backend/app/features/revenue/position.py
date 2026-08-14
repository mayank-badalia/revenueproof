"""The one definition of "evidence-supported revenue".

Two screens quoted this figure and computed it separately. The diligence room
summed `recognized_amount` over *published* items; the downloadable report summed it
over *every* item. On a workspace whose verified items were all withheld pending
review, the room showed INR 0.00 and the report showed INR 4,50,000 — the same
evidence, the same instant, two numbers, and no way for a reader to tell which one
the company was being judged on.

A due-diligence tool that reports two different totals for one question has failed at
the only thing it does. So the definition lives here once and both callers import it.

The rule it encodes: **a figure counts only once a critic has cleared it for
publication.** An item that classified as verified but is still withheld is not
evidence-supported revenue yet — it is a pending question, and it belongs in the
"what a person can clear right now" list rather than in the headline.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.models.enums import RevenueClass


class _Positionable(Protocol):
    """The fields this module needs, so it need not import the ORM model."""

    classification: str
    recognized_amount: int
    is_published: bool


def is_verified(item: _Positionable) -> bool:
    """Did this item classify into a state that may be added into revenue?"""
    return RevenueClass(item.classification).counts_as_verified


def published_verified(items: Iterable[_Positionable]) -> list[_Positionable]:
    """The items that make up the headline figure, in one place."""
    return [item for item in items if item.is_published and is_verified(item)]


def published_verified_total(items: Iterable[_Positionable]) -> int:
    """Evidence-supported revenue, in minor units. The headline number."""
    return sum(item.recognized_amount for item in published_verified(items))


def withheld_verified_total(items: Iterable[_Positionable]) -> int:
    """Classified as verified but not published — the gap between the two screens.

    Reported alongside the headline rather than folded into it, because "we found
    this but cannot stand behind it yet" is a different statement from both "we
    found it" and "it is not there".
    """
    return sum(
        item.recognized_amount
        for item in items
        if is_verified(item) and not item.is_published
    )
