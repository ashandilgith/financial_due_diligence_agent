"""The data shapes that flow through the pipeline.

All of them are frozen dataclasses: once a row is read it cannot be changed by
accident later in the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, order=True)
class Source:
    """Where a row came from: file name and the row number a person sees in Excel."""

    file: str
    row: int

    def __str__(self) -> str:
        return f"{self.file}:{self.row}"


@dataclass(frozen=True)
class GLLine:
    """One line of a general-ledger journal entry. Amounts are cents."""

    date: date
    journal_id: str
    account_code: str
    account_name: str
    description: str
    counterparty: str
    debit: int
    credit: int
    entry_type: str  # "auto", "manual" or "opening"
    source: Source

    @property
    def net(self) -> int:
        """Debit-positive amount: debits are +, credits are -."""
        return self.debit - self.credit


@dataclass(frozen=True)
class BankLine:
    """One bank statement line. amount > 0 is a deposit, < 0 a withdrawal."""

    date: date
    description: str
    amount: int
    balance: int | None
    source: Source


@dataclass(frozen=True)
class Candidate:
    """A possible EBITDA adjustment found by a rule. Never applied until a reviewer accepts it."""

    id: str
    rule: str
    title: str
    month: str
    date: date
    account_code: str
    account_name: str
    counterparty: str
    description: str
    amount_involved: int   # the size of the transaction(s), cents
    proposed_effect: int   # proposed change to EBITDA: + add back, - deduct
    rationale: str
    evidence: tuple[Source, ...]


@dataclass(frozen=True)
class Decision:
    """A reviewer's call on one candidate."""

    candidate_id: str
    accepted: bool
    amount_override: int | None
    note: str
    source: Source


@dataclass(frozen=True)
class ReviewedCandidate:
    candidate: Candidate
    status: str          # "accepted", "rejected" or "pending"
    final_effect: int    # what actually goes into adjusted EBITDA
    note: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    severity: str  # "blocker" stops the run; "warning" is reported
    detail: str
