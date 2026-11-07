"""Leveraged buyout model.

Sources and uses at entry, a two-tranche debt schedule (senior with
amortisation and cash sweep, subordinated bullet), exit, returns (IRR and
money multiple), credit statistics by year, and a value-creation bridge.

Conventions, stated so a reviewer can check them:
- Cash-free, debt-free deal at an enterprise value of entry multiple x LTM adjusted EBITDA.
- Interest is charged on each tranche's opening balance (no circular references).
- Tax is on profit after interest, and never negative.
- Free cash flow = EBITDA - capex - change in NWC - tax - interest.
- Mandatory senior repayment first, then a share of the remaining cash sweeps the senior debt.
- Exit equity = exit EV - debt + cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .money import Money, fmt, mul, ratio
from .projection import OperatingAssumptions, OperatingYear, project
from .workspace import ModelError, check_range


@dataclass(frozen=True)
class LBOAssumptions:
    operating: OperatingAssumptions
    entry_multiple: Decimal
    exit_multiple: Decimal
    senior_multiple: Decimal
    senior_rate: Decimal
    senior_amortisation: Decimal   # fraction of the original senior loan repaid each year
    cash_sweep: Decimal            # fraction of spare cash used to prepay senior debt
    sub_multiple: Decimal
    sub_rate: Decimal
    fees_pct: Decimal              # transaction fees as a fraction of entry EV


@dataclass(frozen=True)
class LBOYear:
    ops: OperatingYear
    senior_open: Money
    sub_open: Money
    interest: Money
    tax: Money
    free_cash_flow: Money
    mandatory_repayment: Money
    sweep: Money
    senior_close: Money
    sub_close: Money
    cash_close: Money
    net_debt: Money
    net_leverage: Decimal | None   # net debt / EBITDA
    interest_cover: Decimal | None  # EBITDA / interest


@dataclass(frozen=True)
class LBOResult:
    base_revenue: Money
    base_ebitda: Money
    entry_ev: Money
    fees: Money
    senior_debt: Money
    sub_debt: Money
    equity: Money
    years: list[LBOYear]
    exit_ev: Money
    exit_net_debt: Money
    exit_equity: Money
    moic: Decimal | None
    irr: Decimal | None
    bridge: dict[str, Money]
    warnings: list[str]


def validate(a: LBOAssumptions) -> None:
    check_range("entry_multiple", a.entry_multiple, "0.5", 50)
    check_range("exit_multiple", a.exit_multiple, "0.5", 50)
    check_range("senior_multiple", a.senior_multiple, 0, 20)
    check_range("sub_multiple", a.sub_multiple, 0, 20)
    check_range("senior_rate", a.senior_rate, 0, "0.5")
    check_range("sub_rate", a.sub_rate, 0, "0.5")
    check_range("senior_amortisation", a.senior_amortisation, 0, 1)
    check_range("cash_sweep", a.cash_sweep, 0, 1)
    check_range("fees_pct", a.fees_pct, 0, "0.2")


def run_lbo(base_revenue: int, base_ebitda: int, a: LBOAssumptions) -> LBOResult:
    validate(a)
    if base_ebitda <= 0:
        raise ModelError("LTM adjusted EBITDA is zero or negative, so an EBITDA-multiple LBO cannot be priced")

    entry_ev = mul(base_ebitda, a.entry_multiple)
    fees = mul(entry_ev, a.fees_pct)
    senior = mul(base_ebitda, a.senior_multiple)
    sub = mul(base_ebitda, a.sub_multiple)
    equity = entry_ev + fees - senior - sub
    if equity <= 0:
        raise ModelError(
            f"debt ({a.senior_multiple + a.sub_multiple}x EBITDA) covers the whole price plus fees, "
            "so equity would be zero or negative; lower the leverage or raise the entry multiple")

    nwc_0, ops_years = project(base_revenue, a.operating)
    senior_open, sub_open, cash = senior, sub, 0
    years, warnings = [], []
    for ops in ops_years:
        interest = mul(senior_open, a.senior_rate) + mul(sub_open, a.sub_rate)
        tax = max(0, mul(ops.ebit - interest, a.operating.tax_rate))
        fcf = ops.ebitda - ops.capex - ops.nwc_change - tax - interest
        mandatory = min(mul(senior, a.senior_amortisation), senior_open)
        available = cash + fcf - mandatory
        sweep = min(max(0, mul(available, a.cash_sweep)), senior_open - mandatory)
        senior_close = senior_open - mandatory - sweep
        cash = available - sweep
        if cash < 0:
            warnings.append(f"year {ops.year}: cash shortfall of {fmt(-cash)}; the business cannot "
                            "service this debt without new funding")
        net_debt = senior_close + sub_open - cash
        years.append(LBOYear(
            ops=ops, senior_open=Money(senior_open), sub_open=Money(sub_open), interest=Money(interest),
            tax=Money(tax), free_cash_flow=Money(fcf), mandatory_repayment=Money(mandatory),
            sweep=Money(sweep), senior_close=Money(senior_close), sub_close=Money(sub_open),
            cash_close=Money(cash), net_debt=Money(net_debt),
            net_leverage=ratio(net_debt, ops.ebitda, 2) if ops.ebitda > 0 else None,
            interest_cover=ratio(ops.ebitda, interest, 2) if interest > 0 else None,
        ))
        senior_open = senior_close

    last = years[-1]
    exit_ev = mul(last.ops.ebitda, a.exit_multiple)
    exit_net_debt = last.net_debt
    exit_equity = exit_ev - exit_net_debt
    if exit_equity > 0:
        moic = (Decimal(exit_equity) / Decimal(equity)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        exact = (Decimal(exit_equity) / Decimal(equity)) ** (Decimal(1) / Decimal(a.operating.years)) - 1
        irr = exact.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    else:
        moic, irr = Decimal(0), None
        warnings.append("exit equity is zero or negative: the equity investment is lost")

    # Value creation bridge: the four parts add up exactly to the equity gain.
    ebitda_growth = mul(last.ops.ebitda, a.entry_multiple) - entry_ev
    bridge = {
        "ebitda_growth": Money(ebitda_growth),
        "multiple_change": Money(exit_ev - mul(last.ops.ebitda, a.entry_multiple)),
        "debt_paydown_and_cash": Money((senior + sub) - exit_net_debt),
        "fees": Money(-fees),
        "equity_gain": Money(exit_equity - equity),
    }
    assert sum(v for k, v in bridge.items() if k != "equity_gain") == bridge["equity_gain"]

    return LBOResult(
        base_revenue=Money(base_revenue), base_ebitda=Money(base_ebitda), entry_ev=Money(entry_ev),
        fees=Money(fees), senior_debt=Money(senior), sub_debt=Money(sub), equity=Money(equity),
        years=years, exit_ev=Money(exit_ev), exit_net_debt=Money(exit_net_debt),
        exit_equity=Money(exit_equity), moic=moic, irr=irr, bridge=bridge, warnings=warnings,
    )
