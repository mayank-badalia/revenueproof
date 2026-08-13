"""Synthetic financial records — spec §15 and the §19 adversarial scenarios.

**Design decision that matters:** this module emits payloads in each provider's
*native* shape — Razorpay's integer-paise JSON, Zoho Books' decimal-string JSON, a
bank CSV's text columns — not RevenueProof's canonical schema. The normalisers in
`app/connectors/normalize.py` therefore run against realistic input, and swapping a
synthetic source for a live API key changes nothing downstream. Emitting canonical
records here would leave the entire normalisation layer untested.

Every record below is deliberate. The dataset encodes each failure mode the product
claims to catch, so a passing verification run is evidence the engine works rather
than evidence the data was easy.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.connectors.synthetic import customers as roster

# Reporting period under review. Everything is dated relative to this.
PERIOD_START = date(2026, 4, 1)
PERIOD_END = date(2027, 3, 31)

GST_RATE = Decimal("0.18")


def _unix(day: date, hour: int = 11) -> int:
    return int(datetime.combine(day, time(hour, 30)).timestamp())


def _paise(amount: Decimal | int | str) -> int:
    return int((Decimal(str(amount)) * 100).to_integral_value())


@dataclass
class InvoiceSpec:
    """One invoice, in the terms a founder would recognise."""

    key: str
    customer_key: str
    number: str
    issue_date: date
    amount: Decimal                       # pre-tax
    description: str
    status: str = "paid"                  # zoho status vocabulary
    is_one_time: bool = False
    balance: Decimal | None = None        # None => derived from status
    due_days: int = 30
    notes: str = ""


@dataclass
class PaymentSpec:
    """One processor payment event."""

    key: str
    customer_key: str
    amount: Decimal                       # gross, pre-fee
    payment_date: date
    status: str = "captured"
    invoice_key: str | None = None
    description: str = ""
    method: str = "netbanking"
    refunded: Decimal = Decimal("0")
    fee_pct: Decimal = Decimal("0.02")
    notes: str = ""


@dataclass
class RefundSpec:
    key: str
    payment_key: str
    amount: Decimal
    refund_date: date
    reason: str = ""
    is_chargeback: bool = False
    notes: str = ""


@dataclass
class BankSpec:
    """One bank statement line."""

    key: str
    day: date
    amount: Decimal
    direction: str                        # credit | debit
    counterparty: str
    reference: str
    narration: str
    notes: str = ""
    covers_invoices: list[str] = field(default_factory=list)


# ===========================================================================
# INVOICES  (~50, mixed statuses — spec §15)
# ===========================================================================

INVOICES: list[InvoiceSpec] = [
    # --- Northstar: largest customer, annual + setup, fully paid --------------
    InvoiceSpec("ns_annual", "northstar", "INV-2026-001", date(2026, 4, 5),
                Decimal("3200000"), "Annual platform subscription FY2026-27"),
    InvoiceSpec("ns_setup", "northstar", "INV-2026-002", date(2026, 4, 5),
                Decimal("400000"), "One-time implementation and onboarding",
                is_one_time=True),

    # --- Lumen: 12 clean monthly invoices (the control case) ------------------
    *[
        InvoiceSpec(
            f"lumen_m{month:02d}", "lumen_software", f"INV-2026-1{month:02d}",
            date(2026, 4, 1) + timedelta(days=30 * (month - 1)),
            Decimal("75000"), f"Monthly subscription — month {month}",
        )
        for month in range(1, 13)
    ],

    # --- Kestrel: annual + onboarding ----------------------------------------
    InvoiceSpec("kestrel_annual", "kestrel_logistics", "INV-2026-020", date(2026, 4, 10),
                Decimal("1200000"), "Annual subscription FY2026-27"),
    InvoiceSpec("kestrel_setup", "kestrel_logistics", "INV-2026-021", date(2026, 4, 10),
                Decimal("150000"), "Onboarding and configuration fee", is_one_time=True),

    # --- Terrace: 4 quarterly -------------------------------------------------
    *[
        InvoiceSpec(
            f"terrace_q{quarter}", "terrace_ventures", f"INV-2026-03{quarter}",
            date(2026, 4, 1) + timedelta(days=91 * (quarter - 1)),
            Decimal("225000"), f"Quarterly subscription Q{quarter} FY2026-27",
        )
        for quarter in range(1, 5)
    ],

    # --- Blue Harbor Analytics: 12 monthly -----------------------------------
    *[
        InvoiceSpec(
            f"bh_m{month:02d}", "blue_harbor", f"INV-2026-2{month:02d}",
            date(2026, 4, 1) + timedelta(days=30 * (month - 1)),
            Decimal("50000"), f"Monthly analytics subscription — month {month}",
        )
        for month in range(1, 13)
    ],

    # --- Blue Harbour Logistics: the near-duplicate name (must stay separate) --
    InvoiceSpec("bhl_annual", "blue_harbour_logistics", "INV-2026-040", date(2026, 5, 1),
                Decimal("240000"), "Annual logistics tracking subscription"),

    # --- ADVERSARIAL: one-time implementation invoiced as if recurring --------
    InvoiceSpec("quantum_impl", "quantum_retail", "INV-2026-050", date(2026, 5, 5),
                Decimal("1500000"),
                "Annual subscription - implementation and migration programme",
                is_one_time=True,
                notes="Company presents this as ARR; the contract says non-recurring."),
    InvoiceSpec("quantum_sub", "quantum_retail", "INV-2026-051", date(2026, 5, 5),
                Decimal("300000"), "Platform subscription FY2026-27"),

    # --- Vertex: ambiguous contract, invoiced monthly -------------------------
    *[
        InvoiceSpec(
            f"vertex_m{month:02d}", "vertex_labs", f"INV-2026-6{month:02d}",
            date(2026, 6, 1) + timedelta(days=30 * (month - 1)),
            Decimal("50000"), f"Subscription — month {month}",
        )
        for month in range(1, 7)
    ],

    # --- Cobalt: paid then fully refunded -------------------------------------
    InvoiceSpec("cobalt_annual", "cobalt_media", "INV-2026-070", date(2026, 4, 12),
                Decimal("600000"), "Annual subscription FY2026-27", status="paid",
                notes="Refunded in full within the 30-day cancellation window."),

    # --- Halcyon: paid, later charged back ------------------------------------
    InvoiceSpec("halcyon_annual", "halcyon_health", "INV-2026-080", date(2026, 4, 20),
                Decimal("500000"), "Annual subscription FY2026-27"),

    # --- Silverline: three invoices settled by ONE combined bank credit -------
    InvoiceSpec("silver_a", "silverline", "INV-2026-090", date(2026, 4, 20),
                Decimal("400000"), "Annual subscription — campus A"),
    InvoiceSpec("silver_b", "silverline", "INV-2026-091", date(2026, 4, 20),
                Decimal("300000"), "Annual subscription — campus B"),
    InvoiceSpec("silver_c", "silverline", "INV-2026-092", date(2026, 4, 20),
                Decimal("200000"), "Annual subscription — campus C"),
    InvoiceSpec("silver_setup", "silverline", "INV-2026-093", date(2026, 4, 20),
                Decimal("100000"), "One-time setup across campuses", is_one_time=True),

    # --- Ironbridge: one invoice settled by THREE partial payments ------------
    InvoiceSpec("iron_h1", "ironbridge", "INV-2026-300", date(2026, 4, 8),
                Decimal("400000"), "Subscription H1 FY2026-27",
                status="partially_paid"),
    InvoiceSpec("iron_h2", "ironbridge", "INV-2026-301", date(2026, 10, 5),
                Decimal("500000"), "Subscription H2 FY2026-27 (revised price)"),

    # --- Tidewater: invoiced, never paid --------------------------------------
    InvoiceSpec("tide_annual", "tidewater", "INV-2026-310", date(2026, 7, 1),
                Decimal("450000"), "Annual subscription FY2026-27",
                status="overdue",
                # balance left to derive as the full tax-inclusive total: nothing
                # was paid, so the whole invoice is outstanding.
                notes="No payment evidence anywhere → INVOICED_UNPAID."),

    # --- Meridian: parent pays the subsidiary's invoice ------------------------
    InvoiceSpec("meridian_svc", "meridian_systems", "INV-2026-320", date(2026, 9, 1),
                Decimal("600000"), "Professional services engagement",
                notes="Settled by the parent, Meridian Holdings."),

    # --- Voided invoice (must not count) ---------------------------------------
    InvoiceSpec("void_one", "crestview", "INV-2026-330", date(2026, 6, 15),
                Decimal("180000"), "Cancelled order — raised in error",
                status="void", balance=Decimal("0"),
                notes="Void status; must be excluded from every total."),

    # --- Shared payment agent -------------------------------------------------
    InvoiceSpec("crest_annual", "crestview", "INV-2026-340", date(2026, 6, 20),
                Decimal("360000"), "Annual retail analytics subscription"),
    InvoiceSpec("pinnacle_annual", "pinnacle_foods", "INV-2026-341", date(2026, 6, 20),
                Decimal("300000"), "Annual subscription FY2026-27"),

    # --- Draft invoice (not yet issued) ----------------------------------------
    InvoiceSpec("draft_one", "orchid_hospitality", "INV-2026-350", date(2027, 1, 10),
                Decimal("800000"), "Annual subscription — draft",
                status="draft",
                notes="Draft only; contract exists but nothing has been issued."),
]

INVOICE_BY_KEY = {inv.key: inv for inv in INVOICES}


# ===========================================================================
# PAYMENTS  (Razorpay-shaped)
# ===========================================================================

PAYMENTS: list[PaymentSpec] = [
    PaymentSpec("pay_ns_annual", "northstar", Decimal("3776000"), date(2026, 4, 12),
                invoice_key="ns_annual", description="Northstar Tech annual + GST"),
    PaymentSpec("pay_ns_setup", "northstar", Decimal("472000"), date(2026, 4, 14),
                invoice_key="ns_setup", description="Northstar implementation fee"),

    *[
        PaymentSpec(
            f"pay_lumen_m{month:02d}", "lumen_software", Decimal("88500"),
            date(2026, 4, 3) + timedelta(days=30 * (month - 1)),
            invoice_key=f"lumen_m{month:02d}",
            description=f"Lumen Software monthly subscription M{month}",
        )
        for month in range(1, 13)
    ],

    PaymentSpec("pay_kestrel", "kestrel_logistics", Decimal("1416000"), date(2026, 4, 18),
                invoice_key="kestrel_annual", description="Kestrel annual subscription"),
    PaymentSpec("pay_kestrel_setup", "kestrel_logistics", Decimal("177000"), date(2026, 4, 18),
                invoice_key="kestrel_setup", description="Kestrel onboarding"),

    *[
        PaymentSpec(
            f"pay_terrace_q{quarter}", "terrace_ventures", Decimal("265500"),
            date(2026, 4, 5) + timedelta(days=91 * (quarter - 1)),
            invoice_key=f"terrace_q{quarter}",
            description=f"Terrace Ventures Q{quarter}",
        )
        for quarter in range(1, 5)
    ],

    *[
        PaymentSpec(
            f"pay_bh_m{month:02d}", "blue_harbor", Decimal("59000"),
            date(2026, 4, 4) + timedelta(days=30 * (month - 1)),
            invoice_key=f"bh_m{month:02d}",
            description=f"Blue Harbor Analytics M{month}",
        )
        for month in range(1, 13)
    ],

    PaymentSpec("pay_bhl", "blue_harbour_logistics", Decimal("283200"), date(2026, 5, 8),
                invoice_key="bhl_annual", description="Blue Harbour Logistics annual"),

    PaymentSpec("pay_quantum_impl", "quantum_retail", Decimal("1770000"), date(2026, 5, 15),
                invoice_key="quantum_impl", description="Quantum Retail implementation"),
    PaymentSpec("pay_quantum_sub", "quantum_retail", Decimal("354000"), date(2026, 5, 15),
                invoice_key="quantum_sub", description="Quantum Retail subscription"),

    *[
        PaymentSpec(
            f"pay_vertex_m{month:02d}", "vertex_labs", Decimal("59000"),
            date(2026, 6, 5) + timedelta(days=30 * (month - 1)),
            invoice_key=f"vertex_m{month:02d}",
            description=f"Vertex Labs M{month}",
        )
        for month in range(1, 7)
    ],

    # Paid, then fully refunded six days later — the rapid-refund pattern.
    PaymentSpec("pay_cobalt", "cobalt_media", Decimal("708000"), date(2026, 4, 15),
                invoice_key="cobalt_annual", refunded=Decimal("708000"),
                status="refunded", description="Cobalt Media annual subscription",
                notes="Money in and back out within 6 days."),

    # Paid, later charged back.
    PaymentSpec("pay_halcyon", "halcyon_health", Decimal("590000"), date(2026, 4, 25),
                invoice_key="halcyon_annual", refunded=Decimal("590000"),
                status="refunded", description="Halcyon Health annual",
                notes="Chargeback raised in December, after the first report."),

    # Combined: one payment covering three invoices (§18).
    PaymentSpec("pay_silverline_combined", "silverline", Decimal("1180000"),
                date(2026, 4, 28), description="Silverline Education - INV-090/091/092/093",
                notes="Settles four invoices at once."),

    # Partial: three instalments against one invoice.
    PaymentSpec("pay_iron_1", "ironbridge", Decimal("200000"), date(2026, 4, 20),
                invoice_key="iron_h1", description="Ironbridge instalment 1 of 3"),
    PaymentSpec("pay_iron_2", "ironbridge", Decimal("150000"), date(2026, 5, 20),
                invoice_key="iron_h1", description="Ironbridge instalment 2 of 3"),
    PaymentSpec("pay_iron_3", "ironbridge", Decimal("122000"), date(2026, 6, 22),
                invoice_key="iron_h1", description="Ironbridge instalment 3 of 3"),
    PaymentSpec("pay_iron_h2", "ironbridge", Decimal("590000"), date(2026, 10, 12),
                invoice_key="iron_h2", description="Ironbridge H2 revised subscription"),

    # FAILED payment — spec §14: contributes zero cash.
    PaymentSpec("pay_failed_1", "tidewater", Decimal("531000"), date(2026, 7, 15),
                status="failed", invoice_key="tide_annual",
                description="Tidewater annual - card declined",
                notes="Failed. Must contribute nothing; invoice stays unpaid."),
    PaymentSpec("pay_failed_2", "crestview", Decimal("424800"), date(2026, 6, 25),
                status="failed", description="Crestview - insufficient funds"),

    # DUPLICATE-LIKE event (§15): same customer, same amount, next day.
    PaymentSpec("pay_bh_dup", "blue_harbor", Decimal("59000"), date(2026, 4, 5),
                description="Blue Harbor Analytics M1",
                notes="Near-duplicate of pay_bh_m01 — same amount, one day apart."),

    # Parent settling the subsidiary's invoice.
    PaymentSpec("pay_meridian_parent", "meridian_holdings", Decimal("708000"),
                date(2026, 9, 10), invoice_key="meridian_svc",
                description="Meridian Holdings on behalf of Meridian Systems",
                notes="Payer entity differs from the invoiced entity."),

    # Shared payment agent covering two unrelated customers.
    PaymentSpec("pay_crest", "crestview", Decimal("424800"), date(2026, 6, 28),
                invoice_key="crest_annual",
                description="GLOBAL PAY SERVICES ref CRESTVIEW"),
    PaymentSpec("pay_pinnacle", "pinnacle_foods", Decimal("354000"), date(2026, 6, 28),
                invoice_key="pinnacle_annual",
                description="GLOBAL PAY SERVICES ref PINNACLE"),

    # Cash with NO invoice and NO contract behind it.
    PaymentSpec("pay_zenith", "unknown_payer", Decimal("250000"), date(2026, 8, 14),
                description="Zenith Consulting - consulting retainer",
                notes="No invoice, no contract → PAYMENT_WITHOUT_SUPPORT."),

    # Related-party inflow that later flows straight back out.
    PaymentSpec("pay_apex_in", "apex_holdings", Decimal("1500000"), date(2027, 3, 25),
                description="Apex Founder Holdings - advance",
                notes="Received 25 Mar, returned 29 Mar. Period-end + circular."),
]

PAYMENT_BY_KEY = {p.key: p for p in PAYMENTS}


# ===========================================================================
# REFUNDS
# ===========================================================================

REFUNDS: list[RefundSpec] = [
    RefundSpec("rfnd_cobalt", "pay_cobalt", Decimal("708000"), date(2026, 4, 21),
               reason="Cancelled within the 30-day window",
               notes="Full refund six days after receipt."),
    RefundSpec("rfnd_halcyon", "pay_halcyon", Decimal("590000"), date(2026, 12, 3),
               reason="Chargeback raised by cardholder", is_chargeback=True,
               notes="Arrives after the first report version was published."),
    RefundSpec("rfnd_quantum_partial", "pay_quantum_impl", Decimal("354000"),
               date(2026, 9, 10),
               reason="Partial credit for descoped migration workstream",
               notes="Partial refund must reduce supported revenue proportionally."),
]


# ===========================================================================
# BANK TRANSACTIONS
# ===========================================================================

MAIN_ACCOUNT = "50100234567890"
SECONDARY_ACCOUNT = "50100987654321"

BANK: list[BankSpec] = [
    # Settlements matching processor payments (net of ~2% fee + GST on fee).
    BankSpec("bank_ns_1", date(2026, 4, 14), Decimal("3700480"), "credit",
             "NSTAR TECH PVT", "RAZORPAY SETTL 4412", "RAZORPAY SETTLEMENT NSTAR TECH PVT"),
    BankSpec("bank_ns_2", date(2026, 4, 16), Decimal("462560"), "credit",
             "NSTAR TECH PVT", "RAZORPAY SETTL 4419", "RAZORPAY SETTLEMENT NSTAR TECH PVT"),
    BankSpec("bank_kestrel", date(2026, 4, 20), Decimal("1387680"), "credit",
             "KESTREL LOGISTICS", "RAZORPAY SETTL 4502", "RAZORPAY SETTLEMENT KESTREL"),
    BankSpec("bank_kestrel_setup", date(2026, 4, 20), Decimal("173460"), "credit",
             "KESTREL LOGISTICS", "RAZORPAY SETTL 4503", "RAZORPAY SETTLEMENT KESTREL"),

    # Combined credit settling four Silverline invoices at once.
    BankSpec("bank_silverline", date(2026, 4, 30), Decimal("1156400"), "credit",
             "SILVERLINE EDU PVT", "NEFT SILVERLINE 8891",
             "NEFT CR SILVERLINE EDU PVT LTD INV090 091 092 093",
             covers_invoices=["silver_a", "silver_b", "silver_c", "silver_setup"],
             notes="One credit, four invoices."),

    # Ironbridge partial instalments.
    BankSpec("bank_iron_1", date(2026, 4, 22), Decimal("196000"), "credit",
             "IRONBRIDGE MFG PVT", "NEFT IRONBRIDGE 1", "NEFT CR IRONBRIDGE MFG INSTALMENT 1"),
    BankSpec("bank_iron_2", date(2026, 5, 22), Decimal("147000"), "credit",
             "IRONBRIDGE MFG PVT", "NEFT IRONBRIDGE 2", "NEFT CR IRONBRIDGE MFG INSTALMENT 2"),
    BankSpec("bank_iron_3", date(2026, 6, 24), Decimal("119560"), "credit",
             "IRONBRIDGE MFG PVT", "NEFT IRONBRIDGE 3", "NEFT CR IRONBRIDGE MFG INSTALMENT 3"),
    BankSpec("bank_iron_h2", date(2026, 10, 14), Decimal("578200"), "credit",
             "IRONBRIDGE MFG PVT", "NEFT IRONBRIDGE H2", "NEFT CR IRONBRIDGE MFG H2"),

    # Cobalt: in, then straight back out.
    BankSpec("bank_cobalt_in", date(2026, 4, 17), Decimal("693840"), "credit",
             "COBALT MEDIA NET", "RAZORPAY SETTL 4460", "RAZORPAY SETTLEMENT COBALT MEDIA"),
    BankSpec("bank_cobalt_out", date(2026, 4, 21), Decimal("708000"), "debit",
             "COBALT MEDIA NET", "REFUND COBALT", "RAZORPAY REFUND COBALT MEDIA NETWORKS",
             notes="Full reversal of the receipt above."),

    # Halcyon chargeback.
    BankSpec("bank_halcyon_in", date(2026, 4, 27), Decimal("578200"), "credit",
             "HALCYON HEALTH TECH", "RAZORPAY SETTL 4488", "RAZORPAY SETTLEMENT HALCYON"),
    BankSpec("bank_halcyon_cb", date(2026, 12, 5), Decimal("590000"), "debit",
             "HALCYON HEALTH TECH", "CHARGEBACK HALCYON", "CHARGEBACK DEBIT HALCYON HEALTH"),

    # Monthly Lumen settlements.
    *[
        BankSpec(
            f"bank_lumen_{month:02d}",
            date(2026, 4, 5) + timedelta(days=30 * (month - 1)),
            Decimal("86730"), "credit", "LUMEN SOFTWARE PVT",
            f"RAZORPAY SETTL 5{month:03d}", f"RAZORPAY SETTLEMENT LUMEN SOFTWARE M{month}",
        )
        for month in range(1, 13)
    ],

    # Blue Harbor monthly settlements.
    *[
        BankSpec(
            f"bank_bh_{month:02d}",
            date(2026, 4, 6) + timedelta(days=30 * (month - 1)),
            Decimal("57820"), "credit", "BLUE HARBOR ANALYTICS",
            f"RAZORPAY SETTL 6{month:03d}", f"RAZORPAY SETTLEMENT BLUE HARBOR M{month}",
        )
        for month in range(1, 13)
    ],

    BankSpec("bank_terrace_q1", date(2026, 4, 7), Decimal("260190"), "credit",
             "TERRACE VENTURES", "RAZORPAY SETTL 7001", "RAZORPAY SETTLEMENT TERRACE Q1"),
    BankSpec("bank_terrace_q2", date(2026, 7, 7), Decimal("260190"), "credit",
             "TERRACE VENTURES", "RAZORPAY SETTL 7002", "RAZORPAY SETTLEMENT TERRACE Q2"),
    BankSpec("bank_terrace_q3", date(2026, 10, 6), Decimal("260190"), "credit",
             "TERRACE VENTURES", "RAZORPAY SETTL 7003", "RAZORPAY SETTLEMENT TERRACE Q3"),
    BankSpec("bank_terrace_q4", date(2027, 1, 5), Decimal("260190"), "credit",
             "TERRACE VENTURES", "RAZORPAY SETTL 7004", "RAZORPAY SETTLEMENT TERRACE Q4"),

    BankSpec("bank_quantum_impl", date(2026, 5, 18), Decimal("1734600"), "credit",
             "QUANTUM RETAIL SOLN", "RAZORPAY SETTL 8001", "RAZORPAY SETTLEMENT QUANTUM RETAIL"),
    BankSpec("bank_quantum_sub", date(2026, 5, 18), Decimal("346920"), "credit",
             "QUANTUM RETAIL SOLN", "RAZORPAY SETTL 8002", "RAZORPAY SETTLEMENT QUANTUM RETAIL"),
    BankSpec("bank_quantum_refund", date(2026, 9, 12), Decimal("354000"), "debit",
             "QUANTUM RETAIL SOLN", "REFUND QUANTUM", "RAZORPAY REFUND QUANTUM RETAIL PARTIAL"),

    BankSpec("bank_bhl", date(2026, 5, 10), Decimal("277536"), "credit",
             "BLUE HARBOUR LOG LLP", "NEFT BHLOG 001", "NEFT CR BLUE HARBOUR LOGISTICS LLP"),

    # Vertex monthly.
    *[
        BankSpec(
            f"bank_vertex_{month:02d}",
            date(2026, 6, 7) + timedelta(days=30 * (month - 1)),
            Decimal("57820"), "credit", "VERTEX LABS PVT LTD",
            f"RAZORPAY SETTL 9{month:03d}", f"RAZORPAY SETTLEMENT VERTEX LABS M{month}",
        )
        for month in range(1, 7)
    ],

    # Parent paying for the subsidiary.
    BankSpec("bank_meridian", date(2026, 9, 12), Decimal("693840"), "credit",
             "MERIDIAN HOLDINGS PVT", "NEFT MERIDIAN 001",
             "NEFT CR MERIDIAN HOLDINGS PVT LTD REF INV-2026-120",
             notes="Payer is the parent; the invoice names the subsidiary."),

    # Shared payment agent — two customers, one originating account.
    BankSpec("bank_crest", date(2026, 6, 30), Decimal("416304"), "credit",
             "GLOBAL PAY SERVICES", "AGENT CREST 001",
             "NEFT CR GLOBAL PAY SERVICES A/C CRESTVIEW RETAIL"),
    BankSpec("bank_pinnacle", date(2026, 6, 30), Decimal("346920"), "credit",
             "GLOBAL PAY SERVICES", "AGENT PINN 001",
             "NEFT CR GLOBAL PAY SERVICES A/C PINNACLE FOODS"),

    # Unsupported receipt.
    BankSpec("bank_zenith", date(2026, 8, 16), Decimal("245000"), "credit",
             "ZENITH CONSULTING", "NEFT ZENITH 001",
             "NEFT CR ZENITH CONSULTING RETAINER",
             notes="No invoice or contract behind this credit."),

    # --- TWO CIRCULAR-LOOKING TRANSFERS (spec §15) ---------------------------
    BankSpec("bank_apex_in_1", date(2027, 3, 25), Decimal("1500000"), "credit",
             "APEX FOUNDER HOLDINGS", "RTGS APEX IN 1",
             "RTGS CR APEX FOUNDER HOLDINGS PVT LTD ADVANCE",
             notes="Circular leg 1: money in, four days before period end."),
    BankSpec("bank_apex_out_1", date(2027, 3, 29), Decimal("1495000"), "debit",
             "APEX FOUNDER HOLDINGS", "RTGS APEX OUT 1",
             "RTGS DR APEX FOUNDER HOLDINGS PVT LTD ADVISORY FEE",
             notes="Circular leg 2: 99.7% returned as an 'advisory fee'."),
    BankSpec("bank_apex_in_2", date(2027, 2, 10), Decimal("800000"), "credit",
             "APEX FOUNDER HOLDINGS", "RTGS APEX IN 2",
             "RTGS CR APEX FOUNDER HOLDINGS INTERCOMPANY"),
    BankSpec("bank_apex_out_2", date(2027, 2, 13), Decimal("800000"), "debit",
             "APEX FOUNDER HOLDINGS", "RTGS APEX OUT 2",
             "RTGS DR APEX FOUNDER HOLDINGS INTERCOMPANY REVERSAL",
             notes="Second circular pair: exact same amount returned in 3 days."),

    # Ordinary operating outflows — noise that must NOT be read as revenue.
    BankSpec("bank_salary_1", date(2026, 4, 30), Decimal("1850000"), "debit",
             "PAYROLL", "SALARY APR26", "SALARY DISBURSEMENT APRIL 2026"),
    BankSpec("bank_rent_1", date(2026, 4, 5), Decimal("225000"), "debit",
             "PROPERTY LANDLORD", "RENT APR26", "OFFICE RENT APRIL 2026"),
    BankSpec("bank_aws", date(2026, 5, 2), Decimal("412000"), "debit",
             "AWS INDIA", "AWS MAY26", "CLOUD HOSTING CHARGES"),
]


# ===========================================================================
# Provider-shaped payload builders
# ===========================================================================


def zoho_contacts() -> list[dict[str, Any]]:
    """Customers in Zoho Books `contact` shape."""
    payloads = []
    for index, customer in enumerate(roster.CUSTOMERS, start=1):
        if not customer.zoho_name:
            continue  # deliberately absent from accounting (the unsupported payer)
        payloads.append(
            {
                "contact_id": f"zc_{index:06d}",
                "contact_name": customer.zoho_name,
                "company_name": customer.zoho_name,
                "email": customer.email or "",
                "phone": "",
                "website": f"https://{customer.domain}" if customer.domain else "",
                "gst_no": customer.gstin or "",
                "billing_address": {
                    "address": customer.address,
                    "country": "India",
                },
                "status": "active",
                "currency_code": "INR",
            }
        )
    return payloads


def zoho_invoices() -> list[dict[str, Any]]:
    """Invoices in Zoho Books shape (decimal numbers, string dates)."""
    contact_ids = {
        customer.key: f"zc_{index:06d}"
        for index, customer in enumerate(roster.CUSTOMERS, start=1)
    }
    payloads = []
    for index, spec in enumerate(INVOICES, start=1):
        customer = roster.get(spec.customer_key)
        tax = (spec.amount * GST_RATE).quantize(Decimal("0.01"))
        total = spec.amount + tax
        balance = (
            spec.balance
            if spec.balance is not None
            else (total if spec.status in {"overdue", "draft", "sent"} else Decimal("0"))
        )
        payloads.append(
            {
                "invoice_id": f"zi_{index:06d}",
                "invoice_number": spec.number,
                "customer_id": contact_ids[spec.customer_key],
                "customer_name": customer.zoho_name or customer.legal_name,
                "status": spec.status,
                "date": spec.issue_date.isoformat(),
                "due_date": (spec.issue_date + timedelta(days=spec.due_days)).isoformat(),
                "currency_code": "INR",
                "sub_total": float(spec.amount),
                "tax_total": float(tax),
                "total": float(total),
                "balance": float(balance),
                "reference_number": spec.number,
                "line_items": [
                    {
                        "line_item_id": f"li_{index:06d}_1",
                        "name": spec.description[:100],
                        "description": _translate(spec.description),
                        "quantity": 1,
                        "rate": float(spec.amount),
                        "item_total": float(spec.amount),
                    }
                ],
            }
        )
    return payloads


def zoho_credit_notes() -> list[dict[str, Any]]:
    """Accounting-side reversals mirroring the processor refunds."""
    invoice_ids = {spec.key: f"zi_{i:06d}" for i, spec in enumerate(INVOICES, start=1)}
    contact_ids = {
        customer.key: f"zc_{index:06d}"
        for index, customer in enumerate(roster.CUSTOMERS, start=1)
    }
    return [
        {
            "creditnote_id": "zcn_000001",
            "creditnote_number": "CN-2026-001",
            "customer_id": contact_ids["cobalt_media"],
            "invoice_id": invoice_ids["cobalt_annual"],
            "date": "2026-04-21",
            "currency_code": "INR",
            "total": 708000.0,
            "reason": "Cancellation within the 30-day window",
            "status": "closed",
        },
        {
            "creditnote_id": "zcn_000002",
            "creditnote_number": "CN-2026-002",
            "customer_id": contact_ids["quantum_retail"],
            "invoice_id": invoice_ids["quantum_impl"],
            "date": "2026-09-10",
            "currency_code": "INR",
            "total": 354000.0,
            "reason": "Descoped migration workstream",
            "status": "closed",
        },
    ]


def razorpay_payments() -> list[dict[str, Any]]:
    """Payments in Razorpay shape: integer paise, unix timestamps."""
    invoice_ids = {spec.key: f"zi_{i:06d}" for i, spec in enumerate(INVOICES, start=1)}
    payloads = []
    for index, spec in enumerate(PAYMENTS, start=1):
        customer = roster.get(spec.customer_key)
        amount = _paise(spec.amount)
        # Razorpay reports zero fee on a failed payment.
        fee = 0 if spec.status == "failed" else int(amount * spec.fee_pct)
        tax = int(fee * float(GST_RATE))
        payloads.append(
            {
                "id": f"pay_{index:012d}",
                "entity": "payment",
                "amount": amount,
                "currency": "INR",
                "status": spec.status,
                "order_id": f"order_{index:012d}",
                "invoice_id": invoice_ids.get(spec.invoice_key or "", None),
                "international": False,
                "method": spec.method,
                "amount_refunded": _paise(spec.refunded),
                "refund_status": "full" if spec.refunded >= spec.amount > 0 else None,
                "captured": spec.status in {"captured", "refunded"},
                "description": _translate(spec.description),
                "email": customer.email or "",
                "contact": "",
                "fee": fee,
                "tax": tax,
                "error_code": "BAD_REQUEST_ERROR" if spec.status == "failed" else None,
                "error_description": (
                    "Payment failed" if spec.status == "failed" else None
                ),
                "created_at": _unix(spec.payment_date),
                "notes": {"customer_name": customer.zoho_name or customer.legal_name},
            }
        )
    return payloads


def razorpay_refunds() -> list[dict[str, Any]]:
    payment_ids = {spec.key: f"pay_{i:012d}" for i, spec in enumerate(PAYMENTS, start=1)}
    return [
        {
            "id": f"rfnd_{index:012d}",
            "entity": "refund",
            "amount": _paise(spec.amount),
            "currency": "INR",
            "payment_id": payment_ids[spec.payment_key],
            "notes": {"reason": spec.reason},
            "receipt": None,
            "status": "processed",
            "speed_processed": "normal",
            "created_at": _unix(spec.refund_date),
        }
        for index, spec in enumerate(REFUNDS, start=1)
    ]


def razorpay_disputes() -> list[dict[str, Any]]:
    """Chargebacks — reported separately from refunds by the processor."""
    payment_ids = {spec.key: f"pay_{i:012d}" for i, spec in enumerate(PAYMENTS, start=1)}
    return [
        {
            "id": "disp_000000000001",
            "entity": "dispute",
            "payment_id": payment_ids["pay_halcyon"],
            "amount": _paise(Decimal("590000")),
            "currency": "INR",
            "reason_code": "chargeback_fraud",
            "phase": "chargeback",
            "status": "lost",
            "created_at": _unix(date(2026, 12, 3)),
        }
    ]


# ---------------------------------------------------------------------------
# Roster translation
#
# The bank narrations and payment descriptions below are written with the §15
# company names inline, because that is how a real statement reads — the name is
# *inside* the free text, not a field beside it. When a generated roster is active
# those literals would otherwise leak the template's companies into an otherwise
# fresh dataset, producing a statement that credits "NSTAR TECH PVT" for an invoice
# raised on Everest Systems. The narration is therefore translated the same way a
# bank would have written it in the first place.
# ---------------------------------------------------------------------------


def _translations() -> list[tuple[str, str]]:
    """Template spelling → active spelling, longest first so partials cannot win."""
    from .customers import template

    pairs: list[tuple[str, str]] = []
    active = {c.key: c for c in roster.CUSTOMERS}
    for original in template():
        current = active.get(original.key)
        if current is None or current is original:
            continue
        for old, new in (
            (original.bank_narration_name, current.bank_narration_name),
            (original.legal_name, current.legal_name),
            (original.zoho_name, current.zoho_name),
            (original.crm_name or "", current.crm_name or ""),
        ):
            if old and new and old != new:
                pairs.append((old, new))

        # Free text refers to a company by whatever part of its name the writer
        # felt like using: a payment description says "Apex Founder Holdings -
        # advance" and a case note says "Distinct from Blue Harbour Logistics".
        # Matching only the four exact spellings left those partial forms behind,
        # so a generated dataset still mentioned Apex and Northstar. The
        # distinctive leading word is mapped too, longest match first.
        def head(value: str, words: int) -> str:
            return " ".join(value.split()[:words]) if value else ""

        for words in (2, 1):
            old_head = head(original.zoho_name, words)
            new_head = head(current.zoho_name, words)
            if len(old_head) >= 4 and new_head and old_head != new_head:
                pairs.append((old_head, new_head))
            # The shared agent is only ever named in prose by its first two words
            # ("the Global Pay agent account"), never by the full narration string.
            old_bank = head(original.bank_narration_name, words)
            new_bank = head(current.bank_narration_name, words)
            if len(old_bank) >= 4 and new_bank and old_bank != new_bank:
                pairs.append((old_bank, new_bank))

    # Longest first: "Blue Harbour Logistics" must be rewritten before "Blue".
    return sorted(set(pairs), key=lambda pair: -len(pair[0]))


def _translate(text: str) -> str:
    """Rewrite any template company name appearing inside free text.

    Case-insensitive, because the same company appears as "GLOBAL PAY SERVICES" in a
    bank narration and "the Global Pay agent account" in a note, and matching only
    the exact casings left half of them behind. The replacement follows the casing
    of what it replaced, so an upper-case statement line stays upper-case.
    """
    if not text:
        return text
    for old, new in _translations():
        def _cased(match: re.Match[str], replacement: str = new) -> str:
            found = match.group(0)
            if found.isupper():
                return replacement.upper()
            if found.islower():
                return replacement.lower()
            return replacement

        text = re.sub(re.escape(old), _cased, text, flags=re.IGNORECASE)
    return text


def bank_csv_rows() -> list[dict[str, str]]:
    """Bank statement rows as they would appear in an exported CSV."""
    running_balance = Decimal("2500000")
    rows: list[dict[str, str]] = []
    for spec in sorted(BANK, key=lambda item: item.day):
        if spec.direction == "credit":
            running_balance += spec.amount
        else:
            running_balance -= spec.amount
        rows.append(
            {
                "Date": spec.day.strftime("%d/%m/%Y"),
                "Value Date": (spec.day + timedelta(days=1)).strftime("%d/%m/%Y"),
                "Description": _translate(spec.narration),
                "Reference": spec.reference,
                "Debit": f"{spec.amount:.2f}" if spec.direction == "debit" else "",
                "Credit": f"{spec.amount:.2f}" if spec.direction == "credit" else "",
                "Balance": f"{running_balance:.2f}",
                "Account Number": MAIN_ACCOUNT,
            }
        )
    return rows


def hubspot_companies() -> list[dict[str, Any]]:
    """CRM records — a supporting identity signal only."""
    payloads = []
    for index, customer in enumerate(roster.CUSTOMERS, start=1):
        if not customer.crm_name:
            continue
        payloads.append(
            {
                "id": f"{7000000 + index}",
                "properties": {
                    "name": customer.crm_name,
                    "domain": customer.domain or "",
                    "city": customer.address.split(",")[-1].strip(),
                    "country": "India",
                    "hubspot_owner_id": "512345",
                    "lifecyclestage": "customer",
                },
                "createdAt": "2026-03-15T09:00:00Z",
                "updatedAt": "2026-06-01T09:00:00Z",
                "archived": False,
            }
        )
    return payloads


def expected_totals() -> dict[str, Any]:
    """Ground truth the tests assert against.

    Kept next to the data so a change to the dataset that silently alters the
    expected outcome fails a test rather than quietly moving the target.
    """
    return {
        "customers": len(roster.CUSTOMERS),
        "contacts": len(zoho_contacts()),
        "invoices": len(INVOICES),
        "credit_notes": len(zoho_credit_notes()),
        "payments": len(PAYMENTS),
        "refunds": len(REFUNDS),
        "disputes": len(razorpay_disputes()),
        "bank_transactions": len(BANK),
        "failed_payments": sum(1 for p in PAYMENTS if p.status == "failed"),
        "refunded_payments": sum(1 for p in PAYMENTS if p.refunded > 0),
        "void_invoices": sum(1 for i in INVOICES if i.status == "void"),
        "draft_invoices": sum(1 for i in INVOICES if i.status == "draft"),
        "overdue_invoices": sum(1 for i in INVOICES if i.status == "overdue"),
        "one_time_invoices": sum(1 for i in INVOICES if i.is_one_time),
        "circular_pairs": 2,
        "related_parties": sum(1 for c in roster.CUSTOMERS if c.related_party),
    }
