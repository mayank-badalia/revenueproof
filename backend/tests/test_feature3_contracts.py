"""Feature 3 tests — Contract Revenue Intelligence Engine.

The feature's value rests on two things a summary cannot provide: a ₹10 lakh contract
correctly split into recurring and one-time parts, and a citation that survives being
re-checked against the original page. Both are tested directly.

Covers Step 2a categories 1, 2, 3, 4, 5, 6 and 11 (goal-fidelity).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.connectors.synthetic import contracts as synth
from app.features.contracts import extraction as extract
from app.features.contracts import parsing
from app.features.contracts import service
from app.features.contracts.extraction import CitedValue
from app.features.contracts.parsing import ParsedDocument, TextBlock


# ---------------------------------------------------------------------------
# 1. Safe intake (sub-feature 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "filename", "fragment"),
    [
        (b"", "c.pdf", "empty"),
        (b"not a pdf at all", "c.pdf", "not a PDF"),
        (b"\x89PNG\r\n\x1a\n", "c.pdf", "not a PDF"),
        (b"%PDF-1.7" + b"x" * (51 * 1024 * 1024), "c.pdf", "limit"),
    ],
)
def test_unsafe_documents_are_rejected(content, filename, fragment):
    with pytest.raises(parsing.DocumentError, match=fragment):
        parsing.check_document_safety(content, filename)


def test_valid_pdf_passes_safety():
    parsing.check_document_safety(synth.render_pdf(synth.CONTRACTS[0]), "c.pdf")


# ---------------------------------------------------------------------------
# 2. Classification and parsing (sub-features 2-3)
# ---------------------------------------------------------------------------


def test_digital_pdf_is_classified_as_digital():
    info = parsing.classify(synth.render_pdf(synth.BY_KEY["northstar_msa"]))
    assert info["is_scanned"] is False
    assert info["chars_per_page"] > parsing.MIN_CHARS_PER_PAGE_FOR_DIGITAL


def test_scanned_pdf_is_classified_as_scanned():
    """The image-only contract must not be read as an empty digital document."""
    info = parsing.classify(synth.render_pdf(synth.BY_KEY["silverline_scanned"]))
    assert info["is_scanned"] is True
    assert info["chars_per_page"] < 5


def test_native_parsing_preserves_pages_and_boxes():
    parsed = parsing.parse_native(synth.render_pdf(synth.BY_KEY["northstar_msa"]))
    assert parsed.page_count >= 1
    assert parsed.blocks
    for block in parsed.blocks:
        assert block.page >= 1
        assert block.bbox is not None and len(block.bbox) == 4
        assert block.source == "native"


def test_scanned_contract_routes_through_ocr_and_recovers_text():
    """Integration reality-check: OCR really runs and really returns text."""
    contract = synth.BY_KEY["silverline_scanned"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)

    assert parsed.ocr_applied is True
    assert parsed.is_usable, "OCR recovered too little text to extract from"
    assert "Silverline" in parsed.full_text or "SILVERLINE" in parsed.full_text.upper()
    assert parsed.ocr_confidence and parsed.ocr_confidence > 60
    # Coordinates survive OCR, in PDF points rather than pixels.
    ocr_blocks = [b for b in parsed.blocks if b.source == "ocr"]
    assert ocr_blocks
    assert all(b.bbox and b.bbox[2] < 1000 for b in ocr_blocks)


def test_digital_contract_skips_ocr():
    """OCR on a digital PDF is wasted time and a lossier result."""
    contract = synth.BY_KEY["northstar_msa"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    assert parsed.ocr_applied is False
    assert parsed.total_chars > 500


def test_every_synthetic_contract_parses_usably():
    for contract in synth.CONTRACTS:
        parsed = parsing.parse_document(
            synth.render_pdf(contract), contract.file_name
        )
        assert parsed.is_usable, f"{contract.file_name} produced unusable text"


# ---------------------------------------------------------------------------
# 3. Clause segmentation and retrieval (sub-feature 4)
# ---------------------------------------------------------------------------


def test_clauses_keep_their_page_number():
    contract = synth.BY_KEY["quantum_implementation"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    clauses = parsing.segment_clauses(parsed)
    assert clauses
    for clause in clauses:
        assert clause.page in parsed.page_text
        assert clause.text.strip()


def test_retrieval_finds_the_fee_clause():
    """The pricing passage must never be the one the retriever drops."""
    contract = synth.BY_KEY["quantum_implementation"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    clauses = parsing.segment_clauses(parsed)
    retrieved = extract.retrieve_clauses(clauses)

    combined = " ".join(clause.text.lower() for clause in retrieved)
    assert "fee" in combined
    assert "implementation" in combined


def test_retrieval_is_deterministic():
    contract = synth.BY_KEY["northstar_msa"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    clauses = parsing.segment_clauses(parsed)
    first = [c.index for c in extract.retrieve_clauses(clauses)]
    second = [c.index for c in extract.retrieve_clauses(clauses)]
    assert first == second


# ---------------------------------------------------------------------------
# 4. Deterministic amount parsing (sub-feature 6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600000", 60_000_000),
        ("6,00,000", 60_000_000),        # Indian grouping
        ("600,000", 60_000_000),         # Western grouping, same value
        ("15 lakh", 150_000_000),
        ("15,00,000", 150_000_000),
        ("1 crore", 1_000_000_000),
        ("1.5 crore", 1_500_000_000),
        ("INR 6,00,000", 60_000_000),
        ("₹75,000", 7_500_000),
        (None, None),
        ("", None),
        ("not a number", None),
    ],
)
def test_amount_parsing_handles_indian_notation(raw, expected):
    assert extract.parse_amount(raw, "INR") == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-04-01", date(2026, 4, 1)),
        ("1 April 2026", date(2026, 4, 1)),
        ("01/04/2026", date(2026, 4, 1)),
        (None, None),
        ("sometime next year", None),
    ],
)
def test_date_parsing(raw, expected):
    assert extract.parse_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("annual", "annual"),
        ("Annually", "annual"),
        ("per annum", "annual"),
        ("MONTHLY", "monthly"),
        ("per month", "monthly"),
        ("quarterly", "quarterly"),
        ("whenever", "unknown"),
        (None, "unknown"),
    ],
)
def test_frequency_parsing(raw, expected):
    assert extract.parse_frequency(raw) == expected


# ---------------------------------------------------------------------------
# 5. Period allocation (sub-feature 6) — the rule that stops ARR inflation
# ---------------------------------------------------------------------------


PERIOD = {"period_start": date(2026, 4, 1), "period_end": date(2027, 3, 31)}


def test_contract_fully_inside_the_period_allocates_in_full():
    result = service.allocate_to_period(
        recurring_minor=320_000_000, one_time_minor=40_000_000, frequency="annual",
        contract_start=date(2026, 4, 1), contract_end=date(2027, 3, 31),
        currency="INR", **PERIOD,
    )
    assert result["in_period_minor"] == 360_000_000  # recurring + one-time
    assert result["future_period_minor"] == 0
    assert result["detail"]["rule"] == "FULLY_IN_PERIOD"


def test_future_contract_allocates_nothing_to_the_current_period():
    """spec §14 and §19: a future contract must not be counted in current revenue."""
    result = service.allocate_to_period(
        recurring_minor=240_000_000, one_time_minor=0, frequency="annual",
        contract_start=date(2027, 4, 1), contract_end=date(2028, 3, 31),
        currency="INR", **PERIOD,
    )
    assert result["in_period_minor"] == 0
    assert result["future_period_minor"] > 0
    assert result["detail"]["rule"] == "OUTSIDE_PERIOD_FUTURE"


def test_contract_straddling_the_period_end_is_prorated_by_days():
    result = service.allocate_to_period(
        recurring_minor=120_000_000, one_time_minor=0, frequency="annual",
        contract_start=date(2026, 10, 1), contract_end=date(2027, 9, 30),
        currency="INR", **PERIOD,
    )
    assert 0 < result["in_period_minor"] < 120_000_000
    assert result["detail"]["rule"] == "DAY_PRORATED"
    assert "/" in result["detail"]["day_ratio"]


def test_monthly_contract_annualises_correctly():
    result = service.allocate_to_period(
        recurring_minor=7_500_000, one_time_minor=0, frequency="monthly",
        contract_start=date(2026, 4, 1), contract_end=date(2027, 3, 31),
        currency="INR", **PERIOD,
    )
    # ₹75,000/month × 12 = ₹9,00,000 annualised.
    assert result["annualised_recurring_minor"] == 90_000_000
    assert result["in_period_minor"] == 90_000_000


def test_unknown_dates_allocate_nothing():
    """Missing dates must not silently become a full-period allocation."""
    result = service.allocate_to_period(
        recurring_minor=100_000_000, one_time_minor=0, frequency="annual",
        contract_start=None, contract_end=None, currency="INR", **PERIOD,
    )
    assert result["in_period_minor"] == 0
    assert result["detail"]["rule"] == "DATES_UNKNOWN"


def test_one_time_fee_lands_in_the_period_it_becomes_payable():
    """A setup fee is not spread across the term."""
    inside = service.allocate_to_period(
        recurring_minor=0, one_time_minor=150_000_000, frequency="annual",
        contract_start=date(2026, 6, 1), contract_end=date(2027, 5, 31),
        currency="INR", **PERIOD,
    )
    assert inside["in_period_minor"] == 150_000_000

    outside = service.allocate_to_period(
        recurring_minor=0, one_time_minor=150_000_000, frequency="annual",
        contract_start=date(2025, 6, 1), contract_end=date(2026, 5, 31),
        currency="INR", **PERIOD,
    )
    # The fee became payable before the period began.
    assert outside["in_period_minor"] == 0


def test_non_recurring_frequency_annualises_to_zero():
    assert service.annualised_recurring(100_000_000, "one_time", "INR") == 0
    assert service.annualised_recurring(100_000_000, "unknown", "INR") == 0
    assert service.annualised_recurring(100_000_000, "usage_based", "INR") == 0


# ---------------------------------------------------------------------------
# 6. Citation validation (sub-feature 8) — the check that makes citations evidence
# ---------------------------------------------------------------------------


def _document(text: str, page: int = 1) -> ParsedDocument:
    return ParsedDocument(
        page_count=1,
        page_text={page: text},
        blocks=[TextBlock(page=page, text=text, bbox=(0, 0, 100, 20), end=len(text))],
    )


def test_a_genuine_quote_verifies():
    doc = _document("The Subscription Fee shall be INR 6,00,000 per annum.")
    validated = extract.validate_citation(
        doc, "recurring_amount",
        CitedValue(value="600000", page=1,
                   quote="The Subscription Fee shall be INR 6,00,000 per annum."),
    )
    assert validated is not None and validated.verified is True
    assert validated.span_start is not None


def test_a_fabricated_quote_is_rejected():
    """The failure this whole sub-feature exists to catch."""
    doc = _document("The Subscription Fee shall be INR 6,00,000 per annum.")
    validated = extract.validate_citation(
        doc, "recurring_amount",
        CitedValue(value="9900000", page=1,
                   quote="The Subscription Fee shall be INR 99,00,000 per annum."),
    )
    assert validated is not None
    assert validated.verified is False
    assert "does not appear" in validated.note


def test_a_quote_on_the_wrong_page_is_corrected_not_discarded():
    """A wrong page number with a real quote is a different failure from invention."""
    doc = ParsedDocument(
        page_count=2,
        page_text={1: "Preamble and definitions.",
                   2: "The Subscription Fee shall be INR 6,00,000 per annum."},
    )
    validated = extract.validate_citation(
        doc, "recurring_amount",
        CitedValue(value="600000", page=1,
                   quote="The Subscription Fee shall be INR 6,00,000 per annum."),
    )
    assert validated is not None
    assert validated.verified is True
    assert validated.page == 2
    assert "not the cited page" in validated.note


def test_citation_matching_tolerates_ocr_spacing():
    """OCR introduces spacing and punctuation noise; that must not fail a real quote."""
    doc = _document("The  Subscription   Fee shall be INR 6,00,000 per annum .")
    validated = extract.validate_citation(
        doc, "recurring_amount",
        CitedValue(value="600000", page=1,
                   quote="The Subscription Fee shall be INR 6,00,000 per annum."),
    )
    assert validated is not None and validated.verified is True


def test_missing_page_or_quote_yields_no_citation():
    doc = _document("Some text")
    assert extract.validate_citation(doc, "f", CitedValue(value="1", page=None)) is None
    assert extract.validate_citation(doc, "f", CitedValue(value="1", page=1)) is None


def test_a_trivially_short_quote_cannot_verify():
    doc = _document("The fee is INR 100.")
    validated = extract.validate_citation(
        doc, "recurring_amount", CitedValue(value="100", page=1, quote="fee")
    )
    assert validated is not None and validated.verified is False


# ---------------------------------------------------------------------------
# 7. Amendment precedence (sub-feature 7)
# ---------------------------------------------------------------------------


class _FakeContract:
    """Minimal stand-in so precedence logic can be tested without the database."""

    def __init__(self, name, customer, start, is_amendment, recurring, effective=None):
        self.id = uuid.uuid4()
        self.document_name = name
        self.customer_entity_id = customer
        self.start_date = start
        self.is_amendment = is_amendment
        self.recurring_amount = recurring
        self.effective_from = effective


def test_amendment_is_linked_to_the_agreement_it_modifies():
    customer = uuid.uuid4()
    original = _FakeContract(
        "Ironbridge_Agreement.pdf", customer, date(2026, 4, 1), False, 80_000_000
    )
    amendment = _FakeContract(
        "Ironbridge_Amendment_1.pdf", customer, date(2026, 10, 1), True, 100_000_000,
        effective=date(2026, 10, 1),
    )
    resolutions = service.resolve_amendment_chain([original, amendment])

    assert len(resolutions) == 1
    assert resolutions[0]["supersedes_id"] == str(original.id)
    assert resolutions[0]["previous_recurring_minor"] == 80_000_000
    assert resolutions[0]["new_recurring_minor"] == 100_000_000
    assert "1 October" in resolutions[0]["explanation"] or "2026-10-01" in str(
        resolutions[0]["effective_from"]
    )


def test_amendments_do_not_link_across_customers():
    original = _FakeContract("A.pdf", uuid.uuid4(), date(2026, 4, 1), False, 100)
    amendment = _FakeContract("B_Amendment.pdf", uuid.uuid4(), date(2026, 10, 1), True, 200)
    assert service.resolve_amendment_chain([original, amendment]) == []


def test_an_amendment_with_no_prior_agreement_is_not_linked():
    customer = uuid.uuid4()
    amendment = _FakeContract(
        "Only_Amendment.pdf", customer, date(2026, 10, 1), True, 100,
        effective=date(2026, 10, 1),
    )
    assert service.resolve_amendment_chain([amendment]) == []


# ---------------------------------------------------------------------------
# 8. Extraction guardrails (sub-feature 5)
# ---------------------------------------------------------------------------


async def test_unusable_document_is_reviewed_not_extracted():
    """An unreadable scan must not become a contract worth zero (spec §18)."""
    empty = ParsedDocument(page_count=1, page_text={1: "x"})
    result = await extract.extract_terms(
        empty, [], filename="broken.pdf", workspace_id="w1"
    )
    assert result.terms is None
    assert result.needs_review is True
    assert any("not usable" in reason for reason in result.review_reasons)


async def test_extraction_without_a_model_routes_to_review(monkeypatch):
    from app.core import llm

    monkeypatch.setattr(llm, "is_available", lambda: False)
    contract = synth.BY_KEY["northstar_msa"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)

    result = await extract.extract_terms(
        parsed, parsing.segment_clauses(parsed),
        filename=contract.file_name, workspace_id="w1",
    )
    assert result.terms is None
    assert result.needs_review is True


def test_extraction_prompt_wraps_the_contract_as_untrusted():
    """A contract is attacker-controllable text (OWASP LLM01)."""
    contract = synth.BY_KEY["northstar_msa"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    prompt = extract.build_extraction_prompt(
        parsing.segment_clauses(parsed), contract.file_name
    )
    assert 'untrusted="true"' in prompt
    assert "must be ignored" in prompt


def test_extraction_schema_allows_unknown_everywhere():
    """Every field must be omissible: an absent term is null, never a default."""
    terms = extract.ContractTerms()
    for field_name in (
        "customer_legal_name", "contract_start", "recurring_amount",
        "one_time_amount", "auto_renewal",
    ):
        assert getattr(terms, field_name).value is None


# ---------------------------------------------------------------------------
# 9. Goal-fidelity — the dataset's adversarial contracts
# ---------------------------------------------------------------------------


def test_ground_truth_separates_recurring_from_implementation():
    """The Quantum case: ₹18L presented as ARR is really ₹3L recurring + ₹15L one-off."""
    contract = synth.BY_KEY["quantum_implementation"]
    assert contract.recurring_amount == Decimal("300000")
    assert contract.one_time_amount == Decimal("1500000")

    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    text = parsed.full_text.lower()
    # The contract states the fee is non-recurring; the extractor must be able to see it.
    assert "non-recurring" in text
    assert "implementation fee" in text


def test_ambiguous_contract_contains_contradictory_prices():
    """Vertex states three inconsistent prices; the correct output is review."""
    contract = synth.BY_KEY["vertex_ambiguous"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    text = parsed.full_text

    assert "6,00,000" in text
    assert "50,000" in text
    assert "7,20,000" in text


def test_future_contract_text_states_its_commencement():
    contract = synth.BY_KEY["meridian_future"]
    parsed = parsing.parse_document(synth.render_pdf(contract), contract.file_name)
    assert "1 April 2027" in parsed.full_text
    assert contract.start_date > date(2027, 3, 31)


# ---------------------------------------------------------------------------
# 10. Structured-output shape tolerance
# ---------------------------------------------------------------------------
#
# These pin a real performance defect. Structured output drifts in predictable
# ways — a cited field arriving as a bare scalar, a bare flag arriving wrapped to
# match its neighbours. Rejecting those cost a full retry per contract, and the
# retry carried the failed exchange in its prompt, roughly doubling token use
# against a per-minute budget. It was the single largest cause of slow runs.


def test_bare_scalar_is_accepted_where_a_cited_value_is_expected():
    terms = extract.ContractTerms.model_validate(
        {"auto_renewal": True, "termination_notice_days": 90, "currency": "INR"}
    )
    assert terms.auto_renewal.value == "true"
    assert terms.termination_notice_days.value == "90"
    assert terms.currency.value == "INR"
    # No citation was offered, so none is claimed.
    assert terms.auto_renewal.page is None


def test_wrapped_flag_is_unwrapped():
    terms = extract.ContractTerms.model_validate(
        {"has_contradiction": {"value": True}, "is_amendment": {"value": False}}
    )
    assert terms.has_contradiction is True
    assert terms.is_amendment is False


def test_string_page_number_is_coerced():
    terms = extract.ContractTerms.model_validate(
        {"recurring_amount": {"value": "600000", "page": "2", "quote": "INR 6,00,000"}}
    )
    assert terms.recurring_amount.page == 2


def test_unexpected_keys_do_not_discard_a_valid_extraction():
    terms = extract.ContractTerms.model_validate(
        {"currency": {"value": "INR"}, "confidence_note": "high", "extra": [1, 2]}
    )
    assert terms.currency.value == "INR"


def test_an_overlong_quote_is_truncated_not_rejected():
    terms = extract.ContractTerms.model_validate(
        {"refund_terms": {"value": "yes", "page": 1, "quote": "x" * 900}}
    )
    assert terms.refund_terms.quote is not None
    assert len(terms.refund_terms.quote) == 400


def test_nulls_survive_normalisation():
    """An absent term must stay absent, never become a default."""
    terms = extract.ContractTerms.model_validate(
        {"auto_renewal": None, "recurring_amount": {"value": None}}
    )
    assert terms.auto_renewal.value is None
    assert terms.recurring_amount.value is None
