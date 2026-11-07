"""13-week cash flow forecast (direct method: receipts and disbursements by week).

How each line is forecast, from the company's own ledger:
- Opening cash: the bank's running balance at the as-of date (or book cash if the
  statement has no balance column).
- Collections: open receivables are collected over the company's collection period
  (days sales outstanding, from the last 13 weeks); new sales at the last-13-week
  run-rate are collected after the same lag. Revenue a reviewer has rejected as a
  cut-off problem (accepted CUT adjustments) is removed from collectible receivables.
- Supplier payments: the same method using payables and payment days.
- Every other payment stream (one counterparty on one account) is scheduled by its own
  pattern over the last 6 months:
    monthly   paid in at least 5 of the last 6 months -> same day of month, median amount
    quarterly paid twice, 3 months apart             -> every 3 months on the same day
    other     -> weekly average of the last 13 weeks
  Payments flagged as one-off costs (UNU candidates not rejected by the reviewer) are left out.
- If the as-of date is in the past, actual bank movements are shown beside each forecast
  week, so the forecast can be back-tested.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median_low

from .money import Money, fmt, mul, ratio
from .statements import month_of, month_range
from .workspace import ModelError, Workspace, check_range

WEEKS = 13
LOOKBACK_DAYS = 91  # 13 weeks


@dataclass(frozen=True)
class Stream:
    account_code: str
    account_name: str
    counterparty: str
    cadence: str            # "monthly", "quarterly" or "weekly average"
    amount: Money           # per occurrence (or per week for weekly average)
    day_of_month: int | None


@dataclass(frozen=True)
class Week:
    number: int
    start: date
    end: date
    opening: Money
    receipts: dict[str, Money]
    payments: dict[str, Money]
    net: Money
    closing: Money
    below_minimum: bool
    actual_net: Money | None
    variance: Money | None   # actual - forecast


@dataclass(frozen=True)
class CashFlowForecast:
    as_of: date
    opening_cash: Money
    opening_source: str
    book_cash: Money
    min_cash: Money
    collection_days: Decimal
    payment_days: Decimal
    collection_lag_weeks: int
    payment_lag_weeks: int
    weekly_sales: Money
    weekly_purchases: Money
    collectible_receivables: Money
    excluded_receivables: Money
    opening_payables: Money
    streams: list[Stream]
    excluded_one_offs: list[str]
    not_forecast: dict[str, Money]
    weeks: list[Week]
    receipt_lines: list[str]
    payment_lines: list[str]
    lowest_week: int
    lowest_closing: Money
    weeks_below_minimum: list[int]
    backtest: dict | None   # forecast vs actual over the weeks the bank statement covers


def _div(cents: int, n: int) -> int:
    return int((Decimal(cents) / n).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _split(cents: int, n: int) -> list[int]:
    """Split into n whole-cent parts that add back exactly; the remainder goes to the first parts."""
    base, remainder = divmod(abs(cents), n)
    parts = [base + (1 if i < remainder else 0) for i in range(n)]
    return [p if cents >= 0 else -p for p in parts]


def _round_days_to_weeks(days: Decimal) -> int:
    weeks = int((days / 7).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(0, min(WEEKS, weeks))


def _allocate(amount: int, weights: list[int]) -> list[int]:
    """Allocate amount across weights in proportion, exactly, largest remainder first."""
    total = sum(weights)
    raw = [Decimal(amount) * w / total for w in weights]
    parts = [int(r) for r in raw]
    leftover = amount - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - parts[i]), i))
    for i in order[:leftover]:
        parts[i] += 1
    return parts


def forecast_13_weeks(ws: Workspace, *, as_of: date | None = None, sales_growth: Decimal = Decimal(0),
                      collection_days: Decimal | None = None, payment_days: Decimal | None = None,
                      min_cash: int = 0) -> CashFlowForecast:
    gl, bank, mapping = ws.data.gl, ws.data.bank, ws.data.mapping
    first_date, last_date = min(l.date for l in gl), max(l.date for l in gl)
    as_of = as_of or last_date
    if as_of > last_date:
        raise ModelError(f"as_of {as_of} is after the last ledger date {last_date}")
    if (as_of - first_date).days < LOOKBACK_DAYS:
        raise ModelError(f"as_of needs at least {LOOKBACK_DAYS} days of history before it; the ledger starts {first_date}")
    check_range("sales_growth", sales_growth, "-0.9", 2)
    if collection_days is not None:
        check_range("collection_days", collection_days, 0, 365)
    if payment_days is not None:
        check_range("payment_days", payment_days, 0, 365)

    category = lambda code: mapping[code]
    upto = [l for l in gl if l.date <= as_of]
    window_start = as_of - timedelta(days=LOOKBACK_DAYS)
    in_window = lambda d: window_start < d <= as_of

    # ---- opening cash
    book_cash = sum(l.net for l in upto if category(l.account_code) == "asset_cash")
    bank_with_balance = [b for b in bank if b.balance is not None and b.date <= as_of]
    if bank_with_balance:
        opening, source = bank_with_balance[-1].balance, "bank statement balance"
    else:
        opening, source = book_cash, "book cash (statement has no running balance)"

    # ---- receivables and sales
    revenue_window = -sum(l.net for l in upto if category(l.account_code) == "revenue" and in_window(l.date))
    receivables = sum(l.net for l in upto if category(l.account_code) == "asset_ar")
    excluded = sum(r.candidate.amount_involved for r in ws.result.reviewed
                   if r.candidate.rule == "CUT" and r.status == "accepted" and r.candidate.date <= as_of)
    collectible = max(0, receivables - excluded)
    if collection_days is None:
        collection_days = ratio(collectible * LOOKBACK_DAYS, revenue_window, 1) if revenue_window > 0 else Decimal(0)
    weekly_sales = mul(_div(revenue_window, WEEKS), 1 + sales_growth)

    # ---- payables and purchases
    ap_accounts = {c for c, cat in mapping.items() if cat == "liability_ap"}
    purchases_window = sum(l.credit for l in upto if l.account_code in ap_accounts and in_window(l.date))
    payables = max(0, -sum(l.net for l in upto if l.account_code in ap_accounts))
    if payment_days is None:
        payment_days = ratio(payables * LOOKBACK_DAYS, purchases_window, 1) if purchases_window > 0 else Decimal(0)
    weekly_purchases = mul(_div(purchases_window, WEEKS), 1 + sales_growth)
    lag_c, lag_p = _round_days_to_weeks(Decimal(collection_days)), _round_days_to_weeks(Decimal(payment_days))

    # ---- other payment streams, from cash-paying journals in the last 6 months
    one_off_sources = {e for r in ws.result.reviewed if r.candidate.rule == "UNU" and r.status != "rejected"
                       for e in r.candidate.evidence}
    journals = defaultdict(list)
    for l in upto:
        journals[l.journal_id].append(l)
    months6 = month_range(_add_months(month_of(as_of), -5), month_of(as_of))
    payments: dict[tuple, list[tuple[date, int]]] = defaultdict(list)
    names: dict[tuple, str] = {}
    excluded_one_offs, not_forecast = [], defaultdict(int)
    for jid, lines in journals.items():
        cash_lines = [l for l in lines if category(l.account_code) == "asset_cash"]
        if not cash_lines or any(l.entry_type == "opening" for l in lines):
            continue
        outflow = -sum(l.net for l in cash_lines)
        when = cash_lines[0].date
        if outflow <= 0 or month_of(when) not in months6:
            continue
        others = [l for l in lines if category(l.account_code) != "asset_cash" and l.net > 0]
        if not others:
            continue
        if any(l.source in one_off_sources for l in lines):
            excluded_one_offs.append(f"{jid} {when} {others[0].description} ({fmt(outflow)})")
            continue
        for line, part in zip(others, _allocate(outflow, [l.net for l in others])):
            cat = category(line.account_code)
            if cat == "liability_ap":
                continue  # supplier payments are forecast from payables
            if cat in ("asset_ar", "asset_inventory", "asset_other", "liability_debt", "liability_other", "equity"):
                if in_window(when):
                    not_forecast[f"{line.account_name or line.account_code} (balance sheet)"] += part
                continue
            key = (line.account_code, line.counterparty)
            names[key] = line.account_name or line.account_code
            payments[key].append((when, part))

    horizon_end = as_of + timedelta(days=7 * WEEKS)
    streams: list[Stream] = []
    scheduled: list[tuple[date, str, int]] = []  # (date, line name, amount)
    for key in sorted(payments):
        items = sorted(payments[key])
        by_month: dict[str, int] = defaultdict(int)
        for d, amt in items:
            by_month[month_of(d)] += amt
        months = sorted(by_month)
        line_name = names[key]
        if len(months) >= 5:
            recent = months[-3:]
            amount = median_low([by_month[m] for m in recent])
            day = median_low([d.day for d, _ in items if month_of(d) in recent])
            streams.append(Stream(key[0], line_name, key[1], "monthly", Money(amount), day))
            for m in month_range(month_of(as_of), month_of(horizon_end)):
                when = _on_day(m, day)
                if as_of < when <= horizon_end:
                    scheduled.append((when, line_name, amount))
        elif len(months) == 2 and _months_apart(months[0], months[1]) == 3:
            amount = median_low([by_month[m] for m in months])
            day = median_low([d.day for d, _ in items])
            streams.append(Stream(key[0], line_name, key[1], "quarterly", Money(amount), day))
            m = _add_months(months[1], 3)
            while _on_day(m, day) <= horizon_end:
                if _on_day(m, day) > as_of:
                    scheduled.append((_on_day(m, day), line_name, amount))
                m = _add_months(m, 3)
        else:
            recent_total = sum(a for d, a in items if in_window(d))
            if recent_total:
                weekly = _div(recent_total, WEEKS)
                streams.append(Stream(key[0], line_name, key[1], "weekly average", Money(weekly), None))
                for k in range(WEEKS):
                    scheduled.append((as_of + timedelta(days=7 * k + 1), line_name, weekly))

    # ---- build the weeks
    receipt_lines = ["Collections: opening receivables", "Collections: new sales"]
    payment_lines = ["Supplier payments"] + sorted({name for _, name, _ in scheduled})
    collect_open = _split(collectible, max(1, lag_c))
    pay_open = _split(payables, max(1, lag_p))
    bank_last = max((b.date for b in bank), default=None)

    weeks, balance = [], opening
    for k in range(1, WEEKS + 1):
        start, end = as_of + timedelta(days=7 * (k - 1) + 1), as_of + timedelta(days=7 * k)
        receipts = {
            receipt_lines[0]: collect_open[k - 1] if k <= len(collect_open) else 0,
            receipt_lines[1]: weekly_sales if k > lag_c else 0,
        }
        pays = {name: 0 for name in payment_lines}
        pays["Supplier payments"] = (pay_open[k - 1] if k <= len(pay_open) else 0) + (weekly_purchases if k > lag_p else 0)
        for when, name, amount in scheduled:
            if start <= when <= end:
                pays[name] += amount
        net = sum(receipts.values()) - sum(pays.values())
        actual = variance = None
        if bank_last is not None and end <= bank_last:
            actual = sum(b.amount for b in bank if start <= b.date <= end)
            variance = actual - net
        closing = balance + net
        weeks.append(Week(k, start, end, Money(balance), {n: Money(v) for n, v in receipts.items()},
                          {n: Money(v) for n, v in pays.items()}, Money(net), Money(closing),
                          closing < min_cash, None if actual is None else Money(actual),
                          None if variance is None else Money(variance)))
        balance = closing

    lowest = min(weeks, key=lambda w: (w.closing, w.number))
    covered = [w for w in weeks if w.actual_net is not None]
    backtest = None
    if covered:
        forecast_total = sum(w.net for w in covered)
        actual_total = sum(w.actual_net for w in covered)
        backtest = {
            "weeks_covered": len(covered),
            "forecast_net": Money(forecast_total),
            "actual_net": Money(actual_total),
            "cumulative_variance": Money(actual_total - forecast_total),
            "forecast_closing": covered[-1].closing,
            "actual_closing": Money(opening + actual_total),
            "variance_pct_of_opening_cash": ratio((actual_total - forecast_total) * 100, opening, 1) if opening else None,
        }
    return CashFlowForecast(
        as_of=as_of, opening_cash=Money(opening), opening_source=source, book_cash=Money(book_cash),
        min_cash=Money(min_cash), collection_days=Decimal(collection_days), payment_days=Decimal(payment_days),
        collection_lag_weeks=lag_c, payment_lag_weeks=lag_p, weekly_sales=Money(weekly_sales),
        weekly_purchases=Money(weekly_purchases), collectible_receivables=Money(collectible),
        excluded_receivables=Money(excluded), opening_payables=Money(payables), streams=streams,
        excluded_one_offs=excluded_one_offs, not_forecast={k: Money(v) for k, v in sorted(not_forecast.items())},
        weeks=weeks, receipt_lines=receipt_lines, payment_lines=payment_lines,
        lowest_week=lowest.number, lowest_closing=lowest.closing,
        weeks_below_minimum=[w.number for w in weeks if w.below_minimum],
        backtest=backtest,
    )


def _on_day(month: str, day: int) -> date:
    year, m = int(month[:4]), int(month[5:])
    return date(year, m, min(day, calendar.monthrange(year, m)[1]))


def _add_months(month: str, n: int) -> str:
    year, m = int(month[:4]), int(month[5:]) - 1 + n
    return f"{year + m // 12:04d}-{m % 12 + 1:02d}"


def _months_apart(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:]) - int(a[5:])
