"""A workspace: one company's validated data plus its computed run.

Every model and tool works from a Workspace, so they all see exactly the same
numbers as the databook.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .money import Money
from .pipeline import Loaded, RunInputs, RunResult, compute, load
from .statements import Statements


class ModelError(ValueError):
    """A model cannot run with these inputs. The message says why and what to change."""


@dataclass(frozen=True)
class BaseFigures:
    """Last-twelve-month figures and closing balances that models start from."""

    months: list[str]
    revenue: Money
    reported_ebitda: Money
    adjusted_ebitda: Money
    depreciation: Money
    interest: Money
    tax: Money
    nwc: Money
    debt: Money
    cash: Money
    pending_candidates: int


@dataclass
class Workspace:
    inputs: RunInputs
    data: Loaded
    result: RunResult
    out_dir: Path

    @property
    def statements(self) -> Statements:
        return self.result.statements

    @property
    def months(self) -> list[str]:
        return self.result.statements.months

    @property
    def currency(self) -> str:
        return self.result.config.currency

    def balance(self, category: str, month: str | None = None) -> int:
        """Month-end balance, debit-positive (liabilities come back negative)."""
        month = month or self.months[-1]
        if month not in self.months:
            raise ModelError(f"month {month} is outside the ledger ({self.months[0]} to {self.months[-1]})")
        return self.statements.balances[category][self.months.index(month)]

    def nwc_at(self, month: str) -> int:
        if month not in self.months:
            raise ModelError(f"month {month} is outside the ledger ({self.months[0]} to {self.months[-1]})")
        return self.statements.nwc[self.months.index(month)]

    def base(self) -> BaseFigures:
        if len(self.months) < 12:
            raise ModelError(f"this model needs 12 months of ledger history; the ledger has {len(self.months)}")
        s = self.statements
        return BaseFigures(
            months=s.months[-12:],
            revenue=Money(s.ltm(s.pl["revenue"])),
            reported_ebitda=Money(s.ltm(s.pl["ebitda"])),
            adjusted_ebitda=Money(s.ltm(self.result.adjusted_ebitda)),
            depreciation=Money(s.ltm(s.pl["depreciation"])),
            interest=Money(s.ltm(s.pl["interest"])),
            tax=Money(s.ltm(s.pl["tax"])),
            nwc=Money(s.nwc[-1]),
            debt=Money(-self.balance("liability_debt")),
            cash=Money(self.balance("asset_cash")),
            pending_candidates=sum(1 for r in self.result.reviewed if r.status == "pending"),
        )


def open_workspace(inputs: RunInputs, out_dir: str | Path) -> Workspace:
    data = load(inputs)
    return Workspace(inputs=inputs, data=data, result=compute(data, inputs.company), out_dir=Path(out_dir))


# ---------------------------------------------------------------- assumptions

@dataclass(frozen=True)
class Assumption:
    name: str
    value: object
    source: str  # "given", "default" or "derived"
    note: str = ""


class AssumptionLog:
    """Records every assumption a model used and where it came from, so nothing is silent."""

    def __init__(self):
        self.items: list[Assumption] = []

    def pick(self, name: str, given, default, *, derived: bool = False, note: str = ""):
        if given is not None:
            self.items.append(Assumption(name, given, "given", note))
            return given
        self.items.append(Assumption(name, default, "derived" if derived else "default", note))
        return default


def check_range(name: str, value: Decimal | int, low, high) -> None:
    if not (Decimal(low) <= Decimal(value) <= Decimal(high)):
        raise ModelError(f"{name} must be between {low} and {high}, got {value}")
