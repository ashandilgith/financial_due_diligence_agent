"""Checks that gate the run.

Blockers mean the numbers cannot be trusted at all (an unbalanced journal, an
unmapped account): the run stops. Warnings are real findings a reviewer must
see (a month where book cash and bank cash disagree), but the run continues
and the warning goes on the databook's front page.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .models import BankLine, CheckResult, GLLine
from .money import fmt
from .proof_of_cash import CashMonth

BLOCKER, WARNING = "blocker", "warning"


def _sample(items: list[str], n: int = 5) -> str:
    more = f" (+{len(items) - n} more)" if len(items) > n else ""
    return ", ".join(items[:n]) + more


def journals_balance(gl: list[GLLine]) -> CheckResult:
    totals: dict[str, int] = defaultdict(int)
    for line in gl:
        totals[line.journal_id] += line.net
    bad = sorted(j for j, net in totals.items() if net != 0)
    detail = (f"all {len(totals)} journals balance" if not bad else
              f"{len(bad)} journal(s) where debits != credits: {_sample(bad)}")
    return CheckResult("Every journal balances", not bad, BLOCKER, detail)


def accounts_mapped(gl: list[GLLine], mapping: dict[str, str]) -> CheckResult:
    unmapped = sorted({l.account_code for l in gl if l.account_code not in mapping})
    detail = ("every account is mapped" if not unmapped else
              f"{len(unmapped)} unmapped account(s): {_sample(unmapped)}")
    return CheckResult("Every account is mapped", not unmapped, BLOCKER, detail)


def no_duplicate_lines(gl: list[GLLine]) -> CheckResult:
    key = lambda l: (l.date, l.account_code, l.debit, l.credit, l.description, l.counterparty)
    counts = Counter(key(l) for l in gl)
    dupes = [l for l in gl if counts[key(l)] > 1]
    detail = ("no duplicate lines" if not dupes else
              f"{len(dupes)} lines look duplicated, e.g. {_sample([str(l.source) for l in dupes])}")
    return CheckResult("No duplicate ledger lines", not dupes, WARNING, detail)


def bank_balance_continuity(bank: list[BankLine]) -> CheckResult:
    """If the statement has a running balance, each line must follow from the one before."""
    with_balance = [b for b in bank if b.balance is not None]
    if len(with_balance) < 2:
        return CheckResult("Bank running balance is continuous", True, WARNING,
                           "no running balance column to check")
    breaks = []
    for previous, current in zip(with_balance, with_balance[1:]):
        if previous.balance + current.amount != current.balance:
            breaks.append(str(current.source))
    detail = ("running balance is continuous" if not breaks else
              f"{len(breaks)} break(s) in the running balance at {_sample(breaks)}")
    return CheckResult("Bank running balance is continuous", not breaks, WARNING, detail)


def proof_of_cash_ties(months: list[CashMonth], tolerance: int) -> CheckResult:
    bad = [m for m in months if not m.ties]
    detail = (f"book and bank cash tie in all {len(months)} months (tolerance {fmt(tolerance)})" if not bad else
              f"{len(bad)} month(s) do not tie: " +
              _sample([f"{m.month} ({fmt(m.difference)})" for m in bad]))
    return CheckResult("Proof of cash ties every month", not bad, WARNING, detail)


def blocking_checks(gl: list[GLLine], mapping: dict[str, str]) -> list[CheckResult]:
    return [journals_balance(gl), accounts_mapped(gl, mapping)]
