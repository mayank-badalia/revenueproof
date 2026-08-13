"""Identifier cleaning and deterministic exact matching — Feature 2, sub-feature 1.

The order of operations matters and is not negotiable: **exact identifiers first,
fuzzy second, semantic last**. core_resoruces.md rejects embeddings as an override
for verified identifiers, and idea_features.md §6.3 says to use fuzzy matching "only
when exact identifiers are unavailable."

The reason is asymmetric cost. A false merge combines two real customers into one,
which understates customer concentration and can hide a related party — the exact
things a reviewer is looking for. A missed match merely leaves work for a human. So
every rule here is built to be confident when it is confident and silent otherwise.

GSTIN gets special treatment because it embeds a PAN: characters 3–12 of a GSTIN are
the entity's PAN. Two GSTINs sharing a PAN are the *same legal entity* registered in
different states — a fact no name comparison could establish.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Indian and international legal-form suffixes. Stripped for comparison because
# "Northstar Technologies Private Limited" and "Northstar Tech" differ mostly by
# boilerplate that carries no identifying information.
LEGAL_SUFFIXES = (
    r"private\s+limited", r"pvt\.?\s*ltd\.?", r"pvt\.?", r"p\.?\s*ltd\.?",
    r"public\s+limited", r"limited", r"ltd\.?", r"llp", r"llc", r"l\.?l\.?c\.?",
    r"inc\.?", r"incorporated", r"corp\.?", r"corporation", r"company", r"co\.?",
    r"gmbh", r"s\.?a\.?", r"b\.?v\.?", r"pte\.?\s*ltd\.?", r"pte\.?",
    r"and\s+sons", r"& sons", r"enterprises", r"ventures",
)

# Words so common in company names that matching on them alone is meaningless.
# Kept in the canonical name but ignored when scoring token overlap.
LOW_INFORMATION_TOKENS = frozenset({
    "technologies", "technology", "tech", "solutions", "systems", "services",
    "software", "digital", "global", "india", "group", "holdings", "international",
    "consulting", "labs", "media", "networks", "industries", "trading",
})

# Free mail providers say nothing about corporate identity: two customers both
# using gmail.com are not related.
FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "rediffmail.com", "zoho.com", "mail.com", "yandex.com", "gmx.com",
})

_SUFFIX_PATTERN = re.compile(
    r"\b(" + "|".join(LEGAL_SUFFIXES) + r")\b\.?\s*$", re.IGNORECASE
)
_ALL_SUFFIXES = re.compile(r"\b(" + "|".join(LEGAL_SUFFIXES) + r")\b\.?", re.IGNORECASE)

# 2-digit state code, 10-char PAN, 1 entity number, 1 letter (usually Z), 1 checksum.
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[0-9A-Z]{1}[Z]{1}[0-9A-Z]{1}$")
PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def normalize_name(name: str | None) -> str:
    """Canonical comparison form: lowercase, no legal suffix, no punctuation."""
    if not name:
        return ""
    text = name.lower().strip()
    text = re.sub(r"[.,'\"()\[\]]", " ", text)
    text = re.sub(r"[&/]", " and ", text)
    # Strip suffixes repeatedly: "Foo Pvt Ltd" carries two.
    previous = None
    while previous != text:
        previous = text
        text = _SUFFIX_PATTERN.sub("", text).strip()
    text = _ALL_SUFFIXES.sub(" ", text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_tokens(name: str | None, *, drop_low_information: bool = True) -> frozenset[str]:
    """Distinctive tokens of a company name.

    Dropping low-information words is what keeps "Northstar Technologies" from
    looking similar to "Quantum Technologies" merely because both are technology
    companies.
    """
    tokens = {token for token in normalize_name(name).split() if len(token) > 1}
    if drop_low_information:
        distinctive = tokens - LOW_INFORMATION_TOKENS
        # If a name is *entirely* generic ("Global Solutions"), keep the generic
        # tokens rather than returning nothing to compare.
        return frozenset(distinctive or tokens)
    return frozenset(tokens)


def normalize_domain(value: str | None) -> str | None:
    """Registrable domain from a URL, email or bare domain. None if uninformative."""
    if not value:
        return None
    text = str(value).strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = text.split("/")[0].split(":")[0].strip()
    if not text or "." not in text:
        return None
    if text in FREE_EMAIL_DOMAINS:
        return None
    return text


def normalize_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    local, _, domain = str(value).strip().lower().partition("@")
    # Gmail-style dot and +tag normalisation, so a.b+x@ and ab@ compare equal.
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+")[0].replace(".", "")
    else:
        local = local.split("+")[0]
    return f"{local}@{domain}" if local and domain else None


def normalize_phone(value: str | None) -> str | None:
    """Last 10 digits — enough to compare Indian numbers across formats."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) < 10:
        return None
    return digits[-10:]


def normalize_tax_id(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^A-Z0-9]", "", str(value).strip().upper()) or None


def is_valid_gstin(value: str | None) -> bool:
    normalized = normalize_tax_id(value)
    return bool(normalized and GSTIN_PATTERN.match(normalized))


def pan_from_gstin(value: str | None) -> str | None:
    """Extract the embedded PAN (characters 3–12) from a GSTIN.

    Two GSTINs sharing a PAN belong to one legal entity registered in different
    states. This is the single strongest identity signal available in Indian data,
    and no name comparison could ever establish it.
    """
    normalized = normalize_tax_id(value)
    if not normalized or not GSTIN_PATTERN.match(normalized):
        return None
    candidate = normalized[2:12]
    return candidate if PAN_PATTERN.match(candidate) else None


def normalize_address(value: str | None) -> str:
    """Comparison form for an address: lowercase, expanded abbreviations, no noise."""
    if not value:
        return ""
    text = str(value).lower()
    replacements = {
        r"\broad\b": "rd", r"\bstreet\b": "st", r"\bavenue\b": "ave",
        r"\bfloor\b": "flr", r"\bbuilding\b": "bldg", r"\bblock\b": "blk",
        r"\bsector\b": "sec", r"\bplot\b": "plt", r"\bnagar\b": "ngr",
        r"\bopposite\b": "opp", r"\bnear\b": "nr",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def postal_code(value: str | None) -> str | None:
    """Indian PIN code — a cheap, high-value blocking key."""
    if not value:
        return None
    match = re.search(r"\b([1-9][0-9]{5})\b", str(value))
    return match.group(1) if match else None


@dataclass(frozen=True, slots=True)
class IdentityKeys:
    """Every comparable identifier extracted from one source record."""

    record_type: str
    record_id: str
    source_system: str
    display_name: str

    normalized_name: str = ""
    tokens: frozenset[str] = field(default_factory=frozenset)
    domains: frozenset[str] = field(default_factory=frozenset)
    emails: frozenset[str] = field(default_factory=frozenset)
    phones: frozenset[str] = field(default_factory=frozenset)
    tax_ids: frozenset[str] = field(default_factory=frozenset)
    pans: frozenset[str] = field(default_factory=frozenset)
    addresses: frozenset[str] = field(default_factory=frozenset)
    postal_codes: frozenset[str] = field(default_factory=frozenset)
    # Platform-native customer IDs, e.g. a Zoho contact_id referenced by an invoice.
    platform_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_strong_identifier(self) -> bool:
        """True when a deterministic match is even possible for this record."""
        return bool(self.pans or self.tax_ids or self.domains or self.emails)


def build_identity_keys(
    *,
    record_type: str,
    record_id: str,
    source_system: str,
    display_name: str,
    emails: list[str] | None = None,
    domains: list[str] | None = None,
    phones: list[str] | None = None,
    tax_ids: list[str] | None = None,
    addresses: list[str] | None = None,
    platform_ids: list[str] | None = None,
    website: str | None = None,
) -> IdentityKeys:
    """Extract and clean every identifier from one record."""
    clean_emails = {e for e in (normalize_email(x) for x in (emails or [])) if e}
    clean_tax = {t for t in (normalize_tax_id(x) for x in (tax_ids or [])) if t}
    clean_pans = {p for p in (pan_from_gstin(x) for x in clean_tax) if p}
    # A bare PAN may also be supplied directly rather than inside a GSTIN.
    clean_pans |= {t for t in clean_tax if PAN_PATTERN.match(t)}

    clean_domains = {d for d in (normalize_domain(x) for x in (domains or [])) if d}
    clean_domains |= {d for d in (normalize_domain(e) for e in clean_emails) if d}
    if website:
        website_domain = normalize_domain(website)
        if website_domain:
            clean_domains.add(website_domain)

    clean_addresses = {a for a in (normalize_address(x) for x in (addresses or [])) if a}
    codes = {c for c in (postal_code(x) for x in (addresses or [])) if c}

    return IdentityKeys(
        record_type=record_type,
        record_id=record_id,
        source_system=source_system,
        display_name=display_name,
        normalized_name=normalize_name(display_name),
        tokens=name_tokens(display_name),
        domains=frozenset(clean_domains),
        emails=frozenset(clean_emails),
        phones=frozenset(p for p in (normalize_phone(x) for x in (phones or [])) if p),
        tax_ids=frozenset(clean_tax),
        pans=frozenset(clean_pans),
        addresses=frozenset(clean_addresses),
        postal_codes=frozenset(codes),
        platform_ids=frozenset(str(p) for p in (platform_ids or []) if p),
    )


# ---------------------------------------------------------------------------
# Deterministic exact matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExactMatch:
    """A deterministic identity link, with the rule that produced it."""

    rule_id: str
    reason: str
    # Confidence is not a probability; it is how much this identifier constrains
    # identity. A shared PAN is close to conclusive; a shared address is not.
    confidence: float


def find_exact_match(left: IdentityKeys, right: IdentityKeys) -> ExactMatch | None:
    """Deterministic identity rules, strongest first. None means "no exact evidence".

    Returning None is a real answer: it routes the pair to probabilistic ranking
    rather than forcing a decision from insufficient evidence.
    """
    # 1. Shared PAN — the same legal entity, possibly across state registrations.
    shared_pan = left.pans & right.pans
    if shared_pan:
        return ExactMatch(
            "EXACT_PAN",
            f"Both records carry PAN {sorted(shared_pan)[0]}, which identifies a "
            f"single legal entity",
            0.99,
        )

    # 2. Identical GSTIN — same entity, same state registration.
    shared_gstin = left.tax_ids & right.tax_ids
    if shared_gstin:
        return ExactMatch(
            "EXACT_TAX_ID",
            f"Both records carry tax identifier {sorted(shared_gstin)[0]}",
            0.98,
        )

    # 3. Same platform customer ID — the provider itself says these are one customer.
    shared_platform = left.platform_ids & right.platform_ids
    if shared_platform and left.source_system == right.source_system:
        return ExactMatch(
            "EXACT_PLATFORM_ID",
            f"Both records reference {left.source_system} customer "
            f"{sorted(shared_platform)[0]}",
            0.97,
        )

    # 4. Identical corporate email address.
    shared_email = left.emails & right.emails
    if shared_email:
        corporate = [e for e in shared_email if normalize_domain(e)]
        if corporate:
            return ExactMatch(
                "EXACT_EMAIL",
                f"Both records use the email address {sorted(corporate)[0]}",
                0.94,
            )

    # 5. Shared corporate domain plus a compatible name.
    #    Domain alone is deliberately not sufficient: a parent and its subsidiary
    #    legitimately share a domain while being distinct customers, which is
    #    exactly the Meridian Holdings / Meridian Systems case in the dataset.
    shared_domain = left.domains & right.domains
    if shared_domain:
        # A conflicting tax registration overrides the domain evidence entirely.
        # "Meridian Holdings" and "Meridian Systems India" share meridiangroup.in
        # and the token "meridian", but hold different GSTINs — they are a parent
        # and its subsidiary, not one customer. Without this guard the group's
        # revenue would be consolidated and customer concentration understated.
        if _has_conflicting_tax_ids(left, right):
            return None
        overlap = left.tokens & right.tokens
        if overlap:
            return ExactMatch(
                "EXACT_DOMAIN_AND_NAME",
                f"Both records share the domain {sorted(shared_domain)[0]} and the "
                f"name token(s) {', '.join(sorted(overlap))}",
                0.92,
            )
        # Same domain, unrelated names → related entities, not the same customer.
        return None

    return None


def _has_conflicting_tax_ids(left: IdentityKeys, right: IdentityKeys) -> bool:
    """True when both sides carry tax IDs that disagree and share no PAN."""
    if not (left.tax_ids and right.tax_ids):
        return False
    if left.tax_ids & right.tax_ids:
        return False
    # A shared PAN across different GSTINs is one entity in two states, not a conflict.
    return not (left.pans & right.pans)
