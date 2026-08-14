"""Normalization Agent — Feature 1, sub-feature 5.

Maps each provider's native payload onto RevenueProof's canonical schema.

**This is deterministic code, not an LLM.** core_resoruces.md is explicit that
structured-output extraction is "useful only for ambiguous semantic mapping; direct
provider fields should be mapped deterministically." Razorpay's `amount` is already
an integer in paise and Zoho's `total` is already a decimal; asking a model to
restate them would add a hallucination risk to a solved problem, and would make the
result non-reproducible between runs.

The only genuinely ambiguous judgement here is whether an invoice line is a one-time
fee. That is handled by an explicit, inspectable keyword rule which produces a
*hint*, never a conclusion — Feature 3 reads the actual contract, and the contract wins.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from app.core.crypto import account_fingerprint
from app.core.money import MoneyError, to_minor_units
from app.models.enums import (
    InvoiceStatus,
    PaymentStatus,
    SourceSystem,
    TransactionDirection,
)
from app.schemas.canonical import (
    CanonicalBankTransaction,
    CanonicalContractDocument,
    CanonicalCreditNote,
    CanonicalCrmAccount,
    CanonicalCustomer,
    CanonicalInvoice,
    CanonicalLineItem,
    CanonicalPayment,
    CanonicalRefund,
)


class NormalizationError(ValueError):
    """Raised when a payload cannot be mapped; the record is then quarantined."""


# ---------------------------------------------------------------------------
# One-time detection
# ---------------------------------------------------------------------------

# spec §14: "Setup and implementation line items are not recurring unless the
# contract explicitly makes them recurring." These words mark a line as *probably*
# non-recurring so Feature 5 knows to check the contract, rather than assuming.
_ONE_TIME_PATTERNS = re.compile(
    r"\b("
    r"set[\s-]?up|setup|onboard(?:ing)?|implementation|migration|installation|"
    r"training|consult(?:ing|ancy)?|professional\s+services|one[\s-]?time|"
    r"customi[sz]ation|integration\s+fee|configuration|deployment|"
    r"data\s+migration|kick[\s-]?off"
    r")\b",
    re.IGNORECASE,
)

# Words that mean recurring, checked first: "annual subscription setup" is a
# subscription, and a naive one-time match would misclassify it.
_RECURRING_PATTERNS = re.compile(
    r"\b(subscription|licen[sc]e|recurring|monthly|quarterly|annual(?:ly)?|"
    r"per\s+month|per\s+annum|retainer|maintenance|support\s+plan|saas)\b",
    re.IGNORECASE,
)


def classify_line_description(text: str | None) -> str:
    """Keyword hint for a line item: recurring | one_time | ambiguous | unknown.

    Three states rather than a boolean, because the interesting case is the third
    one. "Annual subscription — implementation and migration programme" contains
    both vocabularies, and that is exactly how a one-time fee gets presented as ARR
    (spec §19). Silently resolving it either way hides the problem: calling it
    recurring overstates ARR, calling it one-time understates it without evidence.

    `ambiguous` means "the contract must decide" — Feature 3 reads the actual clause
    and Feature 5 applies it. The hint's job is to make sure someone looks.
    """
    if not text:
        return "unknown"
    has_recurring = bool(_RECURRING_PATTERNS.search(text))
    has_one_time = bool(_ONE_TIME_PATTERNS.search(text))

    if has_recurring and has_one_time:
        return "ambiguous"
    if has_one_time:
        return "one_time"
    if has_recurring:
        return "recurring"
    return "unknown"


def looks_one_time(text: str | None) -> bool:
    """Boolean view of the hint. Ambiguous counts as one-time *for flagging only*.

    Used to set `Invoice.has_one_time_items`, which drives review routing rather
    than any financial figure — so erring toward "look at this" is the safe side.
    """
    return classify_line_description(text) in {"one_time", "ambiguous"}


# ---------------------------------------------------------------------------
# Shared coercion helpers
# ---------------------------------------------------------------------------


def _as_datetime(value: date | None) -> datetime | None:
    """A ledger records the day, not the second. Anchor it at midnight UTC so the
    reconciliation window compares like with like rather than local-midnight drift."""
    if value is None:
        return None
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _parse_date(value: Any, field: str) -> date | None:
    if value in (None, "", "0000-00-00"):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise NormalizationError(f"{field}: cannot parse date {value!r}") from exc


def _parse_unix(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (ValueError, TypeError, OSError, OverflowError) as exc:
        raise NormalizationError(f"{field}: cannot parse timestamp {value!r}") from exc


def _money(value: Any, currency: str, field: str) -> int:
    if value in (None, ""):
        return 0
    try:
        return to_minor_units(value, currency)
    except MoneyError as exc:
        raise NormalizationError(f"{field}: {exc}") from exc


def _currency(payload: dict[str, Any], *keys: str, default: str = "INR") -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value).strip().upper()
    return default


def _text(value: Any, limit: int = 300) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text[:limit] or None


# ---------------------------------------------------------------------------
# Razorpay
# ---------------------------------------------------------------------------

# Razorpay amounts are ALREADY in the smallest currency unit, so they are used
# directly. Passing them through the decimal converter would multiply by 100 again.
_RAZORPAY_STATUS = {
    "created": PaymentStatus.CREATED,
    "authorized": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "refunded": PaymentStatus.REFUNDED,
    "failed": PaymentStatus.FAILED,
}


def razorpay_payment(payload: dict[str, Any]) -> CanonicalPayment:
    source_id = _text(payload.get("id"), 300)
    if not source_id:
        raise NormalizationError("razorpay payment is missing its `id`")

    currency = _currency(payload, "currency")
    amount = int(payload.get("amount") or 0)
    refunded = int(payload.get("amount_refunded") or 0)
    status = _RAZORPAY_STATUS.get(
        str(payload.get("status", "")).lower(), PaymentStatus.UNKNOWN
    )
    # A partially refunded payment reports status "captured" with a non-zero
    # amount_refunded; without this the refund would be invisible to the classifier.
    if status is PaymentStatus.CAPTURED and 0 < refunded < amount:
        status = PaymentStatus.PARTIALLY_REFUNDED

    notes = payload.get("notes") or {}

    # A payment made through a payment link carries the merchant's own metadata in
    # `notes`, while `description` is overwritten with the link's receipt id
    # (`#TOUrqC3mDGotkV`). Reading the reference from `description` alone therefore
    # loses the invoice number on exactly the payments that have one — measured
    # against a real Razorpay test account, not assumed from the docs.
    reference = (
        _text(notes.get("invoice_number"))
        or _text(notes.get("invoice"))
        or _text(payload.get("order_id"))
    )

    unknown: list[str] = []
    if not payload.get("invoice_id"):
        unknown.append("invoice_id")
    if not payload.get("email"):
        unknown.append("email")

    return CanonicalPayment(
        source_system=SourceSystem.RAZORPAY,
        source_id=source_id,
        customer_source_id=_text(payload.get("customer_id")),
        customer_name=_text(notes.get("customer_name")),
        email=_text(payload.get("email"), 320),
        phone=_text(payload.get("contact"), 40),
        currency=currency,
        amount_minor=amount,
        fee_minor=int(payload.get("fee") or 0),
        tax_minor=int(payload.get("tax") or 0),
        amount_refunded_minor=refunded,
        status=status,
        payment_time=_parse_unix(payload.get("created_at"), "created_at"),
        method=_text(payload.get("method"), 40),
        description=_text(payload.get("description"), 2000),
        reference=reference,
        invoice_source_id=_text(payload.get("invoice_id")),
        unknown_fields=unknown,
    )


def razorpay_refund(payload: dict[str, Any]) -> CanonicalRefund:
    source_id = _text(payload.get("id"))
    if not source_id:
        raise NormalizationError("razorpay refund is missing its `id`")
    return CanonicalRefund(
        source_system=SourceSystem.RAZORPAY,
        source_id=source_id,
        payment_source_id=_text(payload.get("payment_id")),
        currency=_currency(payload, "currency"),
        amount_minor=int(payload.get("amount") or 0),
        status=str(payload.get("status") or "processed")[:30],
        refund_time=_parse_unix(payload.get("created_at"), "created_at"),
        reason=_text((payload.get("notes") or {}).get("reason"), 1000),
        is_chargeback=False,
    )


def razorpay_dispute(payload: dict[str, Any]) -> CanonicalRefund:
    """Disputes become refunds flagged as chargebacks.

    A chargeback removes cash exactly as a refund does; modelling it separately
    would mean every downstream calculation had to remember to subtract both.
    """
    source_id = _text(payload.get("id"))
    if not source_id:
        raise NormalizationError("razorpay dispute is missing its `id`")
    return CanonicalRefund(
        source_system=SourceSystem.RAZORPAY,
        source_id=source_id,
        payment_source_id=_text(payload.get("payment_id")),
        currency=_currency(payload, "currency"),
        amount_minor=int(payload.get("amount") or 0),
        status=str(payload.get("status") or "open")[:30],
        refund_time=_parse_unix(payload.get("created_at"), "created_at"),
        reason=_text(payload.get("reason_code"), 1000),
        is_chargeback=True,
    )


# ---------------------------------------------------------------------------
# Zoho Books
# ---------------------------------------------------------------------------

_ZOHO_INVOICE_STATUS = {
    "draft": InvoiceStatus.DRAFT,
    "sent": InvoiceStatus.SENT,
    "viewed": InvoiceStatus.SENT,
    "paid": InvoiceStatus.PAID,
    "partially_paid": InvoiceStatus.PARTIALLY_PAID,
    "overdue": InvoiceStatus.OVERDUE,
    "void": InvoiceStatus.VOID,
    "unpaid": InvoiceStatus.SENT,
}


def zoho_contact(payload: dict[str, Any]) -> CanonicalCustomer:
    source_id = _text(payload.get("contact_id"))
    if not source_id:
        raise NormalizationError("zoho contact is missing its `contact_id`")

    name = _text(payload.get("contact_name")) or _text(payload.get("company_name"))
    if not name:
        raise NormalizationError("zoho contact has no usable name")

    address_block = payload.get("billing_address") or {}
    tax_ids = [
        value
        for value in (payload.get("gst_no"), payload.get("vat_reg_no"), payload.get("pan_no"))
        if value
    ]
    unknown = [f for f in ("gst_no", "email") if not payload.get(f)]

    return CanonicalCustomer(
        source_system=SourceSystem.ZOHO_BOOKS,
        source_id=source_id,
        display_name=name,
        legal_name=_text(payload.get("company_name")),
        email=_text(payload.get("email"), 320),
        phone=_text(payload.get("phone"), 40),
        website=_text(payload.get("website")),
        tax_identifiers=[str(t).strip().upper() for t in tax_ids],
        billing_address=_text(address_block.get("address"), 500),
        country=_text(address_block.get("country"), 100),
        raw_attributes={"status": payload.get("status")},
        unknown_fields=unknown,
    )


def zoho_invoice(payload: dict[str, Any]) -> CanonicalInvoice:
    source_id = _text(payload.get("invoice_id"))
    if not source_id:
        raise NormalizationError("zoho invoice is missing its `invoice_id`")

    currency = _currency(payload, "currency_code")
    subtotal = _money(payload.get("sub_total"), currency, "sub_total")
    tax = _money(payload.get("tax_total"), currency, "tax_total")
    total = _money(payload.get("total"), currency, "total")
    balance = _money(payload.get("balance"), currency, "balance")

    line_items = []
    for raw in payload.get("line_items") or []:
        description = _text(raw.get("description"), 1000) or _text(raw.get("name"), 1000) or ""
        line_items.append(
            CanonicalLineItem(
                description=description,
                quantity=Decimal(str(raw.get("quantity", 1) or 1)),
                unit_amount_minor=_money(raw.get("rate"), currency, "rate"),
                total_minor=_money(raw.get("item_total"), currency, "item_total"),
                currency=currency,
                is_one_time_hint=looks_one_time(description),
                classification_hint=classify_line_description(description),
            )
        )

    status = _ZOHO_INVOICE_STATUS.get(
        str(payload.get("status", "")).lower(), InvoiceStatus.UNKNOWN
    )
    # A "paid" invoice with an outstanding balance is contradictory. Trust the
    # number over the label — a status flag can be set by hand, a balance cannot.
    if status is InvoiceStatus.PAID and balance > 0:
        status = InvoiceStatus.PARTIALLY_PAID

    return CanonicalInvoice(
        source_system=SourceSystem.ZOHO_BOOKS,
        source_id=source_id,
        invoice_number=_text(payload.get("invoice_number"), 120),
        customer_source_id=_text(payload.get("customer_id")),
        customer_name=_text(payload.get("customer_name")),
        issue_date=_parse_date(payload.get("date"), "date"),
        due_date=_parse_date(payload.get("due_date"), "due_date"),
        currency=currency,
        subtotal_minor=subtotal,
        tax_minor=tax,
        total_minor=total,
        amount_due_minor=balance,
        status=status,
        line_items=line_items,
        reference=_text(payload.get("reference_number")),
        notes=_text(payload.get("notes"), 2000),
        unknown_fields=[f for f in ("due_date", "reference_number") if not payload.get(f)],
    )


def zoho_payment(payload: dict[str, Any]) -> CanonicalPayment:
    """A customer payment recorded in the accounting system.

    The connector has always fetched these — `providers.py` lists `customerpayments`
    beside contacts, invoices and credit notes — but nothing was registered to read
    them, so every one was counted in `skipped_no_normalizer` and dropped. On a live
    organisation that is most of the cash: 45 payments against 55 invoices went in the
    bin, and the workspace concluded the company had collected 7.5% of what it billed.

    A payment here is not a processor event: there is no gateway fee, no capture, no
    settlement. It is the ledger's own record that money arrived, which is exactly the
    evidence Feature 4 needs to close an invoice.
    """
    source_id = _text(payload.get("payment_id"))
    if not source_id:
        raise NormalizationError("zoho payment is missing its `payment_id`")

    currency = _currency(payload, "currency_code")
    amount = _money(payload.get("amount"), currency, "amount")

    # Which invoices this payment settles. Zoho gives the applied amount per invoice;
    # a payment against exactly one invoice can name it, which is the strongest
    # reference Feature 4 can match on.
    applied = payload.get("invoices") or []
    invoice_source_id = (
        _text(applied[0].get("invoice_id")) if len(applied) == 1 else None
    )
    reference = (
        _text(payload.get("reference_number"))
        or _text(payload.get("invoice_numbers"))
        or (_text(applied[0].get("invoice_number")) if len(applied) == 1 else None)
    )

    return CanonicalPayment(
        source_system=SourceSystem.ZOHO_BOOKS,
        source_id=source_id,
        customer_source_id=_text(payload.get("customer_id")),
        customer_name=_text(payload.get("customer_name")),
        email=_text(payload.get("email"), 320),
        currency=currency,
        amount_minor=amount,
        # An accounting entry records what arrived. There is no processor between the
        # customer and the ledger, so nothing is withheld and both are zero — stated
        # rather than left to default, because Feature 4 compares net figures and a
        # guessed fee would move a reconciliation.
        fee_minor=0,
        tax_minor=0,
        amount_refunded_minor=0,
        status=PaymentStatus.CAPTURED,
        payment_time=_as_datetime(_parse_date(payload.get("date"), "date")),
        method=_text(payload.get("payment_mode"), 40),
        description=_text(payload.get("description"), 2000),
        reference=reference,
        invoice_source_id=invoice_source_id,
    )


def zoho_credit_note(payload: dict[str, Any]) -> CanonicalCreditNote:
    source_id = _text(payload.get("creditnote_id"))
    if not source_id:
        raise NormalizationError("zoho credit note is missing its `creditnote_id`")
    currency = _currency(payload, "currency_code")
    return CanonicalCreditNote(
        source_system=SourceSystem.ZOHO_BOOKS,
        source_id=source_id,
        credit_note_number=_text(payload.get("creditnote_number"), 120),
        customer_source_id=_text(payload.get("customer_id")),
        invoice_source_id=_text(payload.get("invoice_id")),
        issue_date=_parse_date(payload.get("date"), "date"),
        currency=currency,
        total_minor=_money(payload.get("total"), currency, "total"),
        reason=_text(payload.get("reason"), 1000),
    )


# ---------------------------------------------------------------------------
# Bank CSV
# ---------------------------------------------------------------------------

# Column aliases seen across Indian bank exports. Matching on a normalised header
# avoids failing on "Txn Date" vs "Transaction Date" vs "DATE".
_BANK_COLUMNS = {
    "date": {"date", "txndate", "transactiondate", "trandate", "postingdate"},
    "value_date": {"valuedate", "valuedt", "effectivedate"},
    "description": {"description", "narration", "particulars", "details", "remarks"},
    "reference": {"reference", "refno", "referenceno", "chequeno", "utrno", "chqrefno"},
    "debit": {"debit", "withdrawal", "withdrawalamt", "debitamount", "dr"},
    "credit": {"credit", "deposit", "depositamt", "creditamount", "cr"},
    "amount": {"amount", "txnamount", "transactionamount"},
    "balance": {"balance", "closingbalance", "runningbalance", "balanceamt"},
    "account": {"accountnumber", "accountno", "acno", "account"},
}


def normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def map_bank_columns(headers: list[str]) -> dict[str, str]:
    """Map a CSV's actual headers onto canonical field names."""
    mapping: dict[str, str] = {}
    for header in headers:
        key = normalize_header(header)
        for canonical, aliases in _BANK_COLUMNS.items():
            if key in aliases:
                mapping.setdefault(canonical, header)
                break
    return mapping


def bank_row(
    row: dict[str, Any],
    *,
    column_map: dict[str, str],
    workspace_id: str,
    row_number: int,
    default_account: str | None = None,
    currency: str = "INR",
) -> CanonicalBankTransaction:
    """Normalise one bank statement row.

    Handles both layouts: separate debit/credit columns (the Indian norm) and a
    single signed amount column.
    """

    def value(field: str) -> Any:
        column = column_map.get(field)
        return row.get(column) if column else None

    raw_date = value("date")
    if not raw_date:
        raise NormalizationError(f"row {row_number}: no transaction date column found")
    transaction_date = _parse_date(raw_date, "date")
    if transaction_date is None:
        raise NormalizationError(f"row {row_number}: empty transaction date")

    debit_raw = str(value("debit") or "").strip().replace(",", "")
    credit_raw = str(value("credit") or "").strip().replace(",", "")
    amount_raw = str(value("amount") or "").strip().replace(",", "")

    if credit_raw:
        direction = TransactionDirection.CREDIT
        amount_text = credit_raw
    elif debit_raw:
        direction = TransactionDirection.DEBIT
        amount_text = debit_raw
    elif amount_raw:
        # Single-column layout: the sign carries the direction.
        negative = amount_raw.startswith("-")
        direction = TransactionDirection.DEBIT if negative else TransactionDirection.CREDIT
        amount_text = amount_raw.lstrip("+-")
    else:
        raise NormalizationError(
            f"row {row_number}: no debit, credit or amount value present"
        )

    amount = _money(amount_text, currency, f"row {row_number} amount")
    if amount == 0:
        raise NormalizationError(f"row {row_number}: transaction amount is zero")

    account = str(value("account") or default_account or "unknown")
    balance_raw = str(value("balance") or "").strip().replace(",", "")

    return CanonicalBankTransaction(
        source_system=SourceSystem.BANK_CSV,
        # Row number keeps the identifier stable across re-imports of the same file
        # while remaining unique when a statement genuinely repeats a reference.
        source_id=f"bank_{transaction_date.isoformat()}_{row_number}",
        account_fingerprint=account_fingerprint(account, workspace_id),
        transaction_date=transaction_date,
        value_date=_parse_date(value("value_date"), "value_date")
        if value("value_date")
        else None,
        currency=currency,
        amount_minor=amount,
        direction=direction,
        counterparty=_extract_counterparty(_text(value("description"), 1000)),
        reference=_text(value("reference")),
        narration=_text(value("description"), 1000),
        balance_after_minor=_money(balance_raw, currency, "balance") if balance_raw else None,
    )


# Transport prefixes carry no identity information and would otherwise dominate
# every fuzzy name comparison in Feature 2.
_NARRATION_NOISE = re.compile(
    r"^(neft|rtgs|imps|upi|ach|ecs|chq|cash|tfr|trf)\s*(cr|dr)?\s*[-/:]?\s*",
    re.IGNORECASE,
)

# Payment processors and settlement agents appear in the narration but say nothing
# about who the customer is. Leaving them in would make every Razorpay-settled
# receipt look similar to every other one during Feature 2 matching.
_PROCESSOR_NOISE = re.compile(
    r"\b(razorpay|payu|cashfree|billdesk|ccavenue|paytm|phonepe|stripe|"
    r"instamojo|easebuzz)\b",
    re.IGNORECASE,
)


# Markers introducing a *reference code* rather than a party. The closing `\b`
# is load-bearing: without it `ref` matches the start of "REFINERY" and `ac` the
# start of "ACME", and the following `\S*` then eats the rest of that word — so a
# counterparty named ACME INDUSTRIES was stored as "INDUSTRIES" and Accenture
# Solutions as "Solutions". Every such customer lost the one token that identifies
# it, silently, on the way into the vault.
_REFERENCE_MARKER = re.compile(
    r"\b(?:ref|utr|rrn|neft ref|chq no)\b\.?\s*[:#-]?\s*\S*", re.IGNORECASE
)

# "X A/C Y" is account X paying on behalf of party Y. Both matter, but they are not
# interchangeable: X is the account the money actually left, which is what makes
# "several customers settle from one account" answerable.
_ACCOUNT_MARKER = re.compile(r"\s*\b(?:a/c|ac no|acct|account)\b\.?\s*", re.IGNORECASE)

# What is left of "TRF FROM" once the rail prefix goes: prepositions carry no
# identity and must not be mistaken for the paying party.
_CONNECTIVE_ONLY = re.compile(r"(?:\b(?:from|to|by|via|for|in|of)\b[\s:/-]*)*", re.IGNORECASE)

# Words describing what a transfer was *for*. They belong to the transaction, not
# to the party, and leaving them attached gives one counterparty as many identities
# as it has reasons to pay: APEX FOUNDER HOLDINGS arrived as "…PVT LTD ADVANCE"
# inbound and "…PVT LTD ADVISORY FEE" outbound, so a round trip stopped looking
# like a round trip. Only stripped at the ends of the name, never from the middle,
# so "Advance Auto Parts" keeps its first word.
_TRAILING_PURPOSE_TERMS = (
    "advisory fee", "intercompany reversal", "consulting retainer", "part payment",
    "intercompany", "chargeback", "reversal", "disbursement", "reimbursement",
    "retainer", "refund", "advance", "partial", "charges", "fees", "fee",
)
# Only the words no company opens its name with. "Advance Auto Parts" and "Fee
# Brothers" are real firms, and stripping a leading "advance" turned the first into
# "Auto Parts" — trading one wrong name for another. A trailing purpose word is
# safe because bank narrations put the reason after the party, not before it.
_LEADING_PURPOSE_TERMS = (
    "chargeback", "reversal", "refund", "disbursement", "reimbursement",
)


def _purpose_alt(terms: tuple[str, ...]) -> str:
    return "|".join(term.replace(" ", r"\s+") for term in terms)


_LEADING_PURPOSE = re.compile(
    rf"^(?:(?:{_purpose_alt(_LEADING_PURPOSE_TERMS)})\b[\s:/-]*)+", re.IGNORECASE
)
_TRAILING_PURPOSE = re.compile(
    rf"(?:[\s:/-]*\b(?:{_purpose_alt(_TRAILING_PURPOSE_TERMS)}))+\s*$", re.IGNORECASE
)


def _extract_counterparty(narration: str | None) -> str | None:
    """Best-effort counterparty name from a bank narration.

    A hint for Feature 2's matcher, not an identification. Bank narrations are
    truncated and inconsistent, so this can only ever narrow the search.

    What it must not do is give one party several names. Everything stripped here
    describes the *transfer* — its rail, its processor, its purpose, its reference —
    and every such fragment left attached splits one counterparty into as many
    counterparties as it has transactions, which inflates the customer count,
    understates concentration, and hides both shared accounts and circular flows.
    """
    if not narration:
        return None
    text = _NARRATION_NOISE.sub("", narration.strip())
    text = _PROCESSOR_NOISE.sub("", text)
    text = re.sub(r"\b(settlement|payment|transfer|credit|debit)\b", "", text, flags=re.I)
    text = _REFERENCE_MARKER.sub("", text)

    # An account marker splits payer from beneficiary. Keep the payer where there is
    # one; where the narration opens with the marker there is no payer named, so the
    # party after it — minus the account number — is the best available answer.
    parts = _ACCOUNT_MARKER.split(text, maxsplit=1)
    if len(parts) == 2:
        payer, beneficiary = parts[0].strip(), parts[1].strip()
        # "TRF FROM A/C 998877 JOHN DOE" names no payer — only a preposition left
        # over from the rail. Treat that as absent rather than as a company called
        # "From".
        if _CONNECTIVE_ONLY.fullmatch(payer):
            payer = ""
        text = payer or re.sub(r"^[\d\s*x-]+", "", beneficiary, flags=re.I)

    text = _TRAILING_REFERENCE.sub("", text)
    # Collapse before the anchored strips below: the processor and rail removals
    # leave leading whitespace, and `^` would then never match the purpose word
    # sitting behind it.
    text = re.sub(r"\s+", " ", text).strip(" -/:")
    for pattern in (_LEADING_PURPOSE, _TRAILING_PURPOSE):
        # A name made only of purpose words is not improved by deleting all of it;
        # keeping the fragment lets a human see what the bank actually wrote.
        stripped = pattern.sub("", text).strip(" -/:")
        if stripped:
            text = stripped
    text = re.sub(r"\s+", " ", text).strip(" -/:")
    return text[:300] or None


# Trailing transaction references: invoice numbers, month markers, sequence codes.
# These identify the *transaction*, not the customer. Leaving them in makes twelve
# monthly receipts from one customer look like twelve different counterparties —
# the customer never resolves, and the customer count silently inflates.
_TRAILING_REFERENCE = re.compile(
    r"("
    # INV-123, and the run of bare numbers a multi-invoice settlement trails
    # behind it ("INV090 091 092 093" is one reference list, not a company name).
    r"\s+(?:INV|INVOICE|BILL|ORD|ORDER|TXN|TRN|REF|SI|PO)[-_/ ]?\d+(?:\s+\d+)*"
    r"|\s+[MQ]\d{1,2}\b"                                              # M1, M12, Q3
    # Spelled-out sequence markers. "COBBLE INDUSTRIES INSTALMENT 1/2/3" is one
    # customer paying three times, and left in it becomes three customers — the same
    # inflation the M1..M12 case caused, written out in words.
    r"|\s+(?:INSTAL?LMENT|INSTAL|PART|TRANCHE|PAYMENT|MILESTONE|PHASE)"
    r"[-_/ ]?(?:\d{1,3}|[IVX]{1,4})\b"
    r"|\s+\d{4}[-/]\d{2}\b"                                           # 2026-04
    r"|\s+\d{6,}\b"                                                   # long numerics
    r"|\s+(?:FY)?\d{4}[-/]?\d{0,2}\b"                                 # FY2026, 2026-27
    r")+\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Google Drive / HubSpot
# ---------------------------------------------------------------------------


def drive_file(payload: dict[str, Any]) -> CanonicalContractDocument:
    source_id = _text(payload.get("id"), 300)
    if not source_id:
        raise NormalizationError("drive file is missing its `id`")
    modified = payload.get("modifiedTime")
    return CanonicalContractDocument(
        source_system=SourceSystem.GOOGLE_DRIVE,
        source_id=source_id,
        file_name=_text(payload.get("name"), 500) or source_id,
        mime_type=_text(payload.get("mimeType"), 120),
        size_bytes=int(payload["size"]) if payload.get("size") else None,
        modified_time=(
            datetime.fromisoformat(str(modified).replace("Z", "+00:00")) if modified else None
        ),
        web_link=_text(payload.get("webViewLink"), 1000),
        folder_path=_text(payload.get("folderPath"), 1000),
        unknown_fields=[f for f in ("size", "modifiedTime") if not payload.get(f)],
    )


def hubspot_company(payload: dict[str, Any]) -> CanonicalCrmAccount:
    source_id = _text(payload.get("id"), 300)
    if not source_id:
        raise NormalizationError("hubspot company is missing its `id`")
    properties = payload.get("properties") or {}
    name = _text(properties.get("name"))
    if not name:
        raise NormalizationError("hubspot company has no name property")
    return CanonicalCrmAccount(
        source_system=SourceSystem.HUBSPOT,
        source_id=source_id,
        name=name,
        domain=_text(properties.get("domain")),
        owner=_text(properties.get("hubspot_owner_id"), 200),
        lifecycle_stage=_text(properties.get("lifecyclestage"), 100),
        raw_attributes={
            "city": properties.get("city"),
            "country": properties.get("country"),
        },
    )
