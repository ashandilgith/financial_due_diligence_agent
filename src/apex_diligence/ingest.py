"""Reading client files.

Three promises this module keeps:
1. Every row remembers where it came from (file + Excel row number).
2. Bad data is never guessed at. Every problem is collected and reported
   together, with row numbers, so the client can fix the file in one pass.
3. CSV and Excel files are handled the same way.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path

import openpyxl

from .models import BankLine, Decision, GLLine, Source
from .money import AmountError, to_cents
from .taxonomy import CATEGORIES

MAX_PROBLEMS_SHOWN = 20


class IngestError(Exception):
    """A file could not be read. Carries every problem found, not just the first."""

    def __init__(self, file: str, problems: list[str]):
        self.file = file
        self.problems = problems
        shown = "\n".join(f"  - {p}" for p in problems[:MAX_PROBLEMS_SHOWN])
        more = len(problems) - MAX_PROBLEMS_SHOWN
        tail = f"\n  ... and {more} more" if more > 0 else ""
        super().__init__(f"{file}: {len(problems)} problem(s)\n{shown}{tail}")


# Column aliases: the first name is the one we use inside the code.
GL_COLUMNS = {
    "date": ["date", "posting_date", "transaction_date"],
    "journal_id": ["journal_id", "journal", "je", "entry_id", "reference"],
    "account_code": ["account_code", "account", "acct", "account_no", "account_number", "gl_code"],
    "account_name": ["account_name", "account_description"],
    "description": ["description", "memo", "narration", "details"],
    "counterparty": ["counterparty", "vendor", "customer", "payee", "name"],
    "debit": ["debit", "dr", "debit_amount"],
    "credit": ["credit", "cr", "credit_amount"],
    "entry_type": ["entry_type", "type", "source"],
}
GL_REQUIRED = ["date", "journal_id", "account_code", "debit", "credit"]

BANK_COLUMNS = {
    "date": ["date", "transaction_date", "value_date", "posting_date"],
    "description": ["description", "details", "narration", "memo", "particulars"],
    "amount": ["amount", "net_amount"],
    "deposit": ["deposit", "deposits", "credit", "cr"],
    "withdrawal": ["withdrawal", "withdrawals", "debit", "dr"],
    "balance": ["balance", "running_balance"],
}

MAPPING_COLUMNS = {
    "account_code": GL_COLUMNS["account_code"],
    "account_name": GL_COLUMNS["account_name"],
    "category": ["category", "mapping", "taxonomy"],
}

DECISION_COLUMNS = {
    "candidate_id": ["candidate_id", "id"],
    "decision": ["decision"],
    "amount_override": ["amount_override", "override"],
    "note": ["note", "notes", "comment"],
}

ENTRY_TYPES = {"auto", "manual", "opening"}


# ---------------------------------------------------------------- low level

def read_rows(path: str | Path) -> tuple[list[str], list[tuple[int, dict]]]:
    """Read a .csv or .xlsx file into (headers, [(excel_row_number, {header: value})]).

    Headers are lower-cased with spaces turned into underscores.
    Fully blank rows are skipped. Row numbers match what a person sees in Excel.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if not path.exists():
        raise IngestError(path.name, ["file not found"])

    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            raw = [row for row in csv.reader(f)]
    elif suffix in (".xlsx", ".xlsm"):
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            raw = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        finally:
            wb.close()
    else:
        raise IngestError(path.name, [f"unsupported file type '{suffix}' (use .csv or .xlsx)"])

    if not raw:
        raise IngestError(path.name, ["file is empty"])

    headers = [_norm_header(h) for h in raw[0]]
    seen = [h for h in headers if h]
    dupes = sorted({h for h in seen if seen.count(h) > 1})
    if dupes:
        raise IngestError(path.name, [f"duplicate column name(s): {', '.join(dupes)}"])

    rows = []
    for number, values in enumerate(raw[1:], start=2):
        if all(_text(v) == "" for v in values):
            continue
        values = list(values) + [None] * (len(headers) - len(values))
        rows.append((number, {h: v for h, v in zip(headers, values) if h}))
    return headers, rows


def resolve_columns(file: str, headers: list[str], spec: dict, required: list[str]) -> dict[str, str]:
    """Match our column names to the file's, using the aliases in `spec`."""
    found = {}
    for name, aliases in spec.items():
        for alias in aliases:
            if alias in headers:
                found[name] = alias
                break
    missing = [name for name in required if name not in found]
    if missing:
        raise IngestError(file, [
            f"missing required column(s): {', '.join(missing)}; "
            f"found: {', '.join(h for h in headers if h)}"
        ])
    return found


def _norm_header(value: object) -> str:
    return re.sub(r"\s+", "_", _text(value).lower())


def _text(value: object) -> str:
    """Cell value -> clean string. 1000.0 from Excel becomes "1000"."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


_ISO = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})(?:[ T].*)?$")


def parse_date(value: object) -> date:
    """Accept real dates (from Excel) or YYYY-MM-DD text. Refuse anything ambiguous.

    "03/04/2025" could be 3 April or 4 March. Guessing would silently move
    transactions between months, so we refuse and ask for ISO dates.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    match = _ISO.match(text)
    if not match:
        raise ValueError(f"bad date {text!r} (use YYYY-MM-DD)")
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise ValueError(f"impossible date {text!r}") from None


def _cents(row: dict, column: str | None, label: str, problems: list[str], number: int) -> int:
    if column is None:
        return 0
    try:
        return to_cents(row.get(column))
    except AmountError as e:
        problems.append(f"row {number}: {label}: {e}")
        return 0


# ---------------------------------------------------------------- loaders

def load_gl(path: str | Path) -> list[GLLine]:
    path = Path(path)
    headers, rows = read_rows(path)
    col = resolve_columns(path.name, headers, GL_COLUMNS, GL_REQUIRED)
    problems: list[str] = []
    lines: list[GLLine] = []

    for number, row in rows:
        start = len(problems)
        try:
            when = parse_date(row.get(col["date"]))
        except ValueError as e:
            problems.append(f"row {number}: {e}")
            when = None
        journal = _text(row.get(col["journal_id"]))
        account = _text(row.get(col["account_code"]))
        if not journal:
            problems.append(f"row {number}: journal_id is blank")
        if not account:
            problems.append(f"row {number}: account_code is blank")

        debit = _cents(row, col["debit"], "debit", problems, number)
        credit = _cents(row, col["credit"], "credit", problems, number)
        if debit < 0 or credit < 0:
            problems.append(f"row {number}: negative debit or credit (put it in the other column)")
        if debit and credit:
            problems.append(f"row {number}: both debit and credit are filled")

        entry_type = _text(row.get(col.get("entry_type", ""), "")).lower() or "auto"
        if entry_type not in ENTRY_TYPES:
            problems.append(f"row {number}: entry_type {entry_type!r} must be one of {sorted(ENTRY_TYPES)}")

        if len(problems) == start:
            lines.append(GLLine(
                date=when,
                journal_id=journal,
                account_code=account,
                account_name=_text(row.get(col.get("account_name", ""), "")),
                description=_text(row.get(col.get("description", ""), "")),
                counterparty=_text(row.get(col.get("counterparty", ""), "")),
                debit=debit,
                credit=credit,
                entry_type=entry_type,
                source=Source(path.name, number),
            ))

    if not rows:
        problems.append("no data rows")
    if problems:
        raise IngestError(path.name, problems)
    return lines


def load_bank(path: str | Path) -> list[BankLine]:
    """Bank statements come either with one signed 'amount' column or with
    separate 'deposit' and 'withdrawal' columns. Both are supported."""
    path = Path(path)
    headers, rows = read_rows(path)
    col = resolve_columns(path.name, headers, BANK_COLUMNS, ["date"])
    has_amount = "amount" in col
    has_split = "deposit" in col and "withdrawal" in col
    if not (has_amount or has_split):
        raise IngestError(path.name, ["needs an 'amount' column, or 'deposit' and 'withdrawal' columns"])

    problems: list[str] = []
    lines: list[BankLine] = []
    for number, row in rows:
        start = len(problems)
        try:
            when = parse_date(row.get(col["date"]))
        except ValueError as e:
            problems.append(f"row {number}: {e}")
            when = None
        if has_amount:
            amount = _cents(row, col["amount"], "amount", problems, number)
        else:
            deposit = _cents(row, col["deposit"], "deposit", problems, number)
            withdrawal = _cents(row, col["withdrawal"], "withdrawal", problems, number)
            amount = deposit - abs(withdrawal)
        balance = None
        if "balance" in col and _text(row.get(col["balance"])) != "":
            balance = _cents(row, col["balance"], "balance", problems, number)
        if len(problems) == start:
            lines.append(BankLine(
                date=when,
                description=_text(row.get(col.get("description", ""), "")),
                amount=amount,
                balance=balance,
                source=Source(path.name, number),
            ))

    if not rows:
        problems.append("no data rows")
    if problems:
        raise IngestError(path.name, problems)
    return lines


def load_mapping(path: str | Path) -> dict[str, str]:
    """account_code -> taxonomy category."""
    path = Path(path)
    headers, rows = read_rows(path)
    col = resolve_columns(path.name, headers, MAPPING_COLUMNS, ["account_code", "category"])
    problems: list[str] = []
    mapping: dict[str, str] = {}
    for number, row in rows:
        code = _text(row.get(col["account_code"]))
        category = _text(row.get(col["category"])).lower()
        if not code:
            problems.append(f"row {number}: account_code is blank")
            continue
        if category not in CATEGORIES:
            problems.append(f"row {number}: unknown category {category!r} for account {code}")
            continue
        if code in mapping and mapping[code] != category:
            problems.append(f"row {number}: account {code} mapped twice ({mapping[code]} and {category})")
            continue
        mapping[code] = category
    if problems:
        raise IngestError(path.name, problems)
    return mapping


def load_decisions(path: str | Path) -> dict[str, Decision]:
    """Reviewer decisions. Rows with a blank decision are still pending and ignored."""
    path = Path(path)
    headers, rows = read_rows(path)
    col = resolve_columns(path.name, headers, DECISION_COLUMNS, ["candidate_id", "decision"])
    problems: list[str] = []
    decisions: dict[str, Decision] = {}
    for number, row in rows:
        cid = _text(row.get(col["candidate_id"]))
        verdict = _text(row.get(col["decision"])).lower()
        if not verdict:
            continue
        if verdict not in ("accept", "reject"):
            problems.append(f"row {number}: decision must be 'accept', 'reject' or blank, not {verdict!r}")
            continue
        if cid in decisions:
            problems.append(f"row {number}: candidate {cid} decided twice")
            continue
        override = None
        if "amount_override" in col and _text(row.get(col["amount_override"])) != "":
            override = _cents(row, col["amount_override"], "amount_override", problems, number)
        decisions[cid] = Decision(
            candidate_id=cid,
            accepted=verdict == "accept",
            amount_override=override,
            note=_text(row.get(col.get("note", ""), "")),
            source=Source(path.name, number),
        )
    if problems:
        raise IngestError(path.name, problems)
    return decisions
