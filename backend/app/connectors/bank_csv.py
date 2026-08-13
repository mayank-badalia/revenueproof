"""Bank CSV adapter — Feature 1, sub-feature 4.

spec §16 makes this an intentionally deferred dependency: Account Aggregator
production access needs partner onboarding that cannot be obtained during a build,
so the CSV importer defines the canonical bank contract and an AA connector can
later populate the same `CanonicalBankTransaction` records without anything
downstream changing.

Safety is treated as part of the feature, not a wrapper around it. A bank statement
uploader is an unauthenticated-ish file ingestion path into a system holding
financial data, so the OWASP File Upload checks run *before* any parsing:

* size cap enforced on the actual bytes, not a client-declared length;
* declared extension and content sniffing must agree;
* the file must decode as text and contain no NUL bytes;
* CSV formula injection is neutralised on read;
* a row cap prevents a decompression/size bomb from exhausting memory.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any

from app.connectors.normalize import (
    NormalizationError,
    bank_row,
    map_bank_columns,
)
from app.connectors.synthetic import transactions as synthetic_txn
from app.core.events import EventKind, Severity, emit
from app.schemas.canonical import CanonicalBankTransaction

MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB — far beyond any real statement export
MAX_ROWS = 100_000
ALLOWED_EXTENSIONS = {".csv", ".txt"}
# Excel treats a leading =, +, -, @ as a formula. A statement narration beginning
# with one becomes code when the reviewer opens an export in a spreadsheet.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class BankCsvError(ValueError):
    """The upload was rejected before parsing."""


@dataclass
class ImportResult:
    transactions: list[CanonicalBankTransaction] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    detected_columns: dict[str, str] = field(default_factory=dict)
    total_rows: int = 0
    account_numbers: set[str] = field(default_factory=set)

    @property
    def accepted(self) -> int:
        return len(self.transactions)


def check_upload_safety(content: bytes, filename: str) -> None:
    """OWASP file-upload checks. Raises `BankCsvError` on anything suspicious."""
    if not content:
        raise BankCsvError("the uploaded file is empty")

    if len(content) > MAX_FILE_BYTES:
        raise BankCsvError(
            f"file is {len(content) / 1_048_576:.1f} MB; the limit is "
            f"{MAX_FILE_BYTES // 1_048_576} MB"
        )

    lowered = filename.lower()
    if not any(lowered.endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise BankCsvError(
            f"only {', '.join(sorted(ALLOWED_EXTENSIONS))} files are accepted, got {filename!r}"
        )

    # Content sniffing: a renamed binary must not reach the parser.
    signatures = {
        b"%PDF": "PDF",
        b"PK\x03\x04": "ZIP/XLSX",
        b"\xd0\xcf\x11\xe0": "legacy Excel",
        b"\x89PNG": "PNG",
        b"\xff\xd8\xff": "JPEG",
        b"\x7fELF": "ELF executable",
        b"MZ": "Windows executable",
    }
    for magic, label in signatures.items():
        if content.startswith(magic):
            raise BankCsvError(
                f"file content is a {label}, not CSV text, despite its {filename!r} name"
            )

    if b"\x00" in content[:8192]:
        raise BankCsvError("file contains NUL bytes and is not valid CSV text")


def decode(content: bytes) -> str:
    """Decode bytes to text, tolerating the encodings Indian banks actually emit."""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BankCsvError("file could not be decoded as text in any supported encoding")


# A plain number, optionally signed, with optional thousands separators. Checked
# before the formula guard because "-225000.00" is a debit amount, not a formula.
_NUMERIC_CELL = re.compile(r"^[+-]?[\d,]*\.?\d+$")


def sanitize_cell(value: str | None) -> str:
    """Neutralise spreadsheet formula injection while preserving the visible text.

    Numeric cells are exempt. A bank statement using a single signed amount column
    writes debits as "-225000.00"; quoting those would make every debit unparseable
    and silently drop half the statement — a far worse outcome than the injection
    risk, and one that would only surface as a wrong revenue figure.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if _NUMERIC_CELL.match(text):
        return text
    if text.startswith(FORMULA_PREFIXES):
        # Prefix with a quote so Excel renders it literally instead of evaluating it.
        return "'" + text
    return text


def _find_header_row(rows: list[list[str]]) -> int:
    """Locate the real header among a bank's preamble rows.

    Statement exports routinely begin with account-holder blurb before the actual
    column headings, so taking row 0 blindly would misparse most real files.
    """
    best_index, best_score = 0, -1
    for index, row in enumerate(rows[:25]):
        mapping = map_bank_columns(row)
        score = len(mapping)
        # A usable header must at least locate a date and one amount column.
        if "date" in mapping and (
            "credit" in mapping or "debit" in mapping or "amount" in mapping
        ):
            score += 5
        if score > best_score:
            best_index, best_score = index, score
    if best_score < 5:
        raise BankCsvError(
            "could not find a header row containing a date column and a "
            "debit/credit/amount column"
        )
    return best_index


def import_csv(
    content: bytes,
    filename: str,
    *,
    workspace_id: str,
    currency: str = "INR",
    default_account: str | None = None,
) -> ImportResult:
    """Validate and parse a bank statement CSV into canonical transactions.

    A malformed row is rejected individually and reported; it does not abort the
    import. A statement with three bad rows out of four hundred should yield 397
    usable transactions plus three visible quarantine entries.
    """
    check_upload_safety(content, filename)
    text = decode(content)

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # single-column or unusual file; comma is the safe default

    raw_rows = list(csv.reader(io.StringIO(text), dialect))
    if not raw_rows:
        raise BankCsvError("no rows found in the file")

    header_index = _find_header_row(raw_rows)
    headers = [sanitize_cell(cell) for cell in raw_rows[header_index]]
    column_map = map_bank_columns(headers)

    result = ImportResult(detected_columns=column_map)
    emit(
        EventKind.RULE,
        f"Bank CSV: header found on line {header_index + 1}; "
        f"mapped {len(column_map)} columns",
        workspace_id=workspace_id,
        severity=Severity.INFO,
        feature=1,
        columns=column_map,
    )

    for offset, raw in enumerate(raw_rows[header_index + 1 :], start=1):
        if result.total_rows >= MAX_ROWS:
            result.rejected.append(
                {"row": offset, "error": f"row limit of {MAX_ROWS} reached", "data": {}}
            )
            break
        if not any(str(cell).strip() for cell in raw):
            continue  # blank separator line

        result.total_rows += 1
        row = {
            headers[i]: sanitize_cell(raw[i]) if i < len(raw) else ""
            for i in range(len(headers))
        }

        try:
            transaction = bank_row(
                row,
                column_map=column_map,
                workspace_id=workspace_id,
                row_number=offset,
                default_account=default_account,
                currency=currency,
            )
        except (NormalizationError, ValueError) as exc:
            result.rejected.append({"row": offset, "error": str(exc), "data": row})
            continue

        account_column = column_map.get("account")
        if account_column and row.get(account_column):
            result.account_numbers.add(row[account_column])
        result.transactions.append(transaction)

    emit(
        EventKind.RESULT,
        f"Bank CSV: {result.accepted} transactions accepted, "
        f"{len(result.rejected)} rows rejected",
        workspace_id=workspace_id,
        severity=Severity.SUCCESS if result.accepted else Severity.WARNING,
        feature=1,
        total_rows=result.total_rows,
        accounts=len(result.account_numbers),
    )
    return result


def synthetic_csv_bytes() -> bytes:
    """The §15 bank statement, rendered as a real CSV file.

    Produced as bytes rather than objects so the synthetic path exercises the same
    upload validation, header detection and row parsing as a genuine upload.
    """
    rows = synthetic_txn.bank_csv_rows()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    # A realistic preamble, so header detection is exercised rather than assumed.
    buffer.write("Statement of Account\n")
    buffer.write("Account Holder: Northstar Diligence Demo Private Limited\n")
    buffer.write("Period: 01/04/2026 to 31/03/2027\n")
    buffer.write("\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def csv_template() -> bytes:
    """Downloadable template showing the expected columns."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Date", "Value Date", "Description", "Reference", "Debit", "Credit",
         "Balance", "Account Number"]
    )
    writer.writerow(
        ["01/04/2026", "02/04/2026", "NEFT CR ACME CORP INV-001", "NEFT123456",
         "", "118000.00", "1118000.00", "50100234567890"]
    )
    writer.writerow(
        ["05/04/2026", "05/04/2026", "OFFICE RENT APRIL", "RENT0425",
         "225000.00", "", "893000.00", "50100234567890"]
    )
    return buffer.getvalue().encode("utf-8")
