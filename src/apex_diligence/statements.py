"""Monthly financial statements built from the ledger.

Everything here is plain arithmetic on integer cents. Each series is a list
aligned with `months`, so series[i] always belongs to months[i].
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .models import GLLine
from .taxonomy import BS_CATEGORIES, PL_CATEGORIES, pl_amount


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_range(first: str, last: str) -> list[str]:
    """Every month from first to last inclusive, with no gaps: "2024-11".."2025-02"."""
    year, month = int(first[:4]), int(first[5:])
    end = (int(last[:4]), int(last[5:]))
    months = []
    while (year, month) <= end:
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def ledger_months(gl: list[GLLine]) -> list[str]:
    return month_range(month_of(min(l.date for l in gl)), month_of(max(l.date for l in gl)))


@dataclass(frozen=True)
class Statements:
    months: list[str]
    pl: dict[str, list[int]]        # P&L categories + derived rows, natural signs
    balances: dict[str, list[int]]  # month-end balances, debit-positive
    nwc: list[int]                  # net working capital at each month end

    def ltm(self, series: list[int]) -> int:
        """Last twelve months (or all months if fewer than 12)."""
        return sum(series[-12:])


def build_statements(gl: list[GLLine], mapping: dict[str, str]) -> Statements:
    months = ledger_months(gl)
    index = {m: i for i, m in enumerate(months)}

    pl = {c: [0] * len(months) for c in PL_CATEGORIES}
    movement = {c: [0] * len(months) for c in BS_CATEGORIES}

    for line in gl:
        category = mapping[line.account_code]
        i = index[month_of(line.date)]
        if category in pl:
            pl[category][i] += pl_amount(category, line.net)
        else:
            movement[category][i] += line.net

    # Derived rows. Written out long-hand so each formula is easy to check.
    n = len(months)
    pl["gross_profit"] = [pl["revenue"][i] - pl["cogs"][i] for i in range(n)]
    pl["ebitda"] = [pl["gross_profit"][i] - pl["opex"][i] for i in range(n)]
    pl["ebit"] = [pl["ebitda"][i] - pl["depreciation"][i] for i in range(n)]
    pl["net_income"] = [
        pl["ebit"][i] - pl["interest"][i] + pl["other_income"][i] - pl["tax"][i] for i in range(n)
    ]

    # Month-end balances = running total of each month's movement.
    balances = {}
    for category, moves in movement.items():
        running, series = 0, []
        for amount in moves:
            running += amount
            series.append(running)
        balances[category] = series

    # Working capital = receivables + inventory - payables.
    # Payables are credits, so their debit-positive balance is already negative.
    nwc = [
        balances["asset_ar"][i] + balances["asset_inventory"][i] + balances["liability_ap"][i]
        for i in range(n)
    ]
    return Statements(months=months, pl=pl, balances=balances, nwc=nwc)


def revenue_by_customer(gl: list[GLLine], mapping: dict[str, str], months: list[str]) -> list[tuple[str, int]]:
    """Revenue per counterparty over `months`, largest first. Blank names grouped."""
    wanted = set(months)
    totals: dict[str, int] = defaultdict(int)
    for line in gl:
        if mapping[line.account_code] == "revenue" and month_of(line.date) in wanted:
            totals[line.counterparty or "(no customer name)"] += pl_amount("revenue", line.net)
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
