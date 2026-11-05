"""Proof of cash: does the cash the books say moved match what the bank says moved?

For each month we compare
    book net  = debits - credits on cash accounts in the ledger
    bank net  = deposits - withdrawals on the bank statement
A gap means unrecorded transactions, timing differences, or worse.
Opening-balance journals are excluded: they are not cash movements.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import BankLine, GLLine
from .statements import month_of, month_range


@dataclass(frozen=True)
class CashMonth:
    month: str
    book_net: int
    bank_net: int
    difference: int  # bank - book
    ties: bool


def proof_of_cash(
    gl: list[GLLine], bank: list[BankLine], mapping: dict[str, str], tolerance: int
) -> list[CashMonth]:
    cash_accounts = {code for code, category in mapping.items() if category == "asset_cash"}
    book: dict[str, int] = {}
    bank_totals: dict[str, int] = {}

    for line in gl:
        if line.account_code in cash_accounts and line.entry_type != "opening":
            m = month_of(line.date)
            book[m] = book.get(m, 0) + line.net
    for line in bank:
        m = month_of(line.date)
        bank_totals[m] = bank_totals.get(m, 0) + line.amount

    present = set(book) | set(bank_totals)
    if not present:
        return []
    months = month_range(min(present), max(present))

    result = []
    for m in months:
        b, k = book.get(m, 0), bank_totals.get(m, 0)
        result.append(CashMonth(m, b, k, k - b, abs(k - b) <= tolerance))
    return result
