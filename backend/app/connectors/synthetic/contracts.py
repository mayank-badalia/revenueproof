"""Synthetic contracts — spec §15, rendered as real PDFs.

These are generated as genuine multi-page PDFs with PyMuPDF rather than plain text,
because Feature 3's whole difficulty is page-level citation: extracting a value *and*
proving which page and span it came from. A text fixture would let the extractor
appear to work while the citation machinery went untested.

The set deliberately includes the cases §15 and §19 call for:
  * monthly, quarterly and annual subscriptions
  * setup/implementation fees that must NOT be counted as recurring
  * a contract whose term begins in a future period
  * an ambiguous contract with contradictory pricing that must go to human review
  * an amendment that supersedes an earlier price mid-period
  * a scanned (image-only) contract that forces the OCR path
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.connectors.synthetic import customers as roster


@dataclass(frozen=True, slots=True)
class SyntheticContract:
    key: str
    customer_key: str
    file_name: str
    start_date: date
    end_date: date
    billing_frequency: str
    # Ground truth, in major units. Tests assert the extractor recovers these.
    recurring_amount: Decimal
    one_time_amount: Decimal
    currency: str = "INR"
    auto_renewal: bool = True
    termination_notice_days: int = 30
    # Rendering controls
    is_scanned: bool = False           # forces the OCR path
    is_ambiguous: bool = False         # contradictory clauses → human review
    is_amendment: bool = False
    supersedes: str | None = None
    extra_clauses: list[str] = field(default_factory=list)
    notes: str = ""


CONTRACTS: list[SyntheticContract] = [
    # 1. Largest customer: annual subscription + implementation fee.
    SyntheticContract(
        key="northstar_msa",
        customer_key="northstar",
        file_name="Northstar_Technologies_MSA_2026.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("3200000"),
        one_time_amount=Decimal("400000"),
        extra_clauses=[
            "Customer may terminate for convenience on ninety (90) days written notice.",
            "Fees are exclusive of applicable GST.",
        ],
        termination_notice_days=90,
        notes="Clean, large, fully paid. Should verify as recurring + one-time.",
    ),
    # 2. Clean monthly subscription — the control case.
    SyntheticContract(
        key="lumen_subscription",
        customer_key="lumen_software",
        file_name="Lumen_Software_Subscription_Agreement.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="monthly",
        recurring_amount=Decimal("75000"),
        one_time_amount=Decimal("0"),
        notes="₹75,000/month for 12 months = ₹9,00,000 annual recurring.",
    ),
    # 3. Clean annual.
    SyntheticContract(
        key="kestrel_annual",
        customer_key="kestrel_logistics",
        file_name="Kestrel_Logistics_Annual_Agreement.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("1200000"),
        one_time_amount=Decimal("150000"),
        notes="Annual subscription with a modest onboarding fee.",
    ),
    # 4. Quarterly.
    SyntheticContract(
        key="terrace_quarterly",
        customer_key="terrace_ventures",
        file_name="Terrace_Ventures_Services_Agreement.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="quarterly",
        recurring_amount=Decimal("225000"),
        one_time_amount=Decimal("0"),
        notes="₹2,25,000/quarter = ₹9,00,000 annual recurring.",
    ),
    # 5. THE ADVERSARIAL ONE: implementation work dressed as subscription.
    SyntheticContract(
        key="quantum_implementation",
        customer_key="quantum_retail",
        file_name="Quantum_Retail_Implementation_SOW.pdf",
        start_date=date(2026, 5, 1),
        end_date=date(2027, 4, 30),
        billing_frequency="annual",
        recurring_amount=Decimal("300000"),
        one_time_amount=Decimal("1500000"),
        extra_clauses=[
            "The Implementation Fee of INR 15,00,000 covers a one-time data migration, "
            "configuration and training programme, and is non-recurring and "
            "non-refundable once the migration milestone is accepted.",
            "The Implementation Fee shall not renew and is payable once only.",
        ],
        notes=(
            "Company reports the full ₹18,00,000 as ARR. Only ₹3,00,000 is genuinely "
            "recurring — the contract says so explicitly."
        ),
    ),
    # 6. Future-period contract — must not support current-period revenue (§14).
    SyntheticContract(
        key="meridian_future",
        customer_key="meridian_systems",
        file_name="Meridian_Systems_Agreement_FY2028.pdf",
        start_date=date(2027, 4, 1),
        end_date=date(2028, 3, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("2400000"),
        one_time_amount=Decimal("0"),
        extra_clauses=[
            "This Agreement shall come into force on 1 April 2027 and no fees are "
            "payable in respect of any period prior to the Commencement Date.",
        ],
        notes="Signed early. Zero of this belongs in FY2026-27.",
    ),
    # 7. Ambiguous — contradictory pricing, must route to human review.
    SyntheticContract(
        key="vertex_ambiguous",
        customer_key="vertex_labs",
        file_name="Vertex_Labs_Agreement_Amended.pdf",
        start_date=date(2026, 6, 1),
        end_date=date(2027, 5, 31),
        billing_frequency="unknown",
        recurring_amount=Decimal("600000"),
        one_time_amount=Decimal("0"),
        is_ambiguous=True,
        extra_clauses=[
            "Clause 4.1: The Subscription Fee shall be INR 6,00,000 per annum.",
            "Clause 9.3: Notwithstanding Clause 4.1, the Subscription Fee shall be "
            "INR 50,000 per month, invoiced monthly in arrears.",
            "Schedule A states an annual fee of INR 7,20,000.",
        ],
        notes=(
            "Three mutually inconsistent price statements and no precedence clause. "
            "The correct behaviour is HUMAN_REVIEW, not a confident guess."
        ),
    ),
    # 8. Scanned contract — forces the OCR path.
    SyntheticContract(
        key="silverline_scanned",
        customer_key="silverline",
        file_name="Silverline_Education_Agreement_Scanned.pdf",
        start_date=date(2026, 4, 15),
        end_date=date(2027, 4, 14),
        billing_frequency="annual",
        recurring_amount=Decimal("900000"),
        one_time_amount=Decimal("100000"),
        is_scanned=True,
        notes="Image-only PDF. Native text extraction must fail over to OCR.",
    ),
    # 9. Original contract, later amended mid-period.
    SyntheticContract(
        key="ironbridge_original",
        customer_key="ironbridge",
        file_name="Ironbridge_Manufacturing_Agreement.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("800000"),
        one_time_amount=Decimal("0"),
        notes="Superseded from 1 Oct 2026 by the amendment below.",
    ),
    # 10. The amendment — price change mid-period (§18).
    SyntheticContract(
        key="ironbridge_amendment",
        customer_key="ironbridge",
        file_name="Ironbridge_Manufacturing_Amendment_1.pdf",
        start_date=date(2026, 10, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("1000000"),
        one_time_amount=Decimal("0"),
        is_amendment=True,
        supersedes="ironbridge_original",
        extra_clauses=[
            "This Amendment No. 1 amends the Agreement dated 1 April 2026.",
            "With effect from 1 October 2026, the Annual Subscription Fee is "
            "increased from INR 8,00,000 to INR 10,00,000.",
            "All other terms of the Agreement remain unchanged and in full force.",
        ],
        notes="Feature 3 must apply precedence: ₹8L before 1 Oct, ₹10L after.",
    ),
    # 11. Contracted but never invoiced.
    SyntheticContract(
        key="orchid_contract",
        customer_key="orchid_hospitality",
        file_name="Orchid_Hospitality_Agreement.pdf",
        start_date=date(2026, 8, 1),
        end_date=date(2027, 7, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("800000"),
        one_time_amount=Decimal("0"),
        notes="No invoice, no payment → CONTRACTED_UNPAID, never counted as cash.",
    ),
    # 12. Refund clause exercised in practice.
    SyntheticContract(
        key="cobalt_agreement",
        customer_key="cobalt_media",
        file_name="Cobalt_Media_Agreement.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("600000"),
        one_time_amount=Decimal("0"),
        extra_clauses=[
            "Customer may cancel within thirty (30) days of the Effective Date for a "
            "full refund of all fees paid.",
        ],
        notes="Cancelled inside the window and fully refunded.",
    ),
    # 13-14. Remaining ordinary contracts.
    SyntheticContract(
        key="blue_harbor_agreement",
        customer_key="blue_harbor",
        file_name="Blue_Harbor_Analytics_Agreement.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="monthly",
        recurring_amount=Decimal("50000"),
        one_time_amount=Decimal("0"),
        notes="₹50,000/month = ₹6,00,000 annual recurring.",
    ),
    SyntheticContract(
        key="halcyon_agreement",
        customer_key="halcyon_health",
        file_name="Halcyon_Health_Agreement.pdf",
        start_date=date(2026, 4, 1),
        end_date=date(2027, 3, 31),
        billing_frequency="annual",
        recurring_amount=Decimal("500000"),
        one_time_amount=Decimal("0"),
        notes="Later hit by a chargeback.",
    ),
]

BY_KEY: dict[str, SyntheticContract] = {c.key: c for c in CONTRACTS}


def for_customer(customer_key: str) -> list[SyntheticContract]:
    return [c for c in CONTRACTS if c.customer_key == customer_key]


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

_INDIAN_GROUPS = (3, 2, 2, 2, 2, 2, 2)


def format_inr(amount: Decimal) -> str:
    """Format in the Indian numbering system: 1500000 → '15,00,000'."""
    digits = f"{int(amount):d}"
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def _clause_lines(contract: SyntheticContract) -> list[tuple[str, str]]:
    """(heading, body) pairs making up the contract body."""
    customer = roster.get(contract.customer_key)
    freq_label = {
        "monthly": "per month",
        "quarterly": "per quarter",
        "annual": "per annum",
        "unknown": "as set out below",
    }.get(contract.billing_frequency, "per annum")

    sections: list[tuple[str, str]] = [
        (
            "1. PARTIES",
            f"This Agreement is made between Northstar Diligence Demo Private Limited "
            f'("Service Provider") and {contract.customer_key and customer.legal_name} '
            f'("Customer"), having its registered office at {customer.address}, '
            f"GSTIN {customer.gstin or 'not provided'}.",
        ),
        (
            "2. TERM",
            f"This Agreement commences on {contract.start_date.strftime('%d %B %Y')} "
            f"(the \"Effective Date\") and continues until "
            f"{contract.end_date.strftime('%d %B %Y')}, unless terminated earlier in "
            f"accordance with Clause 6.",
        ),
    ]

    if not contract.is_ambiguous:
        fee_body = (
            f"The Customer shall pay a Subscription Fee of INR "
            f"{format_inr(contract.recurring_amount)} {freq_label}, invoiced "
            f"{contract.billing_frequency}."
        )
        if contract.one_time_amount > 0:
            fee_body += (
                f" In addition, a one-time Setup and Implementation Fee of INR "
                f"{format_inr(contract.one_time_amount)} is payable within thirty (30) "
                f"days of the Effective Date. The Setup and Implementation Fee is "
                f"non-recurring."
            )
        sections.append(("3. FEES", fee_body))
    else:
        sections.append(
            (
                "3. FEES",
                "The fees payable under this Agreement are set out in Clause 4.1, "
                "Clause 9.3 and Schedule A.",
            )
        )

    sections.append(
        (
            "4. PAYMENT TERMS",
            "All invoices are payable within thirty (30) days of the invoice date. "
            "Amounts are exclusive of GST, which shall be charged at the prevailing rate.",
        )
    )
    sections.append(
        (
            "5. RENEWAL",
            (
                "This Agreement shall renew automatically for successive periods of "
                "equal length unless either party gives written notice of non-renewal."
                if contract.auto_renewal
                else "This Agreement shall not renew automatically and shall expire at "
                "the end of the Term."
            ),
        )
    )
    sections.append(
        (
            "6. TERMINATION",
            f"Either party may terminate this Agreement on "
            f"{contract.termination_notice_days} days' prior written notice.",
        )
    )

    for index, clause in enumerate(contract.extra_clauses, start=7):
        sections.append((f"{index}. ADDITIONAL TERMS", clause))

    return sections


# Keyed by contract.key rather than the dataclass itself: `extra_clauses` is a
# list, which makes the frozen dataclass unhashable.
#
# **And by the roster the contract names.** Keying on the contract alone meant the
# first roster to render won: every later generated dataset was served the *template*
# company's PDFs, so its contracts said "Blue Harbor Analytics Private Limited" while
# its invoices said "Marrow Ventures". No invoice could then find its contract, so
# nothing was ever classified recurring and supported ARR read zero on every
# generated dataset — while the screen explained it as "no contract establishes a
# recurring charge", which was true of the document and false of the company.
_PDF_CACHE: dict[tuple[str, str], bytes] = {}


def _cache_key(contract: SyntheticContract) -> tuple[str, str]:
    """Identify the render by the contract *and* the party it names."""
    customer = roster.get(contract.customer_key)
    return (contract.key, customer.legal_name)


def render_pdf(contract: SyntheticContract) -> bytes:
    """Render one contract as a real PDF.

    Scanned contracts are rasterised so that no text layer remains — this is what
    forces Feature 3 to detect an image-only document and route it to OCR rather
    than silently extracting nothing.

    Cached because PyMuPDF writes a fresh random document ID on every save, so two
    renders of the same contract produce different bytes. The vault correctly reads
    that as "the source file changed" and cuts a new version — right behaviour for a
    real Drive file, but pure noise from a fixture. Caching also avoids re-rasterising
    the 2 MB scanned contract on every fetch.
    """
    cache_key = _cache_key(contract)
    cached = _PDF_CACHE.get(cache_key)
    if cached is not None:
        return cached

    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    customer = roster.get(contract.customer_key)

    margin, y, width = 60, 70, 475
    title = "CONTRACT AMENDMENT" if contract.is_amendment else "MASTER SERVICES AGREEMENT"
    page.insert_text((margin, y), title, fontsize=15, fontname="Helvetica-Bold")
    y += 24
    page.insert_text(
        (margin, y), customer.legal_name, fontsize=11, fontname="Helvetica-Bold"
    )
    y += 30

    for heading, body in _clause_lines(contract):
        if y > 720:
            page = document.new_page()
            y = 70
        page.insert_text((margin, y), heading, fontsize=10, fontname="Helvetica-Bold")
        y += 15
        used = page.insert_textbox(
            pymupdf.Rect(margin, y, margin + width, y + 130),
            body,
            fontsize=9.5,
            fontname="Helvetica",
            align=0,
        )
        # insert_textbox returns remaining vertical space when it fits, negative
        # when it overflows; either way advance past the text we just drew.
        y += (130 - used) if used >= 0 else 130
        y += 14

    if y > 690:
        page = document.new_page()
        y = 70
    page.insert_text((margin, y + 16), "SIGNED for and on behalf of the parties:",
                     fontsize=9, fontname="Helvetica")
    page.insert_text((margin, y + 44), "_______________________        _______________________",
                     fontsize=9, fontname="Helvetica")
    page.insert_text((margin, y + 58), "Service Provider                       Customer",
                     fontsize=8, fontname="Helvetica")

    if contract.is_scanned:
        rasterised = pymupdf.open()
        for source_page in document:
            # Greyscale at 150 dpi: what a real office scanner produces, legible to
            # OCR, and roughly a third the size of the equivalent colour raster.
            pixmap = source_page.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
            image_page = rasterised.new_page(
                width=source_page.rect.width, height=source_page.rect.height
            )
            image_page.insert_image(source_page.rect, pixmap=pixmap)
        data = rasterised.tobytes()
        rasterised.close()
        document.close()
        _PDF_CACHE[cache_key] = data
        return data

    data = document.tobytes()
    document.close()
    _PDF_CACHE[cache_key] = data
    return data
