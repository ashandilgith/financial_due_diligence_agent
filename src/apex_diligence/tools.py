"""The tools the chat agent can call.

Every tool is a deterministic function over the workspace: same workspace and
same input give the same output, byte for byte. The language model chooses
tools and explains results; it never produces a number itself.

Units in tool inputs: amounts in currency units (e.g. 1500000.50), percentages
as percent numbers (9 means 9%), multiples as plain numbers (6.5 means 6.5x).
"""

from __future__ import annotations

import ast
import hashlib
import operator
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DivisionByZero, InvalidOperation
from pathlib import Path
from typing import Callable

from . import analysis
from .cashflow import forecast_13_weeks
from .databook import fiscal_year
from .dcf import DCFAssumptions, run_dcf
from .lbo import LBOAssumptions, run_lbo
from .money import Money, ratio, to_cents
from .projection import resolve_operating
from .schema import ToolInputError, canonical, to_json, validate
from .workspace import AssumptionLog, ModelError, Workspace

HUNDRED = Decimal(100)
MONTH = {"type": "string", "pattern": r"\d{4}-\d{2}", "description": "YYYY-MM"}
DATE = {"type": "string", "pattern": r"\d{4}-\d{2}-\d{2}", "description": "YYYY-MM-DD"}
NUM = {"type": "number"}


PLACES = Decimal("0.0001")


def num(x) -> Decimal:
    """A number input (multiple, days, ratio) held to 4 decimal places."""
    return Decimal(str(x)).quantize(PLACES, rounding=ROUND_HALF_UP)


def pct(x) -> Decimal | None:
    """A percent input as a fraction held to 4 decimal places: 9.125 -> 0.0913 (9.13%).
    Four places keep every exported Excel formula exact to the centavo (see model_export)."""
    return None if x is None else (Decimal(str(x)) / HUNDRED).quantize(PLACES, rounding=ROUND_HALF_UP)


def cents(x) -> int | None:
    return None if x is None else to_cents(x)


def pct_out(fraction: Decimal | None, places: int = 2) -> Decimal | None:
    if fraction is None:
        return None
    return (fraction * HUNDRED).quantize(Decimal(10) ** -places)


# Assumption names that are fractions (shown as %) or multiples (shown as x).
FRACTIONS = {"revenue_growth", "ebitda_margin", "da_pct_revenue", "capex_pct_revenue", "nwc_pct_revenue",
             "tax_rate", "senior_rate", "senior_amortisation", "cash_sweep", "sub_rate", "fees_pct",
             "wacc", "terminal_growth"}


def assumptions_out(log: AssumptionLog) -> list[dict]:
    out = []
    for a in log.items:
        value, unit = a.value, ""
        if a.name in FRACTIONS:
            unit = "%"
            value = [pct_out(v) for v in value] if isinstance(value, tuple) else pct_out(Decimal(value))
        elif "multiple" in a.name:
            unit = "x"
        item = {"name": a.name, "value": value, "unit": unit, "source": a.source}
        if a.note:
            item["note"] = a.note
        out.append(item)
    return out


# ------------------------------------------------------------------ schemas

OPERATING_PROPS = {
    "years": {"type": "integer", "description": "projection years, 1-10 (default 5)"},
    "revenue_growth_pct": {"anyOf": [NUM, {"type": "array", "items": NUM, "maxItems": 10}],
                           "description": "annual revenue growth %, one number or one per year (default 5)"},
    "ebitda_margin_pct": {**NUM, "description": "EBITDA margin % (default: LTM adjusted margin)"},
    "da_pct_revenue": {**NUM, "description": "D&A as % of revenue (default: LTM)"},
    "capex_pct_revenue": {**NUM, "description": "capex as % of revenue (default 2; ledger has no capex history)"},
    "nwc_pct_revenue": {**NUM, "description": "NWC as % of revenue (default: closing NWC / LTM revenue)"},
    "tax_rate_pct": {**NUM, "description": "tax rate % (default 25)"},
}

LBO_PROPS = {
    "entry_multiple": {**NUM, "description": "entry EV / LTM adjusted EBITDA, e.g. 6.5"},
    "exit_multiple": {**NUM, "description": "exit EV / exit-year EBITDA (default: entry multiple)"},
    "senior_debt_multiple": {**NUM, "description": "senior debt as x LTM EBITDA (default 3.0)"},
    "senior_rate_pct": {**NUM, "description": "senior interest rate % (default 9)"},
    "senior_amortisation_pct": {**NUM, "description": "% of original senior debt repaid each year (default 5)"},
    "cash_sweep_pct": {**NUM, "description": "% of spare cash used to prepay senior debt (default 100)"},
    "sub_debt_multiple": {**NUM, "description": "subordinated bullet debt as x LTM EBITDA (default 0)"},
    "sub_rate_pct": {**NUM, "description": "subordinated interest rate % (default 12)"},
    "fees_pct": {**NUM, "description": "transaction fees % of entry EV (default 2)"},
    **OPERATING_PROPS,
}

DCF_PROPS = {
    "wacc_pct": {**NUM, "description": "discount rate % (WACC), e.g. 12"},
    "terminal_method": {"type": "string", "enum": ["gordon", "exit_multiple"], "description": "default gordon"},
    "terminal_growth_pct": {**NUM, "description": "perpetual growth % for gordon (default 3)"},
    "exit_multiple": {**NUM, "description": "terminal EV / final-year EBITDA for exit_multiple (default 6)"},
    "mid_year": {"type": "boolean", "description": "mid-year discounting (default true)"},
    **OPERATING_PROPS,
}

ITEMS = {"type": "array", "maxItems": 20, "items": {
    "type": "object", "required": ["name", "amount"],
    "properties": {"name": {"type": "string"}, "amount": NUM}}}

PERIOD = {"type": "string", "description": "ltm, all, FY2025 (fiscal year), 2025 (calendar year), 2025-Q2 or 2025-03"}
SERIES = ["revenue", "cogs", "gross_profit", "opex", "reported_ebitda", "adjustments", "adjusted_ebitda",
          "depreciation", "interest", "tax", "net_income", "nwc", "cash", "receivables", "inventory", "payables", "debt"]


# ------------------------------------------------------------------ toolbox

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[Workspace, dict], object]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


class Toolbox:
    def __init__(self, workspace: Workspace):
        self.ws = workspace
        self.tools = {t.name: t for t in TOOLS}

    def specs(self) -> list[dict]:
        return [t.spec() for t in TOOLS]

    def call(self, name: str, arguments: dict) -> tuple[dict, bool]:
        """Run a tool. Returns (output, is_error). Never raises for bad input."""
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown tool '{name}'; available: {', '.join(self.tools)}"}, True
        try:
            validate(arguments, tool.schema)
            return to_json(tool.handler(self.ws, arguments)), False
        except (ToolInputError, ModelError) as e:
            return {"error": str(e)}, True


# ------------------------------------------------------------------ handlers: financials

def months_for(ws: Workspace, period: str) -> tuple[list[str], str]:
    months = ws.months
    p = period.strip()
    if p.lower() == "ltm":
        chosen, label = months[-12:], f"LTM to {months[-1]}"
    elif p.lower() == "all":
        chosen, label = months, f"{months[0]} to {months[-1]}"
    elif p.upper().startswith("FY") and p[2:].isdigit():
        fy_end = ws.result.config.fiscal_year_end_month
        chosen = [m for m in months if fiscal_year(m, fy_end) == int(p[2:])]
        label = f"FY{p[2:]}"
    elif len(p) == 4 and p.isdigit():
        chosen, label = [m for m in months if m.startswith(p)], f"calendar {p}"
    elif len(p) == 7 and p[4:6].upper() == "-Q" and p[6] in "1234":
        q = int(p[6])
        chosen = [m for m in months if m[:4] == p[:4] and (int(m[5:]) - 1) // 3 + 1 == q]
        label = p.upper()
    elif len(p) == 7 and p[4] == "-":
        chosen, label = [m for m in months if m == p], p
    else:
        raise ToolInputError(f"period {period!r} not understood; use ltm, all, FY2025, 2025, 2025-Q2 or 2025-03")
    if not chosen:
        raise ModelError(f"no ledger months in {label}; the ledger covers {months[0]} to {months[-1]}")
    return chosen, label


def get_financials(ws: Workspace, a: dict) -> dict:
    chosen, label = months_for(ws, a.get("period", "ltm"))
    s, idx = ws.statements, [ws.months.index(m) for m in chosen]
    total = lambda series: Money(sum(series[i] for i in idx))
    pl = {k: total(v) for k, v in s.pl.items()}
    accepted, pending = {}, {"count": 0, "proposed_effect": 0}
    for r in ws.result.reviewed:
        if r.candidate.month not in chosen:
            continue
        if r.status == "accepted":
            accepted[r.candidate.rule] = accepted.get(r.candidate.rule, 0) + r.final_effect
        elif r.status == "pending":
            pending["count"] += 1
            pending["proposed_effect"] += r.candidate.proposed_effect
    adjusted = total(ws.result.adjusted_ebitda)
    margin = lambda v: pct_out(ratio(v, pl["revenue"], 6)) if pl["revenue"] else None
    complete = not (a.get("period", "ltm").lower() == "ltm" and len(chosen) < 12)
    return {
        "period": label, "months": f"{chosen[0]} to {chosen[-1]} ({len(chosen)} months)",
        "currency": ws.currency,
        "revenue": pl["revenue"], "cogs": pl["cogs"], "gross_profit": pl["gross_profit"], "opex": pl["opex"],
        "reported_ebitda": pl["ebitda"],
        "accepted_adjustments_by_rule": {k: Money(v) for k, v in sorted(accepted.items())},
        "adjusted_ebitda": adjusted,
        "depreciation": pl["depreciation"], "ebit": pl["ebit"], "interest": pl["interest"],
        "other_income": pl["other_income"], "tax": pl["tax"], "net_income": pl["net_income"],
        "gross_margin_pct": margin(pl["gross_profit"]), "reported_ebitda_margin_pct": margin(pl["ebitda"]),
        "adjusted_ebitda_margin_pct": margin(adjusted),
        "pending_review": {"count": pending["count"], "proposed_effect": Money(pending["proposed_effect"])},
        "note": None if complete else "fewer than 12 months of data in this LTM window",
    }


def monthly_series(ws: Workspace, a: dict) -> dict:
    s, metric = ws.statements, a["metric"]
    start, end = a.get("start", ws.months[0]), a.get("end", ws.months[-1])
    chosen = [m for m in ws.months if start <= m <= end]
    if not chosen:
        raise ModelError(f"no months between {start} and {end}; the ledger covers {ws.months[0]} to {ws.months[-1]}")
    special = {
        "reported_ebitda": s.pl["ebitda"], "adjustments": ws.result.adjustments,
        "adjusted_ebitda": ws.result.adjusted_ebitda, "nwc": s.nwc,
        "cash": s.balances["asset_cash"], "receivables": s.balances["asset_ar"],
        "inventory": s.balances["asset_inventory"],
        "payables": [-v for v in s.balances["liability_ap"]], "debt": [-v for v in s.balances["liability_debt"]],
    }
    source = special[metric] if metric in special else s.pl[metric]
    kind = "month-end balance" if metric in ("nwc", "cash", "receivables", "inventory", "payables", "debt") else "monthly total"
    return {"metric": metric, "kind": kind, "currency": ws.currency,
            "values": [{"month": m, "value": Money(source[ws.months.index(m)])} for m in chosen]}


def list_adjustments(ws: Workspace, a: dict) -> dict:
    rows = [r for r in ws.result.reviewed
            if a.get("rule") in (None, r.candidate.rule) and a.get("status") in (None, r.status)]
    limit = int(a.get("limit", 50))
    if not 1 <= limit <= 200:
        raise ToolInputError("limit must be between 1 and 200")
    return {
        "matching": len(rows), "shown": min(limit, len(rows)),
        "total_proposed_effect": Money(sum(r.candidate.proposed_effect for r in rows)),
        "total_accepted_effect": Money(sum(r.final_effect for r in rows if r.status == "accepted")),
        "candidates": [{"id": r.candidate.id, "rule": r.candidate.rule, "finding": r.candidate.title,
                        "date": r.candidate.date, "counterparty": r.candidate.counterparty,
                        "description": r.candidate.description,
                        "proposed_effect": Money(r.candidate.proposed_effect), "status": r.status,
                        "final_effect": Money(r.final_effect), "evidence": [str(e) for e in r.candidate.evidence]}
                       for r in rows[:limit]],
        "rules": {"OWN": "owner/personal expense", "REL": "related party", "CUT": "year-end revenue cut-off",
                  "UNU": "unusually large (possible one-off) cost"},
    }


def explain_adjustment(ws: Workspace, a: dict) -> dict:
    match = next((r for r in ws.result.reviewed if r.candidate.id == a["candidate_id"]), None)
    if match is None:
        raise ModelError(f"no candidate with id {a['candidate_id']}; use list_adjustments to see ids")
    c = match.candidate
    by_source = {l.source: l for l in ws.data.gl}
    journals = {by_source[e].journal_id for e in c.evidence if e in by_source}
    lines = [l for l in ws.data.gl if l.journal_id in journals]
    return {
        "candidate": {"id": c.id, "rule": c.rule, "finding": c.title, "why_flagged": c.rationale,
                      "amount_involved": Money(c.amount_involved), "proposed_effect": Money(c.proposed_effect),
                      "status": match.status, "final_effect": Money(match.final_effect), "reviewer_note": match.note},
        "journal_lines": [{"source": str(l.source), "date": l.date, "journal_id": l.journal_id,
                           "account": f"{l.account_code} {l.account_name}".strip(), "description": l.description,
                           "counterparty": l.counterparty, "debit": Money(l.debit), "credit": Money(l.credit),
                           "entry_type": l.entry_type} for l in lines],
        "how_to_decide": "the reviewer sets decision (accept/reject) and optional amount_override in review.csv",
    }


def proof_of_cash(ws: Workspace, a: dict) -> dict:
    cash = ws.result.cash
    return {"tolerance": Money(ws.result.config.cash_tolerance),
            "months_not_tying": [m.month for m in cash if not m.ties],
            "months": [{"month": m.month, "book_net": Money(m.book_net), "bank_net": Money(m.bank_net),
                        "difference_bank_minus_book": Money(m.difference), "ties": m.ties} for m in cash],
            "reading": "differences that reverse in the next month are usually timing (e.g. uncleared cheques); "
                       "ones that do not reverse need explaining"}


def run_checks(ws: Workspace, a: dict) -> dict:
    return {"checks": [{"name": c.name, "severity": c.severity, "passed": c.passed, "detail": c.detail}
                       for c in ws.result.checks]}


# ------------------------------------------------------------------ handlers: analyses

def nwc_tool(ws: Workspace, a: dict) -> dict:
    out = analysis.nwc_analysis(ws, peg_months=int(a.get("peg_months", 12)),
                                exclude_months=tuple(a.get("exclude_months", ())))
    if not a.get("include_monthly", False):
        out["monthly"] = out["monthly"][-12:]
    return out


def customers_tool(ws: Workspace, a: dict) -> dict:
    return analysis.customer_analysis(ws, top_n=int(a.get("top_n", 10)))


def credit_tool(ws: Workspace, a: dict) -> dict:
    return analysis.credit_metrics(
        ws, total_debt=cents(a.get("total_debt")), cash=cents(a.get("cash")),
        interest_rate=pct(a.get("interest_rate_pct")), annual_principal=cents(a.get("annual_principal", 0)),
        max_net_leverage=None if a.get("max_net_leverage") is None else num(a["max_net_leverage"]),
        min_interest_cover=None if a.get("min_interest_cover") is None else num(a["min_interest_cover"]))


def bridge_tool(ws: Workspace, a: dict) -> dict:
    return analysis.purchase_price_bridge(
        ws, ev_multiple=None if a.get("ev_multiple") is None else num(a["ev_multiple"]),
        enterprise_value=cents(a.get("enterprise_value")), closing_month=a.get("closing_month"),
        peg=cents(a.get("peg")),
        debt_like_items=[(i["name"], to_cents(i["amount"])) for i in a.get("debt_like_items", [])],
        cash_like_items=[(i["name"], to_cents(i["amount"])) for i in a.get("cash_like_items", [])])


def build_forecast(ws: Workspace, a: dict):
    as_of = None
    if a.get("as_of"):
        try:
            as_of = date.fromisoformat(a["as_of"])
        except ValueError:
            raise ToolInputError(f"as_of {a['as_of']!r} is not a real date") from None
    return forecast_13_weeks(ws, as_of=as_of, sales_growth=pct(a.get("sales_growth_pct", 0)),
                             collection_days=None if a.get("collection_days") is None else num(a["collection_days"]),
                             payment_days=None if a.get("payment_days") is None else num(a["payment_days"]),
                             min_cash=cents(a.get("min_cash", 0)))


def cashflow_tool(ws: Workspace, a: dict) -> dict:
    f = build_forecast(ws, a)
    totals = lambda key: Money(sum(sum(getattr(w, key).values()) for w in f.weeks))
    return {
        "as_of": f.as_of, "currency": ws.currency,
        "opening_cash": f.opening_cash, "opening_source": f.opening_source,
        "book_cash_at_as_of": f.book_cash, "bank_minus_book": Money(f.opening_cash - f.book_cash),
        "ending_cash_week_13": f.weeks[-1].closing,
        "total_receipts": totals("receipts"), "total_payments": totals("payments"),
        "lowest_week": f.lowest_week, "lowest_closing": f.lowest_closing,
        "minimum_cash": f.min_cash, "weeks_below_minimum": f.weeks_below_minimum,
        "assumptions": {
            "collection_days": f.collection_days, "collection_lag_weeks": f.collection_lag_weeks,
            "payment_days": f.payment_days, "payment_lag_weeks": f.payment_lag_weeks,
            "weekly_sales_run_rate": f.weekly_sales, "weekly_purchases_run_rate": f.weekly_purchases,
            "collectible_receivables": f.collectible_receivables,
            "receivables_excluded_as_accepted_cutoff": f.excluded_receivables,
            "opening_payables": f.opening_payables,
            "sales_growth_pct": a.get("sales_growth_pct", 0),
        },
        "payment_streams": [{"line": s.account_name, "counterparty": s.counterparty, "cadence": s.cadence,
                             "amount": s.amount, "day_of_month": s.day_of_month} for s in f.streams],
        "excluded_one_off_payments": f.excluded_one_offs,
        "not_forecast_balance_sheet_payments_last_13_weeks": f.not_forecast,
        "weeks": [{"week": w.number, "start": w.start, "end": w.end, "opening": w.opening,
                   "receipts": w.receipts, "payments": {k: v for k, v in w.payments.items() if v},
                   "net": w.net, "closing": w.closing, "below_minimum": w.below_minimum,
                   **({"actual_net": w.actual_net, "variance": w.variance} if w.actual_net is not None else {})}
                  for w in f.weeks],
        "backtest": f.backtest,
    }


# ------------------------------------------------------------------ handlers: models

def _operating(ws: Workspace, a: dict, log: AssumptionLog):
    base = ws.base()
    growth = a.get("revenue_growth_pct")
    growth = [pct(g) for g in growth] if isinstance(growth, list) else pct(growth)
    return base, resolve_operating(
        base, log, years=None if a.get("years") is None else int(a["years"]), revenue_growth=growth,
        ebitda_margin=pct(a.get("ebitda_margin_pct")), da_pct_revenue=pct(a.get("da_pct_revenue")),
        capex_pct_revenue=pct(a.get("capex_pct_revenue")), nwc_pct_revenue=pct(a.get("nwc_pct_revenue")),
        tax_rate=pct(a.get("tax_rate_pct")))


def build_lbo(ws: Workspace, a: dict):
    log = AssumptionLog()
    base, operating = _operating(ws, a, log)
    entry = log.pick("entry_multiple", num(a["entry_multiple"]), None)
    assumptions = LBOAssumptions(
        operating=operating, entry_multiple=entry,
        exit_multiple=log.pick("exit_multiple", None if a.get("exit_multiple") is None else num(a["exit_multiple"]),
                               entry, note="same as entry: no multiple expansion"),
        senior_multiple=log.pick("senior_debt_multiple", None if a.get("senior_debt_multiple") is None
                                 else num(a["senior_debt_multiple"]), Decimal(3)),
        senior_rate=log.pick("senior_rate", pct(a.get("senior_rate_pct")), Decimal("0.09")),
        senior_amortisation=log.pick("senior_amortisation", pct(a.get("senior_amortisation_pct")), Decimal("0.05")),
        cash_sweep=log.pick("cash_sweep", pct(a.get("cash_sweep_pct")), Decimal(1)),
        sub_multiple=log.pick("sub_debt_multiple", None if a.get("sub_debt_multiple") is None
                              else num(a["sub_debt_multiple"]), Decimal(0)),
        sub_rate=log.pick("sub_rate", pct(a.get("sub_rate_pct")), Decimal("0.12")),
        fees_pct=log.pick("fees_pct", pct(a.get("fees_pct")), Decimal("0.02")),
    )
    return base, assumptions, run_lbo(base.revenue, base.adjusted_ebitda, assumptions), log


def lbo_tool(ws: Workspace, a: dict) -> dict:
    base, assumptions, r, log = build_lbo(ws, a)
    caveats = list(r.warnings)
    if base.pending_candidates:
        caveats.append(f"{base.pending_candidates} adjustment candidate(s) are pending review; "
                       "the entry EBITDA includes accepted adjustments only")
    return {
        "currency": ws.currency,
        "entry": {"ltm_revenue": r.base_revenue, "ltm_adjusted_ebitda": r.base_ebitda,
                  "enterprise_value": r.entry_ev, "fees": r.fees, "senior_debt": r.senior_debt,
                  "subordinated_debt": r.sub_debt, "sponsor_equity": r.equity,
                  "equity_pct_of_sources": pct_out(ratio(r.equity, r.entry_ev + r.fees, 6), 1),
                  "entry_leverage_x": ratio(r.senior_debt + r.sub_debt, r.base_ebitda, 2)},
        "returns": {"exit_enterprise_value": r.exit_ev, "exit_net_debt": r.exit_net_debt,
                    "exit_equity": r.exit_equity, "moic_x": r.moic, "irr_pct": pct_out(r.irr)},
        "value_creation_bridge": r.bridge,
        "years": [{"year": y.ops.year, "revenue": y.ops.revenue, "ebitda": y.ops.ebitda, "capex": y.ops.capex,
                   "nwc_change": y.ops.nwc_change, "interest": y.interest, "tax": y.tax,
                   "free_cash_flow": y.free_cash_flow, "mandatory_repayment": y.mandatory_repayment,
                   "cash_sweep": y.sweep, "senior_debt_close": y.senior_close, "sub_debt_close": y.sub_close,
                   "cash_close": y.cash_close, "net_debt": y.net_debt, "net_leverage_x": y.net_leverage,
                   "interest_cover_x": y.interest_cover} for y in r.years],
        "assumptions": assumptions_out(log),
        "conventions": "cash-free debt-free entry; interest on opening balances; tax on profit after interest; "
                       "IRR from a single entry and exit cash flow",
        "caveats": caveats,
    }


def build_dcf(ws: Workspace, a: dict):
    log = AssumptionLog()
    base, operating = _operating(ws, a, log)
    method = log.pick("terminal_method", a.get("terminal_method"), "gordon")
    assumptions = DCFAssumptions(
        operating=operating, wacc=log.pick("wacc", pct(a["wacc_pct"]), None),
        terminal_method=method,
        terminal_growth=log.pick("terminal_growth", pct(a.get("terminal_growth_pct")), Decimal("0.03"))
        if method == "gordon" else Decimal(0),
        exit_multiple=log.pick("exit_multiple", None if a.get("exit_multiple") is None else num(a["exit_multiple"]),
                               Decimal(6)) if method == "exit_multiple" else Decimal(0),
        mid_year=log.pick("mid_year", a.get("mid_year"), True),
    )
    return base, assumptions, run_dcf(base.adjusted_ebitda, base.revenue, assumptions), log


def dcf_tool(ws: Workspace, a: dict) -> dict:
    base, assumptions, r, log = build_dcf(ws, a)
    return {
        "currency": ws.currency,
        "enterprise_value": r.enterprise_value, "sum_of_pv_cash_flows": r.sum_of_pv,
        "terminal_value": r.terminal_value, "pv_of_terminal_value": r.pv_terminal,
        "terminal_share_of_ev_pct": pct_out(r.terminal_share_of_ev, 1),
        "implied_ev_to_ltm_adjusted_ebitda_x": r.implied_ev_to_ltm_ebitda,
        "ltm_adjusted_ebitda": base.adjusted_ebitda,
        "years": [{"year": y.ops.year, "revenue": y.ops.revenue, "ebitda": y.ops.ebitda,
                   "tax_on_ebit": y.tax_on_ebit, "capex": y.ops.capex, "nwc_change": y.ops.nwc_change,
                   "unlevered_fcf": y.unlevered_fcf, "discount_factor": y.discount_factor,
                   "present_value": y.present_value} for y in r.years],
        "assumptions": assumptions_out(log),
        "caveats": r.warnings + ([f"{base.pending_candidates} adjustment candidate(s) pending review"]
                                 if base.pending_candidates else []),
    }


SENSITIVITY_METRICS = {
    "lbo": {"irr_pct": lambda r: pct_out(r.irr), "moic_x": lambda r: r.moic, "exit_equity": lambda r: r.exit_equity},
    "dcf": {"enterprise_value": lambda r: r.enterprise_value,
            "implied_ev_to_ltm_adjusted_ebitda_x": lambda r: r.implied_ev_to_ltm_ebitda},
}


def sensitivity_tool(ws: Workspace, a: dict) -> dict:
    model = a["model"]
    props = LBO_PROPS if model == "lbo" else DCF_PROPS
    metrics = SENSITIVITY_METRICS[model]
    metric = a.get("metric", "irr_pct" if model == "lbo" else "enterprise_value")
    if metric not in metrics:
        raise ToolInputError(f"metric for {model} must be one of: {', '.join(metrics)}")
    base_inputs = dict(a.get("base", {}))
    validate(base_inputs, {"type": "object", "properties": props}, "base")
    numeric = {k for k, v in props.items() if v.get("type") in ("number", "integer") or "anyOf" in v}
    for axis in ("x", "y"):
        if a[axis]["param"] not in numeric:
            raise ToolInputError(f"{axis}.param must be a numeric {model} input: {', '.join(sorted(numeric))}")
    if a["x"]["param"] == a["y"]["param"]:
        raise ToolInputError("x and y must vary different inputs")
    build = build_lbo if model == "lbo" else build_dcf
    required = "entry_multiple" if model == "lbo" else "wacc_pct"
    if required not in base_inputs and required not in (a["x"]["param"], a["y"]["param"]):
        raise ToolInputError(f"base must include {required} (or vary it on an axis)")

    grid = []
    for yv in a["y"]["values"]:
        row = []
        for xv in a["x"]["values"]:
            inputs = {**base_inputs, a["x"]["param"]: xv, a["y"]["param"]: yv}
            validate(inputs, {"type": "object", "properties": props}, "inputs")
            try:
                row.append(metrics[metric](build(ws, inputs)[2]))
            except ModelError as e:
                row.append(f"n/a: {e}")
        grid.append({a["y"]["param"]: num(yv), "values": row})
    return {"model": model, "metric": metric, "x_param": a["x"]["param"],
            "x_values": [num(v) for v in a["x"]["values"]], "y_param": a["y"]["param"],
            "rows": grid, "base_inputs": base_inputs}


# ------------------------------------------------------------------ calculator and export

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _evaluate(node) -> Decimal:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    raise ToolInputError("only numbers, + - * / and parentheses are allowed")


def calculator_tool(ws: Workspace, a: dict) -> dict:
    expression = a["expression"].replace(",", "")
    try:
        result = _evaluate(ast.parse(expression, mode="eval"))
    except SyntaxError:
        raise ToolInputError(f"cannot parse {a['expression']!r}") from None
    except (DivisionByZero, InvalidOperation, ZeroDivisionError):
        raise ToolInputError("division by zero") from None
    places = int(a.get("decimals", 2))
    if not 0 <= places <= 10:
        raise ToolInputError("decimals must be between 0 and 10")
    return {"expression": a["expression"], "result": result.quantize(Decimal(10) ** -places)}


def export_tool(ws: Workspace, a: dict) -> dict:
    from .model_export import cashflow_workbook, lbo_workbook

    model, inputs = a["model"], dict(a.get("inputs", {}))
    if model == "lbo":
        validate(inputs, obj(LBO_PROPS, ["entry_multiple"]), "inputs")
        base, assumptions, result, log = build_lbo(ws, inputs)
        data = lbo_workbook(ws, assumptions, result)
    else:
        validate(inputs, obj(CASHFLOW_PROPS), "inputs")
        data = cashflow_workbook(ws, build_forecast(ws, inputs))
    digest = hashlib.sha256(data).hexdigest()
    tag = hashlib.sha256(canonical(inputs).encode()).hexdigest()[:8]
    relative = Path("exports") / f"{model}-{tag}.xlsx"
    target = ws.out_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"model": model, "path": relative.as_posix(), "sha256": digest,
            "note": "the workbook uses live Excel formulas, so assumptions can be changed in Excel"}


CASHFLOW_PROPS = {
    "as_of": {**DATE, "description": "forecast start, YYYY-MM-DD (default: last ledger date); a past date back-tests against the bank"},
    "sales_growth_pct": {**NUM, "description": "uplift % on the 13-week sales and purchase run-rate (default 0)"},
    "collection_days": {**NUM, "description": "override days sales outstanding (default: derived)"},
    "payment_days": {**NUM, "description": "override days payables outstanding (default: derived)"},
    "min_cash": {**NUM, "description": "minimum cash threshold that flags a week (default 0)"},
}

AXIS = {"type": "object", "required": ["param", "values"], "properties": {
    "param": {"type": "string"}, "values": {"type": "array", "items": NUM, "minItems": 2, "maxItems": 7}}}

TOOLS = [
    Tool("get_financials",
         "P&L summary for a period: revenue, gross profit, reported EBITDA, accepted adjustments by rule, "
         "adjusted EBITDA, net income, margins, and how many adjustments are still pending review.",
         obj({"period": PERIOD}), get_financials),
    Tool("monthly_series", "One metric month by month (P&L totals or month-end balances).",
         obj({"metric": {"type": "string", "enum": SERIES}, "start": MONTH, "end": MONTH}, ["metric"]), monthly_series),
    Tool("list_adjustments",
         "EBITDA adjustment candidates found by the rules, with status (pending/accepted/rejected), "
         "proposed and accepted effects, and source rows.",
         obj({"rule": {"type": "string", "enum": ["OWN", "REL", "CUT", "UNU"]},
              "status": {"type": "string", "enum": ["pending", "accepted", "rejected"]},
              "limit": {"type": "integer"}}), list_adjustments),
    Tool("explain_adjustment", "Full evidence for one candidate: why it was flagged and every ledger line of its journal.",
         obj({"candidate_id": {"type": "string"}}, ["candidate_id"]), explain_adjustment),
    Tool("proof_of_cash", "Monthly book cash vs bank cash movements and which months do not tie.", obj({}), proof_of_cash),
    Tool("run_checks", "Data-quality and blocker checks with results.", obj({}), run_checks),
    Tool("nwc_analysis",
         "Net working capital by month with DSO, DIO, DPO and cash conversion days, and the NWC peg "
         "(average over a trailing window, optionally excluding months).",
         obj({"peg_months": {"type": "integer"}, "exclude_months": {"type": "array", "items": MONTH, "maxItems": 12},
              "include_monthly": {"type": "boolean"}}), nwc_tool),
    Tool("customer_analysis",
         "Revenue by customer for the last 12 months: shares, top-1/5/10 concentration, HHI, growth vs prior 12 months, "
         "new and lost customers.", obj({"top_n": {"type": "integer"}}), customers_tool),
    Tool("credit_metrics",
         "Leverage, interest cover, DSCR and covenant headroom on LTM adjusted EBITDA. Defaults use the balance sheet "
         "debt and cash and the P&L interest; pass proposed debt and rate to test a new facility.",
         obj({"total_debt": NUM, "cash": NUM, "interest_rate_pct": NUM, "annual_principal": NUM,
              "max_net_leverage": NUM, "min_interest_cover": NUM}), credit_tool),
    Tool("purchase_price_bridge",
         "Enterprise value to equity purchase price: less debt, plus cash, less debt-like items, plus cash-like items, "
         "plus NWC at close minus the peg.",
         obj({"ev_multiple": NUM, "enterprise_value": NUM, "closing_month": MONTH, "peg": NUM,
              "debt_like_items": ITEMS, "cash_like_items": ITEMS}), bridge_tool),
    Tool("cash_flow_13_week",
         "13-week direct cash flow forecast: weekly receipts and payments by line, opening and closing cash, "
         "lowest week and minimum-cash breaches. With a past as_of, compares each week to actual bank movements.",
         obj(CASHFLOW_PROPS), cashflow_tool),
    Tool("lbo_model",
         "Leveraged buyout: sources and uses, senior and subordinated debt schedule with amortisation and cash sweep, "
         "yearly credit stats, exit, IRR and money multiple, and the value-creation bridge. Unset inputs use stated defaults.",
         obj(LBO_PROPS, ["entry_multiple"]), lbo_tool),
    Tool("dcf_valuation",
         "Unlevered DCF: projected free cash flows discounted at the WACC, terminal value by Gordon growth or exit "
         "multiple, enterprise value and implied EV/EBITDA.", obj(DCF_PROPS, ["wacc_pct"]), dcf_tool),
    Tool("sensitivity",
         "Two-way sensitivity grid for the LBO or DCF: vary any two numeric inputs (2-7 values each) and report one metric "
         "(lbo: irr_pct, moic_x, exit_equity; dcf: enterprise_value, implied_ev_to_ltm_adjusted_ebitda_x).",
         obj({"model": {"type": "string", "enum": ["lbo", "dcf"]}, "metric": {"type": "string"},
              "base": {"type": "object"}, "x": AXIS, "y": AXIS}, ["model", "x", "y"]), sensitivity_tool),
    Tool("calculator",
         "Exact decimal arithmetic on numbers taken from earlier tool results (+ - * / and parentheses). "
         "Use it for any figure not returned directly by a tool.",
         obj({"expression": {"type": "string", "maxLength": 300}, "decimals": {"type": "integer"}}, ["expression"]),
         calculator_tool),
    Tool("export_model",
         "Write the LBO or 13-week cash flow as an Excel workbook with live formulas; returns its path and fingerprint.",
         obj({"model": {"type": "string", "enum": ["lbo", "cash_flow_13_week"]}, "inputs": {"type": "object"}},
             ["model"]), export_tool),
]
