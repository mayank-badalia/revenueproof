"""Tests for the exact-money core (Step 2a categories 1, 2, 4 and invariants).

The financial correctness of every headline figure in RevenueProof reduces to this
module, so it is tested harder than anything else: worked examples, boundary
conditions, adversarial input, and Hypothesis properties for conservation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.core.money import (
    ConservationError,
    FxRate,
    Money,
    MoneyError,
    allocate_proportionally,
    assert_conservation,
    assert_not_over_allocated,
    convert,
    days_inclusive,
    format_money,
    from_minor_units,
    minor_unit_exponent,
    overlap_days,
    prorate_for_period,
    sum_money,
    to_minor_units,
)

# ---------------------------------------------------------------------------
# 1. Functional correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("100.00", "INR", 10_000),
        ("1", "INR", 100),
        ("0.01", "INR", 1),
        ("600000", "INR", 60_000_000),
        (Decimal("1234.56"), "USD", 123_456),
        ("1,00,000.50", "INR", 10_000_050),   # Indian digit grouping
        ("1000", "JPY", 1000),                # zero-decimal currency
        ("10.505", "KWD", 10_505),            # three-decimal currency
        ("-250.75", "INR", -25_075),          # refunds are negative
    ],
)
def test_to_minor_units(amount, currency, expected):
    assert to_minor_units(amount, currency) == expected


def test_round_trip_preserves_value():
    for raw in ["0.00", "1.00", "999999.99", "-45.67"]:
        minor = to_minor_units(raw, "INR")
        assert from_minor_units(minor, "INR") == Decimal(raw)


def test_minor_unit_exponents():
    assert minor_unit_exponent("INR") == 2
    assert minor_unit_exponent("jpy") == 0
    assert minor_unit_exponent("KWD") == 3


def test_format_money_uses_currency_precision():
    assert format_money(10_000_050, "INR") == "1,00,000.50"
    assert format_money(1000, "JPY") == "1,000"


def test_inr_groups_in_the_indian_system():
    """One crore is 1,00,00,000 — not 10,000,000.

    The room rendered rupees this way and the backend rendered them the Western
    way, so a single workspace showed the same figure two ways on two screens.
    Everything now formats through this one function.
    """
    assert format_money(1_000_000_000, "INR") == "1,00,00,000.00"
    assert format_money(10_000_000_000, "INR") == "10,00,00,000.00"
    assert format_money(100_000, "INR") == "1,000.00"
    assert format_money(99, "INR") == "0.99"
    assert format_money(-1_000_000_000, "INR") == "-1,00,00,000.00"
    # Other currencies keep the thousands grouping their readers expect.
    assert format_money(1_000_000_000, "USD") == "10,000,000.00"


def test_money_arithmetic():
    a = Money.parse("100.00", "INR")
    b = Money.parse("25.50", "INR")
    assert (a + b).minor == 12_550
    assert (a - b).minor == 7_450
    assert (-b).minor == -2_550
    assert str(a) == "INR 100.00"


def test_sum_money():
    items = [Money.parse(x, "INR") for x in ["10.10", "20.20", "30.30"]]
    assert sum_money(items, "INR").minor == 6_060
    assert sum_money([], "INR").is_zero


# ---------------------------------------------------------------------------
# 2. Edge cases and boundary conditions
# ---------------------------------------------------------------------------


def test_banker_rounding_at_the_half():
    # ROUND_HALF_EVEN: .005 goes to the nearest even minor unit.
    assert to_minor_units("1.005", "INR") == 100   # 1.00, not 1.01
    assert to_minor_units("1.015", "INR") == 102   # 1.02


def test_very_large_amount_stays_exact():
    # ₹1,00,00,00,000 (1000 crore) — well beyond float's exact-integer range.
    large = to_minor_units("10000000000.99", "INR")
    assert large == 1_000_000_000_099
    assert from_minor_units(large, "INR") == Decimal("10000000000.99")


def test_zero_and_single_unit():
    assert Money.zero("INR").is_zero
    assert to_minor_units("0", "INR") == 0
    assert to_minor_units("0.01", "INR") == 1


def test_days_inclusive_boundaries():
    assert days_inclusive(date(2026, 4, 1), date(2026, 4, 30)) == 30
    assert days_inclusive(date(2026, 4, 1), date(2026, 4, 1)) == 1     # single day
    assert days_inclusive(date(2026, 4, 2), date(2026, 4, 1)) == 0     # inverted
    # Leap year: FY2024-25 spans 29 Feb.
    assert days_inclusive(date(2024, 4, 1), date(2025, 3, 31)) == 365
    assert days_inclusive(date(2024, 1, 1), date(2024, 12, 31)) == 366


def test_overlap_days_at_period_edges():
    ps, pe = date(2026, 4, 1), date(2027, 3, 31)
    # Ends exactly on the first day of the period.
    assert overlap_days(date(2025, 1, 1), date(2026, 4, 1), ps, pe) == 1
    # Starts exactly on the last day.
    assert overlap_days(date(2027, 3, 31), date(2028, 1, 1), ps, pe) == 1
    # Entirely before / after.
    assert overlap_days(date(2024, 1, 1), date(2026, 3, 31), ps, pe) == 0
    assert overlap_days(date(2027, 4, 1), date(2028, 1, 1), ps, pe) == 0


# ---------------------------------------------------------------------------
# 3. Proration — the rule that stops future contracts inflating ARR
# ---------------------------------------------------------------------------


def test_prorate_full_period_returns_whole_amount():
    amount = Money.parse("600000", "INR")
    result = prorate_for_period(
        amount, date(2026, 4, 1), date(2027, 3, 31), date(2026, 4, 1), date(2027, 3, 31)
    )
    assert result == amount


def test_prorate_contract_outside_period_returns_zero():
    """spec §14: a contract outside the reporting period supports no revenue."""
    amount = Money.parse("1000000", "INR")
    result = prorate_for_period(
        amount, date(2027, 4, 1), date(2028, 3, 31), date(2026, 4, 1), date(2027, 3, 31)
    )
    assert result.is_zero


def test_prorate_half_period():
    # 365-day term, 184 days inside the period.
    amount = Money.parse("365000", "INR")
    result = prorate_for_period(
        amount, date(2026, 10, 1), date(2027, 9, 30), date(2026, 4, 1), date(2027, 3, 31)
    )
    covered = overlap_days(date(2026, 10, 1), date(2027, 9, 30), date(2026, 4, 1), date(2027, 3, 31))
    assert covered == 182
    expected = (Decimal(36_500_000) * Decimal(182) / Decimal(365)).quantize(Decimal(1))
    assert result.minor == int(expected)


# ---------------------------------------------------------------------------
# 4. Negative / adversarial input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "12.3.4", "NaN", "Infinity", "1e999", None, [], {}, "'; DROP TABLE invoices;--"],
)
def test_malformed_amounts_are_rejected(bad):
    with pytest.raises(MoneyError):
        to_minor_units(bad, "INR")


@pytest.mark.parametrize("bad", ["", "IN", "RUPEE", "1NR", None, 123, "₹"])
def test_invalid_currency_codes_are_rejected(bad):
    with pytest.raises(MoneyError):
        to_minor_units("100", bad)


def test_cross_currency_arithmetic_is_blocked():
    """Silently adding USD to INR is how a verified total becomes meaningless."""
    inr = Money.parse("100", "INR")
    usd = Money.parse("100", "USD")
    with pytest.raises(MoneyError, match="currency mismatch"):
        _ = inr + usd
    with pytest.raises(MoneyError):
        sum_money([inr, usd], "INR")


def test_bool_is_not_accepted_as_minor_units():
    # bool is an int subclass; accepting it would let True become ₹0.01.
    with pytest.raises(MoneyError):
        Money(True, "INR")


def test_fx_rate_must_be_positive():
    for bad in ["0", "-1.5"]:
        with pytest.raises(MoneyError):
            FxRate("USD", "INR", Decimal(bad), date(2026, 4, 1), "ecb")


def test_convert_rejects_mismatched_base_currency():
    rate = FxRate("USD", "INR", Decimal("83.25"), date(2026, 4, 1), "ecb")
    with pytest.raises(MoneyError):
        convert(Money.parse("100", "EUR"), rate)


def test_convert_applies_pinned_rate():
    rate = FxRate("USD", "INR", Decimal("83.25"), date(2026, 4, 1), "ecb")
    result = convert(Money.parse("1000.00", "USD"), rate)
    assert result.currency == "INR"
    assert result.minor == 8_325_000


def test_convert_across_different_precisions():
    # USD (2dp) -> JPY (0dp)
    rate = FxRate("USD", "JPY", Decimal("157.30"), date(2026, 4, 1), "ecb")
    assert convert(Money.parse("10.00", "USD"), rate).minor == 1573


# ---------------------------------------------------------------------------
# 5. Allocation and conservation invariants
# ---------------------------------------------------------------------------


def test_allocation_conserves_indivisible_remainder():
    """₹1.00 split three ways must still total ₹1.00."""
    total = Money.parse("1.00", "INR")
    parts = allocate_proportionally(total, [1, 1, 1])
    assert sum(p.minor for p in parts) == 100
    assert sorted(p.minor for p in parts) == [33, 33, 34]


def test_allocation_by_uneven_weights():
    total = Money.parse("1000.00", "INR")
    parts = allocate_proportionally(total, [7, 2, 1])
    assert sum(p.minor for p in parts) == 100_000
    assert parts[0].minor == 70_000


def test_allocation_edge_cases():
    total = Money.parse("100.00", "INR")
    assert allocate_proportionally(total, []) == []
    assert allocate_proportionally(total, [5])[0].minor == 10_000       # single item
    zero_weights = allocate_proportionally(total, [0, 0])              # degenerate
    assert sum(p.minor for p in zero_weights) == 10_000
    with pytest.raises(MoneyError):
        allocate_proportionally(total, [1, -1])


def test_conservation_assertions():
    invoiced = Money.parse("1000.00", "INR")
    assert_conservation(invoiced, Money.parse("600.00", "INR"), Money.parse("400.00", "INR"))
    with pytest.raises(ConservationError):
        assert_conservation(invoiced, Money.parse("600.00", "INR"), Money.parse("500.00", "INR"))


def test_over_allocation_is_blocked():
    """spec §14: a payment cannot be counted twice across multiple invoices."""
    available = Money.parse("500.00", "INR")
    assert_not_over_allocated(Money.parse("500.00", "INR"), available)
    with pytest.raises(ConservationError, match="over-allocation"):
        assert_not_over_allocated(Money.parse("500.01", "INR"), available)


# ---------------------------------------------------------------------------
# 6. Property-based tests (core_resoruces.md requires conservation properties)
# ---------------------------------------------------------------------------

minor_amounts = st.integers(min_value=0, max_value=10**14)
weight_lists = st.lists(st.integers(min_value=0, max_value=10_000), min_size=1, max_size=25)


@given(total=minor_amounts, weights=weight_lists)
@hyp_settings(max_examples=300, deadline=None)
def test_property_allocation_always_conserves(total: int, weights: list[int]):
    """No allocation may create or destroy a single paisa."""
    parts = allocate_proportionally(Money(total, "INR"), weights)
    assert sum(p.minor for p in parts) == total
    assert len(parts) == len(weights)


@given(total=minor_amounts, weights=weight_lists)
@hyp_settings(max_examples=200, deadline=None)
def test_property_allocation_is_deterministic(total: int, weights: list[int]):
    """Same input, same split — reruns must not shuffle money between customers."""
    first = allocate_proportionally(Money(total, "INR"), weights)
    second = allocate_proportionally(Money(total, "INR"), weights)
    assert [p.minor for p in first] == [p.minor for p in second]


@given(
    amount=st.integers(min_value=0, max_value=10**12),
    term_days=st.integers(min_value=1, max_value=1500),
    offset=st.integers(min_value=-800, max_value=800),
)
@hyp_settings(max_examples=300, deadline=None)
def test_property_proration_never_exceeds_the_whole(amount: int, term_days: int, offset: int):
    """A prorated share is always between zero and the full amount."""
    period_start = date(2026, 4, 1)
    period_end = date(2027, 3, 31)
    term_start = date.fromordinal(period_start.toordinal() + offset)
    term_end = date.fromordinal(term_start.toordinal() + term_days - 1)

    result = prorate_for_period(
        Money(amount, "INR"), term_start, term_end, period_start, period_end
    )
    assert 0 <= result.minor <= amount


@given(st.integers(min_value=-(10**12), max_value=10**12))
@hyp_settings(max_examples=200, deadline=None)
def test_property_minor_unit_round_trip(minor: int):
    """Converting to major units and back is lossless."""
    major = from_minor_units(minor, "INR")
    assert to_minor_units(major, "INR") == minor
