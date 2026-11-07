"""Excel workbooks for the LBO and the 13-week cash flow.

Assumptions are input cells; every calculated cell is a live formula, rounding
to cents exactly where the Python model does. So the workbook gives the same
answers as the tool, and a reviewer can change an assumption in Excel and watch
the model recalculate.

Rounding is written ROUND(ROUND(x, 6), 2). Spreadsheets use binary floating
point, so a true half-cent such as 548389.365 can arrive as 548389.364999999
and round the wrong way. Rates and multiples are held to 4 decimal places, so
x has at most 6 exact decimals: the inner ROUND snaps away the float noise and
the outer one rounds half away from zero, matching Python's exact arithmetic.
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter as col

from .cashflow import CashFlowForecast
from .databook import BOLD, FIXED_TIME, HEADER_FILL, MONEY, PERCENT, TITLE, TOTAL_FILL, _reproducible_zip, money_cell
from .lbo import LBOAssumptions, LBOResult
from .workspace import Workspace

INPUT_FONT = Font(color="1F4E9E")  # blue = input, the usual modelling convention


def _save(wb: Workbook) -> bytes:
    wb.properties.creator = "Apex Diligence"
    wb.properties.created = wb.properties.modified = FIXED_TIME
    buffer = io.BytesIO()
    wb.save(buffer)
    return _reproducible_zip(buffer.getvalue())


def _input(ws, ref: str, value, fmt: str) -> None:
    ws[ref] = value
    ws[ref].number_format = fmt
    ws[ref].font = INPUT_FONT


def lbo_workbook(space: Workspace, a: LBOAssumptions, r: LBOResult) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "LBO"
    n = a.operating.years
    last = 2 + n  # column B is year 0, projection years run C onwards
    L = col(last)

    ws["A1"], ws["A1"].font = "Leveraged buyout model", TITLE
    ws["A2"] = f"{space.result.company} · {space.currency} · blue cells are inputs"

    ws["A4"], ws["A4"].font = "Assumptions", BOLD
    rows = [
        (5, "LTM revenue", r.base_revenue, MONEY), (6, "LTM adjusted EBITDA", r.base_ebitda, MONEY),
        (7, "Entry multiple (x EBITDA)", a.entry_multiple, "0.00x"), (8, "Exit multiple (x EBITDA)", a.exit_multiple, "0.00x"),
        (9, "Senior debt (x EBITDA)", a.senior_multiple, "0.00x"), (10, "Senior interest rate", a.senior_rate, PERCENT),
        (11, "Senior amortisation (% of original, per year)", a.senior_amortisation, PERCENT),
        (12, "Cash sweep (% of spare cash)", a.cash_sweep, PERCENT),
        (13, "Subordinated debt (x EBITDA)", a.sub_multiple, "0.00x"), (14, "Subordinated interest rate", a.sub_rate, PERCENT),
        (15, "Transaction fees (% of EV)", a.fees_pct, PERCENT), (16, "EBITDA margin", a.operating.ebitda_margin, "0.0000%"),
        (17, "D&A (% of revenue)", a.operating.da_pct_revenue, "0.0000%"),
        (18, "Capex (% of revenue)", a.operating.capex_pct_revenue, "0.0000%"),
        (19, "NWC (% of revenue)", a.operating.nwc_pct_revenue, "0.0000%"), (20, "Tax rate", a.operating.tax_rate, PERCENT),
    ]
    for row, label, value, fmt in rows:
        ws.cell(row=row, column=1, value=label)
        _input(ws, f"B{row}", money_value(value) if fmt == MONEY else value, fmt)

    ws["A22"], ws["A22"].font = "Sources and uses", BOLD
    formulas = [(23, "Entry enterprise value", "=ROUND(ROUND(B6*B7,6),2)"), (24, "Transaction fees", "=ROUND(ROUND(B23*B15,6),2)"),
                (25, "Senior debt", "=ROUND(ROUND(B6*B9,6),2)"), (26, "Subordinated debt", "=ROUND(ROUND(B6*B13,6),2)"),
                (27, "Sponsor equity", "=B23+B24-B25-B26")]
    for row, label, formula in formulas:
        ws.cell(row=row, column=1, value=label)
        money_cell(ws, f"B{row}", formula)
    ws["A27"].font = ws["B27"].font = BOLD

    header = 29
    ws.cell(row=header, column=1, value="Year").font = BOLD
    ws.cell(row=header, column=2, value="0 (LTM)")
    for t in range(1, n + 1):
        ws.cell(row=header, column=2 + t, value=t)
    for c in range(1, last + 1):
        ws.cell(row=header, column=c).font, ws.cell(row=header, column=c).fill = BOLD, HEADER_FILL

    labels = {30: "Revenue growth", 31: "Revenue", 32: "EBITDA", 33: "D&A", 34: "EBIT", 35: "Capex", 36: "NWC",
              37: "Change in NWC", 38: "Senior debt, opening", 39: "Subordinated debt", 40: "Interest",
              41: "Tax", 42: "Free cash flow", 43: "Mandatory repayment", 44: "Cash sweep",
              45: "Senior debt, closing", 46: "Cash, closing", 47: "Net debt", 48: "Net debt / EBITDA",
              49: "EBITDA / interest"}
    for row, label in labels.items():
        ws.cell(row=row, column=1, value=label)
    money_cell(ws, "B31", "=B5")
    money_cell(ws, "B32", "=B6")
    money_cell(ws, "B36", "=ROUND(ROUND(B31*$B$19,6),2)")
    money_cell(ws, "B45", "=B25")
    money_cell(ws, "B46", 0)
    money_cell(ws, "B47", "=B45+B26-B46")
    for t, growth in enumerate(a.operating.revenue_growth, start=1):
        c, p = col(2 + t), col(1 + t)
        _input(ws, f"{c}30", growth, PERCENT)
        cells = {
            31: f"=ROUND(ROUND({p}31*(1+{c}30),6),2)", 32: f"=ROUND(ROUND({c}31*$B$16,6),2)", 33: f"=ROUND(ROUND({c}31*$B$17,6),2)",
            34: f"={c}32-{c}33", 35: f"=ROUND(ROUND({c}31*$B$18,6),2)", 36: f"=ROUND(ROUND({c}31*$B$19,6),2)",
            37: f"={c}36-{p}36", 38: f"={p}45", 39: "=$B$26",
            40: f"=ROUND(ROUND({c}38*$B$10,6),2)+ROUND(ROUND({c}39*$B$14,6),2)", 41: f"=MAX(0,ROUND(ROUND(({c}34-{c}40)*$B$20,6),2))",
            42: f"={c}32-{c}35-{c}37-{c}41-{c}40", 43: f"=MIN(ROUND(ROUND($B$25*$B$11,6),2),{c}38)",
            44: f"=MIN(MAX(0,ROUND(ROUND(({p}46+{c}42-{c}43)*$B$12,6),2)),{c}38-{c}43)",
            45: f"={c}38-{c}43-{c}44", 46: f"={p}46+{c}42-{c}43-{c}44", 47: f"={c}45+{c}39-{c}46",
        }
        for row, formula in cells.items():
            money_cell(ws, f"{c}{row}", formula)
        ws[f"{c}48"] = f'=IF({c}32>0,{c}47/{c}32,"n/a")'
        ws[f"{c}49"] = f'=IF({c}40>0,{c}32/{c}40,"n/a")'
        ws[f"{c}48"].number_format = ws[f"{c}49"].number_format = "0.00x"
    for row in (32, 42, 47):
        for c in range(1, last + 1):
            ws.cell(row=row, column=c).fill = TOTAL_FILL

    ws["A51"], ws["A51"].font = "Returns", BOLD
    returns = [(52, "Exit enterprise value", f"=ROUND(ROUND({L}32*B8,6),2)"), (53, "Exit net debt", f"={L}47"),
               (54, "Exit equity", "=B52-B53")]
    for row, label, formula in returns:
        ws.cell(row=row, column=1, value=label)
        money_cell(ws, f"B{row}", formula)
    ws["A55"], ws["B55"] = "Money multiple (MOIC)", "=B54/B27"
    ws["B55"].number_format = "0.00x"
    ws["A56"], ws["B56"] = "IRR", f'=IF(B54<=0,"n/a",(B54/B27)^(1/{n})-1)'
    ws["B56"].number_format = "0.00%"
    ws["A56"].font = ws["B56"].font = BOLD

    ws["A58"], ws["A58"].font = "Value creation bridge", BOLD
    bridge = [(59, "EBITDA growth", f"=ROUND(ROUND({L}32*B7,6),2)-B23"), (60, "Multiple change", f"=B52-ROUND(ROUND({L}32*B7,6),2)"),
              (61, "Debt paydown and cash", "=(B25+B26)-B53"), (62, "Fees", "=-B24"), (63, "Equity gain", "=B54-B27")]
    for row, label, formula in bridge:
        ws.cell(row=row, column=1, value=label)
        money_cell(ws, f"B{row}", formula)
    ws["A63"].font = ws["B63"].font = BOLD

    ws.column_dimensions["A"].width = 44
    for c in range(2, last + 1):
        ws.column_dimensions[col(c)].width = 16
    ws.freeze_panes = "B30"
    return _save(wb)


def money_value(cents: int):
    from .money import to_decimal
    return to_decimal(cents)


def cashflow_workbook(space: Workspace, f: CashFlowForecast) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "13-week cash flow"
    weeks = f.weeks
    W = len(weeks)
    total_col = 2 + W
    T = col(total_col)
    ws["A1"], ws["A1"].font = "13-week cash flow forecast", TITLE
    ws["A2"] = f"{space.result.company} · {space.currency} · as of {f.as_of.isoformat()} · direct method"

    ws.cell(row=4, column=1, value="Week").font = BOLD
    ws.cell(row=5, column=1, value="Week ending")
    for w in weeks:
        c = 1 + w.number
        ws.cell(row=4, column=c, value=w.number)
        ws.cell(row=5, column=c, value=w.end).number_format = "yyyy-mm-dd"
    ws.cell(row=4, column=total_col, value="Total")
    for c in range(1, total_col + 1):
        ws.cell(row=4, column=c).font, ws.cell(row=4, column=c).fill = BOLD, HEADER_FILL

    row = 6
    ws.cell(row=row, column=1, value="Opening cash").font = BOLD
    opening_row = row
    row += 1
    ws.cell(row=row, column=1, value="Receipts").font = BOLD
    row += 1
    receipt_rows = []
    for name in f.receipt_lines:
        ws.cell(row=row, column=1, value=f"  {name}")
        for w in weeks:
            money_cell(ws, f"{col(1 + w.number)}{row}", w.receipts[name])
        receipt_rows.append(row)
        row += 1
    total_receipts = row
    ws.cell(row=row, column=1, value="Total receipts").font = BOLD
    row += 1
    ws.cell(row=row, column=1, value="Payments").font = BOLD
    row += 1
    payment_rows = []
    for name in f.payment_lines:
        ws.cell(row=row, column=1, value=f"  {name}")
        for w in weeks:
            money_cell(ws, f"{col(1 + w.number)}{row}", w.payments[name])
        payment_rows.append(row)
        row += 1
    total_payments = row
    ws.cell(row=row, column=1, value="Total payments").font = BOLD
    net_row, closing_row, min_row, headroom_row, flag_row = row + 1, row + 2, row + 3, row + 4, row + 5
    ws.cell(row=net_row, column=1, value="Net cash flow").font = BOLD
    ws.cell(row=closing_row, column=1, value="Closing cash").font = BOLD
    ws.cell(row=min_row, column=1, value="Minimum cash")
    ws.cell(row=headroom_row, column=1, value="Headroom over minimum")
    ws.cell(row=flag_row, column=1, value="Below minimum?")
    _input(ws, f"B{min_row}", money_value(f.min_cash), MONEY)

    for w in weeks:
        c, p = col(1 + w.number), col(w.number)
        money_cell(ws, f"{c}{opening_row}", money_value(f.opening_cash) if w.number == 1 else f"={p}{closing_row}")
        if w.number == 1:
            ws[f"{c}{opening_row}"].font = INPUT_FONT
        money_cell(ws, f"{c}{total_receipts}", f"=SUM({c}{receipt_rows[0]}:{c}{receipt_rows[-1]})")
        money_cell(ws, f"{c}{total_payments}", f"=SUM({c}{payment_rows[0]}:{c}{payment_rows[-1]})")
        money_cell(ws, f"{c}{net_row}", f"={c}{total_receipts}-{c}{total_payments}")
        money_cell(ws, f"{c}{closing_row}", f"={c}{opening_row}+{c}{net_row}")
        if w.number > 1:
            money_cell(ws, f"{c}{min_row}", f"=$B${min_row}")
        money_cell(ws, f"{c}{headroom_row}", f"={c}{closing_row}-{c}{min_row}")
        ws[f"{c}{flag_row}"] = f'=IF({c}{closing_row}<{c}{min_row},"BELOW","")'
    for r in receipt_rows + payment_rows + [total_receipts, total_payments, net_row]:
        money_cell(ws, f"{T}{r}", f"=SUM(B{r}:{col(1 + W)}{r})")
    for r in (total_receipts, total_payments, net_row, closing_row):
        for c in range(1, total_col + 1):
            ws.cell(row=r, column=c).fill = TOTAL_FILL

    row = flag_row + 2
    if f.backtest:
        ws.cell(row=row, column=1, value="Actual net cash flow (bank)").font = BOLD
        ws.cell(row=row + 1, column=1, value="Variance (actual - forecast)")
        for w in weeks:
            c = col(1 + w.number)
            if w.actual_net is not None:
                money_cell(ws, f"{c}{row}", w.actual_net)
                money_cell(ws, f"{c}{row + 1}", f"={c}{row}-{c}{net_row}")
        row += 3

    ws.cell(row=row, column=1, value="Assumptions").font = BOLD
    notes = [
        ("Opening cash source", f.opening_source),
        ("Collection days (DSO)", f"{f.collection_days} days, collected after {f.collection_lag_weeks} week(s)"),
        ("Payment days (DPO)", f"{f.payment_days} days, paid after {f.payment_lag_weeks} week(s)"),
        ("Weekly sales run-rate", str(money_value(f.weekly_sales))),
        ("Weekly purchases run-rate", str(money_value(f.weekly_purchases))),
    ] + [(f"{s.account_name} – {s.counterparty}", f"{s.cadence}: {money_value(s.amount)}"
          + (f" on day {s.day_of_month}" if s.day_of_month else "")) for s in f.streams]
    for i, (k, v) in enumerate(notes, start=row + 1):
        ws.cell(row=i, column=1, value=k)
        ws.cell(row=i, column=2, value=v)

    ws.column_dimensions["A"].width = 40
    for c in range(2, total_col + 1):
        ws.column_dimensions[col(c)].width = 14
    ws.freeze_panes = "B6"
    return _save(wb)
