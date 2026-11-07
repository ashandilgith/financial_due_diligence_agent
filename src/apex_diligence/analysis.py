"""Standard diligence analyses: working capital peg, customers, credit metrics,
and the enterprise-value-to-equity price bridge.

Each function returns a plain dict. Money values are `Money` (cents) so the
tool layer can format them; ratios are Decimals.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

from .money import Money, mul, ratio
from .statements import month_of
from .workspace import ModelError, Workspace, check_range


def _days_in(months: list[str]) -> int:
    return sum(calendar.monthrange(int(m[:4]), int(m[5:]))[1] for m in months)


def _avg(values: list[int]) -> int:
    return int((Decimal(sum(values)) / len(values)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ------------------------------------------------------------------ working capital

def nwc_analysis(ws: Workspace, *, peg_months: int = 12, exclude_months: tuple[str, ...] = ()) -> dict:
    s = ws.statements
    if not 3 <= peg_months <= 24:
        raise ModelError("peg_months must be between 3 and 24")
    unknown = [m for m in exclude_months if m not in s.months]
    if unknown:
        raise ModelError(f"exclude_months not in the ledger: {', '.join(unknown)}")

    rows = []
    for i, m in enumerate(s.months):
        trailing = s.months[max(0, i - 2): i + 1]
        days = _days_in(trailing)
        revenue_3m = sum(s.pl["revenue"][max(0, i - 2): i + 1])
        cogs_3m = sum(s.pl["cogs"][max(0, i - 2): i + 1])
        ar, inv, ap = s.balances["asset_ar"][i], s.balances["asset_inventory"][i], -s.balances["liability_ap"][i]
        dso = ratio(ar * days, revenue_3m, 1) if revenue_3m > 0 else None
        dio = ratio(inv * days, cogs_3m, 1) if cogs_3m > 0 else None
        dpo = ratio(ap * days, cogs_3m, 1) if cogs_3m > 0 else None
        ccc = dso + dio - dpo if None not in (dso, dio, dpo) else None
        rows.append({"month": m, "receivables": Money(ar), "inventory": Money(inv), "payables": Money(ap),
                     "nwc": Money(s.nwc[i]), "dso_days": dso, "dio_days": dio, "dpo_days": dpo,
                     "cash_conversion_days": ccc})

    window = [r for r in rows[-peg_months:] if r["month"] not in exclude_months]
    if not window:
        raise ModelError("every month in the peg window was excluded")
    values = [r["nwc"] for r in window]
    low = min(window, key=lambda r: (r["nwc"], r["month"]))
    high = max(window, key=lambda r: (r["nwc"], r["month"]))
    peg = _avg(values)
    ltm_revenue = s.ltm(s.pl["revenue"])
    return {
        "peg": Money(peg),
        "peg_basis": f"average month-end NWC over {len(window)} month(s): {window[0]['month']} to {window[-1]['month']}"
                     + (f", excluding {', '.join(exclude_months)}" if exclude_months else ""),
        "latest_month": rows[-1]["month"],
        "latest_nwc": rows[-1]["nwc"],
        "latest_vs_peg": Money(rows[-1]["nwc"] - peg),
        "lowest": {"month": low["month"], "nwc": low["nwc"]},
        "highest": {"month": high["month"], "nwc": high["nwc"]},
        "peg_pct_of_ltm_revenue": ratio(peg * 100, ltm_revenue, 2) if ltm_revenue > 0 else None,
        "monthly": rows,
        "notes": ["NWC = receivables + inventory - payables (trade working capital only)",
                  "DSO uses trailing 3-month revenue; DIO and DPO use trailing 3-month cost of sales"],
    }


# ------------------------------------------------------------------ customers

def customer_analysis(ws: Workspace, *, top_n: int = 10) -> dict:
    if not 1 <= top_n <= 50:
        raise ModelError("top_n must be between 1 and 50")
    s, gl, mapping = ws.statements, ws.data.gl, ws.data.mapping
    current = set(s.months[-12:])
    prior = set(s.months[-24:-12]) if len(s.months) >= 24 else set()
    now, before = defaultdict(int), defaultdict(int)
    for line in gl:
        if mapping[line.account_code] != "revenue":
            continue
        m, name = month_of(line.date), line.counterparty or "(no customer name)"
        if m in current:
            now[name] -= line.net
        elif m in prior:
            before[name] -= line.net
    total = sum(now.values())
    if total <= 0:
        raise ModelError("no positive revenue in the last 12 months")

    ranked = sorted(now.items(), key=lambda kv: (-kv[1], kv[0]))
    share = lambda v: ratio(v * 100, total, 2)
    customers = []
    for name, value in ranked[:top_n]:
        p = before.get(name, 0)
        customers.append({"customer": name, "revenue": Money(value), "share_pct": share(value),
                          "prior_12m": Money(p) if prior else None,
                          "growth_pct": ratio((value - p) * 100, p, 1) if prior and p > 0 else None})
    hhi = sum((Decimal(v) / total * 100) ** 2 for _, v in ranked).quantize(Decimal("1"))
    top = lambda n: share(sum(v for _, v in ranked[:n]))
    return {
        "period": f"{s.months[-12:][0]} to {s.months[-1]}",
        "total_revenue": Money(total),
        "customer_count": len(ranked),
        "top_1_share_pct": top(1), "top_5_share_pct": top(5), "top_10_share_pct": top(10),
        "hhi": hhi,
        "hhi_reading": "high concentration" if hhi > 2500 else "moderate concentration" if hhi > 1500 else "low concentration",
        "customers": customers,
        "new_customers": sorted(n for n in now if prior and before.get(n, 0) <= 0),
        "lost_customers": sorted(n for n, v in before.items() if v > 0 and now.get(n, 0) <= 0),
        "comparison": "prior 12 months" if prior else "not available (needs 24 months of ledger)",
    }


# ------------------------------------------------------------------ credit

def credit_metrics(ws: Workspace, *, total_debt: int | None = None, cash: int | None = None,
                   interest_rate: Decimal | None = None, annual_principal: int = 0,
                   max_net_leverage: Decimal | None = None, min_interest_cover: Decimal | None = None) -> dict:
    base = ws.base()
    debt = base.debt if total_debt is None else total_debt
    cash = base.cash if cash is None else cash
    if debt < 0 or annual_principal < 0:
        raise ModelError("total_debt and annual_principal cannot be negative")
    if interest_rate is not None:
        check_range("interest_rate", interest_rate, 0, "0.5")
        interest, interest_basis = mul(debt, interest_rate), f"{interest_rate * 100}% x debt"
    else:
        interest, interest_basis = base.interest, "LTM interest expense in the P&L"
    ebitda, net_debt = base.adjusted_ebitda, debt - cash
    positive = ebitda > 0
    debt_service = interest + annual_principal
    result = {
        "ltm_adjusted_ebitda": Money(ebitda),
        "total_debt": Money(debt), "cash": Money(cash), "net_debt": Money(net_debt),
        "interest": Money(interest), "interest_basis": interest_basis,
        "annual_principal": Money(annual_principal),
        "gross_leverage": ratio(debt, ebitda, 2) if positive else None,
        "net_leverage": ratio(net_debt, ebitda, 2) if positive else None,
        "interest_cover": ratio(ebitda, interest, 2) if interest > 0 else None,
        "dscr": ratio(ebitda - base.tax, debt_service, 2) if debt_service > 0 else None,
        "dscr_basis": "(LTM adjusted EBITDA - LTM tax) / (interest + annual principal)",
        "covenants": [],
        "warnings": [] if positive else ["EBITDA is zero or negative: leverage ratios are not meaningful"],
    }
    if base.pending_candidates:
        result["warnings"].append(f"{base.pending_candidates} adjustment candidate(s) are still pending review; "
                                  "EBITDA includes only accepted adjustments")
    if max_net_leverage is not None:
        check_range("max_net_leverage", max_net_leverage, "0.1", 20)
        floor = max(0, int((Decimal(net_debt) / max_net_leverage).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        result["covenants"].append({
            "covenant": f"net leverage at most {max_net_leverage}x",
            "passes": positive and result["net_leverage"] <= max_net_leverage,
            "ebitda_floor": Money(floor),
            "ebitda_headroom_pct": ratio((ebitda - floor) * 100, ebitda, 1) if positive else None,
        })
    if min_interest_cover is not None:
        check_range("min_interest_cover", min_interest_cover, "0.1", 50)
        floor = mul(interest, min_interest_cover)
        result["covenants"].append({
            "covenant": f"interest cover at least {min_interest_cover}x",
            "passes": interest == 0 or (result["interest_cover"] or 0) >= min_interest_cover,
            "ebitda_floor": Money(floor),
            "ebitda_headroom_pct": ratio((ebitda - floor) * 100, ebitda, 1) if positive else None,
        })
    return result


# ------------------------------------------------------------------ price bridge

def purchase_price_bridge(ws: Workspace, *, ev_multiple: Decimal | None = None, enterprise_value: int | None = None,
                          closing_month: str | None = None, peg: int | None = None,
                          debt_like_items: list[tuple[str, int]] = (),
                          cash_like_items: list[tuple[str, int]] = ()) -> dict:
    if (ev_multiple is None) == (enterprise_value is None):
        raise ModelError("give exactly one of ev_multiple or enterprise_value")
    base = ws.base()
    closing_month = closing_month or ws.months[-1]
    if ev_multiple is not None:
        check_range("ev_multiple", ev_multiple, "0.5", 50)
        if base.adjusted_ebitda <= 0:
            raise ModelError("LTM adjusted EBITDA is not positive, so a multiple cannot set the price")
        ev, ev_basis = mul(base.adjusted_ebitda, ev_multiple), f"{ev_multiple}x LTM adjusted EBITDA"
    else:
        if enterprise_value <= 0:
            raise ModelError("enterprise_value must be positive")
        ev, ev_basis = enterprise_value, "given"

    debt = -ws.balance("liability_debt", closing_month)
    cash = ws.balance("asset_cash", closing_month)
    actual_nwc = ws.nwc_at(closing_month)
    if peg is None:
        i = ws.months.index(closing_month)
        window = ws.statements.nwc[max(0, i - 11): i + 1]
        peg, peg_basis = _avg(window), f"average month-end NWC, {len(window)} months to {closing_month}"
    else:
        peg_basis = "given"
    debt_like = sum(v for _, v in debt_like_items)
    cash_like = sum(v for _, v in cash_like_items)
    nwc_adjustment = actual_nwc - peg
    equity = ev - debt + cash - debt_like + cash_like + nwc_adjustment
    return {
        "enterprise_value": Money(ev), "ev_basis": ev_basis,
        "less_debt": Money(-debt),
        "plus_cash": Money(cash),
        "less_debt_like_items": [{"item": n, "amount": Money(-v)} for n, v in debt_like_items],
        "plus_cash_like_items": [{"item": n, "amount": Money(v)} for n, v in cash_like_items],
        "nwc_at_close": Money(actual_nwc), "nwc_peg": Money(peg), "peg_basis": peg_basis,
        "nwc_adjustment": Money(nwc_adjustment),
        "equity_value": Money(equity),
        "closing_month": closing_month,
        "notes": ["Equity value = EV - debt + cash - debt-like items + cash-like items + (NWC at close - peg)",
                  "Debt is the closing balance of accounts mapped to liability_debt"],
    }
