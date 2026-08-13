"""Exact money arithmetic in integer minor units.

Design rule from idea_features.md §14 and core_resoruces.md: floating point is
rejected for contract values, allocations and totals. Every amount that crosses a
module boundary is an integer count of minor units (paise for INR, cents for USD)
plus an ISO-4217 currency code. `Decimal` is used only at the boundary where a
human-entered or provider-supplied decimal string is converted in, and where a
proration is computed with an explicit rounding mode.

Why integers rather than Decimal end to end: the OR-Tools allocation solver in
Feature 4 requires integer variables, and conservation invariants ("allocated +
unapplied == invoiced") are only exactly checkable in integers. Carrying Decimal
would force a conversion at the least forgiving point in the system.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Self

# ISO-4217 currencies whose minor unit is the major unit (no subdivision).
ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "XAF", "XOF", "PYG", "RWF"})
# Currencies with three decimal places.
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "OMR", "TND", "LYD"})

# Banker's rounding avoids the systematic upward bias of ROUND_HALF_UP when
# thousands of prorations are summed into a headline ARR figure.
DEFAULT_ROUNDING = ROUND_HALF_EVEN

# Upper bound on any single amount, in major units. Chosen far above any plausible
# revenue claim (10^15 ≈ ₹100 lakh crore) but low enough that a malformed or
# hostile value is rejected as data rather than propagating into the solver.
MAX_MAJOR_UNITS = Decimal(10) ** 15


class MoneyError(ValueError):
    """Raised for malformed amounts, unknown currencies or currency mismatches."""


def minor_unit_exponent(currency: str) -> int:
    """Number of decimal places for a currency."""
    code = _normalise_currency(currency)
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def _normalise_currency(currency: str) -> str:
    if not isinstance(currency, str):
        raise MoneyError(f"currency must be a string, got {type(currency).__name__}")
    code = currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise MoneyError(f"invalid ISO-4217 currency code: {currency!r}")
    return code


def to_minor_units(amount: str | int | float | Decimal, currency: str) -> int:
    """Convert a human/provider amount into integer minor units.

    Floats are accepted because provider JSON delivers them, but they are routed
    through `str()` so that 0.1 + 0.2 style artefacts are not silently magnified.
    """
    code = _normalise_currency(currency)
    try:
        if isinstance(amount, float):
            value = Decimal(repr(amount))
        else:
            value = Decimal(str(amount).strip().replace(",", ""))
    except (InvalidOperation, AttributeError, TypeError) as exc:
        raise MoneyError(f"cannot parse amount {amount!r} as a decimal") from exc

    if not value.is_finite():
        raise MoneyError(f"amount is not finite: {amount!r}")

    # Reject absurd magnitudes explicitly. Without this bound, a value like 1e999
    # is a perfectly valid Decimal that then raises a raw InvalidOperation inside
    # quantize(), crashing an ingestion worker instead of quarantining one bad row.
    if abs(value) > MAX_MAJOR_UNITS:
        raise MoneyError(
            f"amount {amount!r} exceeds the maximum supported magnitude "
            f"({MAX_MAJOR_UNITS:.0e} major units)"
        )

    exponent = minor_unit_exponent(code)
    quantum = Decimal(1).scaleb(-exponent)
    try:
        quantised = value.quantize(quantum, rounding=DEFAULT_ROUNDING)
    except InvalidOperation as exc:  # defence in depth against exotic Decimals
        raise MoneyError(f"cannot represent amount {amount!r} in {code}") from exc
    return int(quantised.scaleb(exponent))


def from_minor_units(minor: int, currency: str) -> Decimal:
    """Convert integer minor units back to a Decimal in major units."""
    if not isinstance(minor, int) or isinstance(minor, bool):
        raise MoneyError(f"minor units must be an int, got {type(minor).__name__}")
    exponent = minor_unit_exponent(currency)
    return Decimal(minor).scaleb(-exponent)


#: Currencies grouped in the Indian system (3, then 2s): 10000000 -> 1,00,00,000.
#: A rupee figure written 10,000,000 is read as "ten million" by nobody in the
#: audience for this product; it is one crore, and the grouping is how that is
#: conveyed. The frontend already rendered the room this way, so the backend
#: rendering it the other way meant one workspace showed a figure two ways on two
#: screens — the exact confusion a due-diligence tool cannot afford.
_INDIAN_GROUPED = frozenset({"INR"})


def _group_indian(whole: str) -> str:
    """Group the integer part as 3 digits then pairs: '10000000' -> '1,00,00,000'."""
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def format_money(minor: int, currency: str) -> str:
    """Render for display, e.g. `1234567` INR -> `'12,345.67'`."""
    code = _normalise_currency(currency)
    exponent = minor_unit_exponent(code)
    value = from_minor_units(minor, code)
    if code not in _INDIAN_GROUPED:
        return f"{value:,.{exponent}f}"

    # Format without separators first, then group the integer part. Doing it this
    # way keeps the decimal handling identical to every other currency, so only the
    # grouping differs and rounding cannot drift between the two paths.
    plain = f"{value:.{exponent}f}"
    negative = plain.startswith("-")
    plain = plain.removeprefix("-")
    whole, _, fraction = plain.partition(".")
    grouped = _group_indian(whole)
    rendered = f"{grouped}.{fraction}" if fraction else grouped
    return f"-{rendered}" if negative else rendered


@dataclass(frozen=True, slots=True)
class Money:
    """An exact monetary amount. Immutable; arithmetic requires matching currency."""

    minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise MoneyError(f"Money.minor must be an int, got {type(self.minor).__name__}")
        object.__setattr__(self, "currency", _normalise_currency(self.currency))

    @classmethod
    def parse(cls, amount: str | int | float | Decimal, currency: str) -> Self:
        return cls(to_minor_units(amount, currency), currency)

    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls(0, currency)

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            # Silent cross-currency arithmetic is how "verified revenue" becomes
            # a meaningless number; convert explicitly via FxRate instead.
            raise MoneyError(
                f"currency mismatch: {self.currency} vs {other.currency}; "
                "convert explicitly before combining"
            )

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.minor <= other.minor

    @property
    def decimal(self) -> Decimal:
        return from_minor_units(self.minor, self.currency)

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    def multiply(self, factor: Decimal | int | str, rounding: str = DEFAULT_ROUNDING) -> Money:
        """Scale by a ratio, rounding once at the end."""
        result = (Decimal(self.minor) * Decimal(str(factor))).quantize(Decimal(1), rounding=rounding)
        return Money(int(result), self.currency)

    def __str__(self) -> str:
        return f"{self.currency} {format_money(self.minor, self.currency)}"


def sum_money(amounts: list[Money], currency: str) -> Money:
    """Total a list, verifying every entry shares the expected currency."""
    code = _normalise_currency(currency)
    total = 0
    for item in amounts:
        if item.currency != code:
            raise MoneyError(f"cannot sum {item.currency} into a {code} total")
        total += item.minor
    return Money(total, code)


def allocate_proportionally(total: Money, weights: list[int]) -> list[Money]:
    """Split an amount by integer weights with no minor units lost or created.

    Largest-remainder method: floor each share, then distribute the leftover minor
    units to the entries with the biggest fractional remainders. Guarantees
    `sum(result) == total` exactly, which the reconciliation conservation invariant
    in Feature 4 depends on.
    """
    if not weights:
        return []
    if any(w < 0 for w in weights):
        raise MoneyError("allocation weights must be non-negative")

    weight_total = sum(weights)
    if weight_total == 0:
        # Degenerate input: put everything on the first slot rather than dropping it.
        return [Money(total.minor, total.currency)] + [
            Money(0, total.currency) for _ in weights[1:]
        ]

    shares: list[int] = []
    remainders: list[tuple[Decimal, int]] = []
    for index, weight in enumerate(weights):
        exact = Decimal(total.minor) * Decimal(weight) / Decimal(weight_total)
        floored = int(exact.to_integral_value(rounding="ROUND_FLOOR"))
        shares.append(floored)
        remainders.append((exact - floored, index))

    leftover = total.minor - sum(shares)
    # Ties broken by index so the split is deterministic and reproducible.
    remainders.sort(key=lambda pair: (-pair[0], pair[1]))
    step = 1 if leftover >= 0 else -1
    for offset in range(abs(leftover)):
        shares[remainders[offset % len(remainders)][1]] += step

    return [Money(share, total.currency) for share in shares]


# --------------------------------------------------------------------------
# Period allocation
# --------------------------------------------------------------------------


def days_inclusive(start: date, end: date) -> int:
    """Day count treating both endpoints as inside the range.

    A contract running 1 Apr to 30 Apr covers 30 days, not 29. Getting this wrong
    silently misstates every prorated ARR figure, so it is defined once here.
    """
    if end < start:
        return 0
    return (end - start).days + 1


def overlap_days(
    start: date, end: date, period_start: date, period_end: date
) -> int:
    """Inclusive day overlap between a contract term and a reporting period."""
    if end < start:
        raise MoneyError(f"contract end {end} precedes start {start}")
    if period_end < period_start:
        raise MoneyError(f"period end {period_end} precedes start {period_start}")
    left = max(start, period_start)
    right = min(end, period_end)
    return days_inclusive(left, right)


def prorate_for_period(
    amount: Money,
    term_start: date,
    term_end: date,
    period_start: date,
    period_end: date,
) -> Money:
    """Portion of a term amount falling inside a reporting period, by day count.

    Deliberately day-based rather than month-based: month arithmetic needs a
    convention for partial and unequal months that reviewers would have to trust
    blindly, whereas a day ratio is inspectable and matches the citation the UI
    shows. `idea_features.md` requires annualisation rules to be displayed, so the
    caller stores both the ratio and this result.
    """
    term_days = days_inclusive(term_start, term_end)
    if term_days == 0:
        return Money.zero(amount.currency)
    covered = overlap_days(term_start, term_end, period_start, period_end)
    if covered == 0:
        # Spec §14: a contract outside the reporting period supports no revenue
        # in that period. This is the rule that stops future contracts inflating ARR.
        return Money.zero(amount.currency)
    if covered >= term_days:
        return amount
    return amount.multiply(Decimal(covered) / Decimal(term_days))


# --------------------------------------------------------------------------
# Foreign exchange
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FxRate:
    """A conversion rate pinned to its source and date.

    idea_features.md §14 requires currency conversion to store the exact rate and
    date used. An FX result that cannot name its rate source is not auditable, so
    the rate travels with the converted amount rather than being applied anonymously.
    """

    base_currency: str
    quote_currency: str
    rate: Decimal
    rate_date: date
    source: str  # e.g. "ecb", "manual_override", "provider_reported"

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_currency", _normalise_currency(self.base_currency))
        object.__setattr__(self, "quote_currency", _normalise_currency(self.quote_currency))
        rate = Decimal(str(self.rate))
        if rate <= 0:
            raise MoneyError(f"FX rate must be positive, got {self.rate}")
        object.__setattr__(self, "rate", rate)


def convert(amount: Money, rate: FxRate) -> Money:
    """Convert using a pinned rate. Rounds once, at the target currency's precision."""
    if amount.currency != rate.base_currency:
        raise MoneyError(
            f"rate converts {rate.base_currency}->{rate.quote_currency}, "
            f"but amount is {amount.currency}"
        )
    source_exp = minor_unit_exponent(amount.currency)
    target_exp = minor_unit_exponent(rate.quote_currency)
    major = Decimal(amount.minor).scaleb(-source_exp)
    converted = (major * rate.rate).scaleb(target_exp)
    return Money(int(converted.quantize(Decimal(1), rounding=DEFAULT_ROUNDING)), rate.quote_currency)


# --------------------------------------------------------------------------
# Invariants — asserted at persistence boundaries, exercised by property tests
# --------------------------------------------------------------------------


class ConservationError(AssertionError):
    """Raised when an allocation creates or destroys value."""


def assert_conservation(invoiced: Money, allocated: Money, unapplied: Money) -> None:
    """Allocated + unapplied must exactly equal the invoiced total."""
    if allocated.currency != invoiced.currency or unapplied.currency != invoiced.currency:
        raise ConservationError("conservation check requires a single currency")
    if allocated.minor + unapplied.minor != invoiced.minor:
        raise ConservationError(
            f"allocation does not conserve value: allocated {allocated.minor} + "
            f"unapplied {unapplied.minor} != invoiced {invoiced.minor} "
            f"(difference {allocated.minor + unapplied.minor - invoiced.minor})"
        )


def assert_not_over_allocated(allocated: Money, available: Money) -> None:
    """A payment can never be applied for more than it is worth (spec §14)."""
    if allocated.currency != available.currency:
        raise ConservationError("over-allocation check requires a single currency")
    if allocated.minor > available.minor:
        raise ConservationError(
            f"over-allocation: applied {allocated.minor} against available {available.minor}"
        )
