"""Discounted cash flow valuation (unlevered).

Unlevered free cash flow = EBITDA - tax on EBIT - capex - change in NWC.
Each year is discounted at the WACC (optionally mid-year). The terminal value
is either a growing perpetuity (Gordon growth) or an exit EBITDA multiple,
discounted from the end of the last year.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import Money, mul, ratio
from .projection import OperatingAssumptions, OperatingYear, project
from .workspace import ModelError, check_range


@dataclass(frozen=True)
class DCFAssumptions:
    operating: OperatingAssumptions
    wacc: Decimal
    terminal_method: str           # "gordon" or "exit_multiple"
    terminal_growth: Decimal
    exit_multiple: Decimal
    mid_year: bool


@dataclass(frozen=True)
class DCFYear:
    ops: OperatingYear
    tax_on_ebit: Money
    unlevered_fcf: Money
    discount_factor: Decimal
    present_value: Money


@dataclass(frozen=True)
class DCFResult:
    years: list[DCFYear]
    sum_of_pv: Money
    terminal_value: Money
    pv_terminal: Money
    enterprise_value: Money
    implied_ev_to_ltm_ebitda: Decimal | None
    terminal_share_of_ev: Decimal | None
    warnings: list[str]


def run_dcf(base_ebitda: int, base_revenue: int, a: DCFAssumptions) -> DCFResult:
    check_range("wacc", a.wacc, "0.01", "0.5")
    if a.terminal_method not in ("gordon", "exit_multiple"):
        raise ModelError("terminal_method must be 'gordon' or 'exit_multiple'")
    if a.terminal_method == "gordon":
        check_range("terminal_growth", a.terminal_growth, "-0.05", "0.1")
        if a.terminal_growth >= a.wacc:
            raise ModelError("terminal_growth must be below the WACC, or the perpetuity has no finite value")
    else:
        check_range("exit_multiple", a.exit_multiple, "0.5", 50)

    _, ops_years = project(base_revenue, a.operating)
    one = Decimal(1)
    years = []
    for ops in ops_years:
        tax = max(0, mul(ops.ebit, a.operating.tax_rate))
        ufcf = ops.ebitda - tax - ops.capex - ops.nwc_change
        period = Decimal(ops.year) - (Decimal("0.5") if a.mid_year else 0)
        factor = (one / (one + a.wacc) ** period).quantize(Decimal("0.0000000001"))
        years.append(DCFYear(ops, Money(tax), Money(ufcf), factor, Money(mul(ufcf, factor))))

    last = years[-1]
    if a.terminal_method == "gordon":
        terminal = mul(last.unlevered_fcf, (one + a.terminal_growth) / (a.wacc - a.terminal_growth))
    else:
        terminal = mul(last.ops.ebitda, a.exit_multiple)
    end_factor = (one / (one + a.wacc) ** a.operating.years).quantize(Decimal("0.0000000001"))
    pv_terminal = mul(terminal, end_factor)
    sum_pv = sum(y.present_value for y in years)
    ev = sum_pv + pv_terminal

    warnings = []
    share = ratio(pv_terminal, ev, 4) if ev > 0 else None
    if share is not None and share > Decimal("0.85"):
        warnings.append(f"terminal value is {share * 100:.1f}% of EV; the result rests mostly on the terminal assumption")
    if ev <= 0:
        warnings.append("enterprise value is zero or negative")
    return DCFResult(
        years=years, sum_of_pv=Money(sum_pv), terminal_value=Money(terminal), pv_terminal=Money(pv_terminal),
        enterprise_value=Money(ev),
        implied_ev_to_ltm_ebitda=ratio(ev, base_ebitda, 2) if base_ebitda > 0 else None,
        terminal_share_of_ev=share, warnings=warnings,
    )
