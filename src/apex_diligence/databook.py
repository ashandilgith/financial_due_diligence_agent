"""The Excel databook.

Base numbers (revenue, costs, balances) are written as values. Everything
derived from them (subtotals, EBITDA, FY and LTM totals, differences) is
written as a live Excel formula, so a reviewer can trace and change it.

The file is also made byte-for-byte reproducible: fixed document dates and
fixed zip timestamps, so two runs on the same inputs produce identical files.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from . import __version__
from .money import to_decimal
from .pipeline import RunResult
from .taxonomy import LABELS

MONEY = '#,##0.00;(#,##0.00);"-"'
PERCENT = "0.0%"
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=14)
HEADER_FILL = PatternFill("solid", fgColor="DDE3EA")
TOTAL_FILL = PatternFill("solid", fgColor="F2F4F7")
FIXED_TIME = datetime(2000, 1, 1)
FIRST_ROW = 5  # data starts here on the monthly sheets


# ------------------------------------------------------------- helpers

def col(n: int) -> str:
    return get_column_letter(n)


def money_cell(ws: Worksheet, ref: str, value) -> None:
    """Write cents as an exact Decimal, or a formula string, with money format."""
    ws[ref] = to_decimal(value) if isinstance(value, int) else value
    ws[ref].number_format = MONEY


def header_row(ws: Worksheet, row: int, labels: list[str], start_col: int = 1) -> None:
    for i, label in enumerate(labels):
        cell = ws.cell(row=row, column=start_col + i, value=label)
        cell.font, cell.fill = BOLD, HEADER_FILL
        cell.alignment = Alignment(horizontal="center" if i else "left")


def sheet_title(ws: Worksheet, title: str, subtitle: str) -> None:
    ws["A1"], ws["A1"].font = title, TITLE
    ws["A2"] = subtitle


def fiscal_year(month: str, fy_end: int) -> int:
    """FY is named after the calendar year in which it ends."""
    year, m = int(month[:4]), int(month[5:])
    return year if m <= fy_end else year + 1


class MonthlyLayout:
    """Column positions shared by every month-by-month sheet:
    A = labels, then one column per month, then one per fiscal year, then LTM."""

    def __init__(self, months: list[str], fy_end: int):
        self.months = months
        self.month_col = {m: 2 + i for i, m in enumerate(months)}
        self.fy_spans: list[tuple[str, int, int]] = []  # (label, first col, last col)
        for m in months:
            fy, c = fiscal_year(m, fy_end), self.month_col[m]
            label = f"FY{fy}"
            if self.fy_spans and self.fy_spans[-1][0] == label:
                self.fy_spans[-1] = (self.fy_spans[-1][0], self.fy_spans[-1][1], c)
            else:
                self.fy_spans.append((label, c, c))
        # Mark part years, e.g. "FY2026 (3m)".
        self.fy_spans = [(lbl if last - first == 11 else f"{lbl} ({last - first + 1}m)", first, last)
                         for lbl, first, last in self.fy_spans]
        last_month_col = 1 + len(months)
        self.fy_col = {lbl: last_month_col + 1 + i for i, (lbl, _, _) in enumerate(self.fy_spans)}
        self.ltm_col = last_month_col + len(self.fy_spans) + 1
        self.ltm_first = max(2, last_month_col - 11)
        self.ltm_last = last_month_col

    def headers(self) -> list[str]:
        ltm = "LTM" if len(self.months) >= 12 else f"Total ({len(self.months)}m)"
        return ["", *self.months, *[lbl for lbl, _, _ in self.fy_spans], ltm]

    def write_series(self, ws: Worksheet, row: int, label: str, values: list[int], bold=False) -> None:
        ws.cell(row=row, column=1, value=label).font = Font(bold=bold)
        for m, v in zip(self.months, values):
            money_cell(ws, f"{col(self.month_col[m])}{row}", v)
        self.write_totals(ws, row, bold)

    def write_formula_row(self, ws: Worksheet, row: int, label: str, template: str, bold=True) -> None:
        """template uses {c} for the column letter, e.g. '={c}5-{c}6'."""
        ws.cell(row=row, column=1, value=label).font = Font(bold=bold)
        for m in self.months:
            c = col(self.month_col[m])
            money_cell(ws, f"{c}{row}", template.format(c=c))
        self.write_totals(ws, row, bold)
        if bold:
            for c in range(1, self.ltm_col + 1):
                ws.cell(row=row, column=c).fill = TOTAL_FILL

    def write_totals(self, ws: Worksheet, row: int, bold=False) -> None:
        for lbl, first, last in self.fy_spans:
            ref = f"{col(self.fy_col[lbl])}{row}"
            money_cell(ws, ref, f"=SUM({col(first)}{row}:{col(last)}{row})")
            ws[ref].font = Font(bold=bold)
        ref = f"{col(self.ltm_col)}{row}"
        money_cell(ws, ref, f"=SUM({col(self.ltm_first)}{row}:{col(self.ltm_last)}{row})")
        ws[ref].font = Font(bold=True)

    def format(self, ws: Worksheet, label_width: int = 34) -> None:
        ws.column_dimensions["A"].width = label_width
        for c in range(2, self.ltm_col + 1):
            ws.column_dimensions[col(c)].width = 14
        ws.freeze_panes = ws.cell(row=FIRST_ROW, column=2)


# ------------------------------------------------------------- sheets

PL_ROWS = {  # row number of each line on the P&L sheet
    "revenue": 5, "cogs": 6, "gross_profit": 7, "opex": 8, "ebitda": 9,
    "depreciation": 10, "ebit": 11, "interest": 12, "other_income": 13, "tax": 14, "net_income": 15,
}
RULE_NAMES = {"OWN": "Owner / personal expenses", "REL": "Related-party normalisation",
              "CUT": "Revenue cut-off", "UNU": "One-off costs"}


def write_pl(wb: Workbook, result: RunResult, layout: MonthlyLayout) -> None:
    ws = wb.create_sheet("P&L")
    sheet_title(ws, "Monthly profit and loss", f"{result.company} · {result.config.currency} · costs shown as positive")
    header_row(ws, 4, layout.headers())
    pl, r = result.statements.pl, PL_ROWS
    layout.write_series(ws, r["revenue"], LABELS["revenue"], pl["revenue"])
    layout.write_series(ws, r["cogs"], "Less: " + LABELS["cogs"], pl["cogs"])
    layout.write_formula_row(ws, r["gross_profit"], "Gross profit", "={c}5-{c}6")
    layout.write_series(ws, r["opex"], "Less: " + LABELS["opex"], pl["opex"])
    layout.write_formula_row(ws, r["ebitda"], "Reported EBITDA", "={c}7-{c}8")
    layout.write_series(ws, r["depreciation"], "Less: " + LABELS["depreciation"], pl["depreciation"])
    layout.write_formula_row(ws, r["ebit"], "EBIT", "={c}9-{c}10")
    layout.write_series(ws, r["interest"], "Less: " + LABELS["interest"], pl["interest"])
    layout.write_series(ws, r["other_income"], "Add: " + LABELS["other_income"], pl["other_income"])
    layout.write_series(ws, r["tax"], "Less: " + LABELS["tax"], pl["tax"])
    layout.write_formula_row(ws, r["net_income"], "Net income", "={c}11-{c}12+{c}13-{c}14")
    layout.format(ws)


def write_adjusted(wb: Workbook, result: RunResult, layout: MonthlyLayout) -> None:
    ws = wb.create_sheet("Adjusted EBITDA")
    sheet_title(ws, "Reported to adjusted EBITDA", "Only reviewer-accepted adjustments are included")
    header_row(ws, 4, layout.headers())
    layout.write_formula_row(ws, 5, "Reported EBITDA (from P&L)", "='P&L'!{c}9", bold=False)

    index = {m: i for i, m in enumerate(layout.months)}
    row = 6
    for rule, name in RULE_NAMES.items():
        per_month = [0] * len(layout.months)
        for rc in result.reviewed:
            if rc.status == "accepted" and rc.candidate.rule == rule:
                per_month[index[rc.candidate.month]] += rc.final_effect
        layout.write_series(ws, row, f"  {name}", per_month)
        row += 1
    last_rule_row = row - 1
    layout.write_formula_row(ws, row, "Total accepted adjustments", f"=SUM({{c}}6:{{c}}{last_rule_row})")
    layout.write_formula_row(ws, row + 1, "Adjusted EBITDA", f"={{c}}5+{{c}}{row}")
    layout.format(ws)


ADJ_HEADERS = ["ID", "Rule", "Finding", "Date", "Account", "Counterparty", "Description",
               "Amount involved", "Proposed effect", "Status", "Final effect", "Evidence", "Why flagged", "Reviewer note"]


def write_adjustment_list(wb: Workbook, result: RunResult) -> tuple[int, int]:
    ws = wb.create_sheet("Adjustments")
    sheet_title(ws, "Adjustment candidates", "Proposed by rules; only 'accepted' rows change adjusted EBITDA")
    header_row(ws, 4, ADJ_HEADERS)
    first = 5
    for i, rc in enumerate(result.reviewed):
        c, r = rc.candidate, first + i
        values = [c.id, c.rule, c.title, c.date, f"{c.account_code} {c.account_name}".strip(),
                  c.counterparty, c.description, None, None, rc.status, None,
                  ", ".join(str(e) for e in c.evidence), c.rationale, rc.note]
        for j, v in enumerate(values, start=1):
            if v is not None:
                ws.cell(row=r, column=j, value=v)
        ws.cell(row=r, column=4).number_format = "yyyy-mm-dd"
        money_cell(ws, f"H{r}", c.amount_involved)
        money_cell(ws, f"I{r}", c.proposed_effect)
        money_cell(ws, f"K{r}", rc.final_effect)
    last = first + max(len(result.reviewed), 1) - 1
    total_row = last + 2
    ws.cell(row=total_row, column=1, value="Accepted total").font = BOLD
    money_cell(ws, f"K{total_row}", f'=SUMIF(J{first}:J{last},"accepted",K{first}:K{last})')
    ws[f"K{total_row}"].font = BOLD
    widths = [14, 6, 34, 11, 26, 26, 38, 15, 15, 10, 15, 16, 60, 30]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[col(j)].width = w
    ws.freeze_panes = "B5"
    return first, last


def write_cash(wb: Workbook, result: RunResult) -> tuple[int, int]:
    ws = wb.create_sheet("Proof of cash")
    sheet_title(ws, "Proof of cash", "Book = cash accounts in the ledger; bank = bank statement. Difference = bank - book.")
    ws["D2"], ws["D2"].font = "Tolerance", BOLD
    money_cell(ws, "E2", result.config.cash_tolerance)
    header_row(ws, 4, ["Month", "Book net movement", "Bank net movement", "Difference", "Ties?"])
    first = 5
    for i, m in enumerate(result.cash):
        r = first + i
        ws.cell(row=r, column=1, value=m.month)
        money_cell(ws, f"B{r}", m.book_net)
        money_cell(ws, f"C{r}", m.bank_net)
        money_cell(ws, f"D{r}", f"=C{r}-B{r}")
        ws[f"E{r}"] = f'=IF(ABS(D{r})<=$E$2,"yes","NO")'
        ws[f"E{r}"].alignment = Alignment(horizontal="center")
    last = first + max(len(result.cash), 1) - 1
    for letter, width in zip("ABCDE", (12, 20, 20, 16, 8)):
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "B5"
    return first, last


def write_nwc(wb: Workbook, result: RunResult) -> tuple[int, int]:
    ws = wb.create_sheet("Working capital")
    sheet_title(ws, "Net working capital", "Month-end balances; payables shown as positive")
    header_row(ws, 4, ["Month", "Receivables", "Inventory", "Payables", "Net working capital"])
    s, first = result.statements, 5
    for i, m in enumerate(s.months):
        r = first + i
        ws.cell(row=r, column=1, value=m)
        money_cell(ws, f"B{r}", s.balances["asset_ar"][i])
        money_cell(ws, f"C{r}", s.balances["asset_inventory"][i])
        money_cell(ws, f"D{r}", -s.balances["liability_ap"][i])
        money_cell(ws, f"E{r}", f"=B{r}+C{r}-D{r}")
    last = first + len(s.months) - 1
    avg_row = last + 2
    ws.cell(row=avg_row, column=1, value="Average, last 12 months (a starting point for the peg)").font = BOLD
    money_cell(ws, f"E{avg_row}", f"=AVERAGE(E{max(first, last - 11)}:E{last})")
    ws[f"E{avg_row}"].font = BOLD
    for letter, width in zip("ABCDE", (52, 16, 16, 16, 22)):
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "B5"
    return avg_row, 0


def write_customers(wb: Workbook, result: RunResult) -> int:
    ws = wb.create_sheet("Customers")
    window = result.statements.months[-12:]
    sheet_title(ws, "Revenue by customer", f"{window[0]} to {window[-1]}")
    header_row(ws, 4, ["Customer", "Revenue", "Share", "Cumulative share"])
    first = 5
    rows = result.customers or [("(no revenue)", 0)]
    last = first + len(rows) - 1
    total_row = last + 1
    for i, (name, amount) in enumerate(rows):
        r = first + i
        ws.cell(row=r, column=1, value=name)
        money_cell(ws, f"B{r}", amount)
        ws[f"C{r}"] = f"=IF($B${total_row}=0,0,B{r}/$B${total_row})"
        ws[f"D{r}"] = f"=IF($B${total_row}=0,0,SUM($B${first}:B{r})/$B${total_row})"
        ws[f"C{r}"].number_format = ws[f"D{r}"].number_format = PERCENT
    ws.cell(row=total_row, column=1, value="Total").font = BOLD
    money_cell(ws, f"B{total_row}", f"=SUM(B{first}:B{last})")
    ws[f"B{total_row}"].font = BOLD
    for letter, width in zip("ABCD", (34, 18, 10, 18)):
        ws.column_dimensions[letter].width = width
    return first


def write_checks(wb: Workbook, result: RunResult) -> None:
    ws = wb.create_sheet("Checks")
    sheet_title(ws, "Checks", "Blockers stop the run; warnings are findings to review")
    header_row(ws, 4, ["Check", "Severity", "Result", "Detail"])
    for i, c in enumerate(result.checks):
        r = 5 + i
        ws.cell(row=r, column=1, value=c.name)
        ws.cell(row=r, column=2, value=c.severity)
        cell = ws.cell(row=r, column=3, value="PASS" if c.passed else "FAIL")
        cell.font = Font(bold=True, color="1A7F37" if c.passed else "C62828")
        ws.cell(row=r, column=4, value=c.detail)
    for letter, width in zip("ABCD", (44, 10, 8, 100)):
        ws.column_dimensions[letter].width = width


def write_summary(wb: Workbook, result: RunResult, layout: MonthlyLayout,
                  adj_rows: tuple[int, int], cash_rows: tuple[int, int], nwc_avg_row: int,
                  results_hash: str) -> None:
    ws = wb.active
    ws.title = "Summary"
    s = result.statements
    ltm = col(layout.ltm_col)
    adj_first, adj_last = adj_rows
    cash_first, cash_last = cash_rows
    total_adj_row = 6 + len(RULE_NAMES)

    sheet_title(ws, "Quality of earnings databook", result.company)
    info = [
        ("Period", f"{s.months[0]} to {s.months[-1]}"),
        ("Currency", result.config.currency),
        ("LTM window", f"{s.months[-12:][0]} to {s.months[-1]}"),
        ("Prepared with", f"Apex Diligence {__version__}"),
        ("Results fingerprint", results_hash[:16]),
    ]
    for i, (k, v) in enumerate(info, start=4):
        ws.cell(row=i, column=1, value=k).font = BOLD
        ws.cell(row=i, column=2, value=v)

    header_row(ws, 10, ["Key figures (LTM)", "Amount"])
    figures = [
        ("Revenue", f"='P&L'!{ltm}5"),
        ("Reported EBITDA", f"='P&L'!{ltm}9"),
        ("Accepted adjustments", f"='Adjusted EBITDA'!{ltm}{total_adj_row}"),
        ("Adjusted EBITDA", f"='Adjusted EBITDA'!{ltm}{total_adj_row + 1}"),
    ]
    for i, (label, formula) in enumerate(figures, start=11):
        ws.cell(row=i, column=1, value=label)
        money_cell(ws, f"B{i}", formula)
    ws["A14"].font = ws["B14"].font = BOLD
    ws["A15"] = "Adjusted EBITDA margin"
    ws["B15"] = "=IF(B11=0,0,B14/B11)"
    ws["B15"].number_format = PERCENT
    ws["A16"] = "Average net working capital (12m)"
    money_cell(ws, "B16", f"='Working capital'!E{nwc_avg_row}")

    header_row(ws, 18, ["Review status", "Count"])
    statuses = ["accepted", "rejected", "pending"]
    for i, status in enumerate(statuses, start=19):
        ws.cell(row=i, column=1, value=f"Candidates {status}")
        ws.cell(row=i, column=2, value=f'=COUNTIF(Adjustments!J{adj_first}:J{adj_last},"{status}")')
    ws["A22"] = "Months where proof of cash does not tie"
    ws["B22"] = f'=COUNTIF(\'Proof of cash\'!E{cash_first}:E{cash_last},"NO")'

    header_row(ws, 24, ["Checks needing attention", "Detail"])
    failing = [c for c in result.checks if not c.passed]
    for i, c in enumerate(failing or [], start=25):
        ws.cell(row=i, column=1, value=c.name)
        ws.cell(row=i, column=2, value=c.detail)
    if not failing:
        ws["A25"] = "None: all checks passed"
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 60


# ------------------------------------------------------------- entry point

def build_databook(result: RunResult, results_hash: str) -> bytes:
    layout = MonthlyLayout(result.statements.months, result.config.fiscal_year_end_month)
    wb = Workbook()
    write_pl(wb, result, layout)
    write_adjusted(wb, result, layout)
    adj_rows = write_adjustment_list(wb, result)
    cash_rows = write_cash(wb, result)
    nwc_avg_row, _ = write_nwc(wb, result)
    write_customers(wb, result)
    write_checks(wb, result)
    write_summary(wb, result, layout, adj_rows, cash_rows, nwc_avg_row, results_hash)

    wb.properties.creator = "Apex Diligence"
    wb.properties.created = wb.properties.modified = FIXED_TIME
    buffer = io.BytesIO()
    wb.save(buffer)
    return _reproducible_zip(buffer.getvalue())


def _reproducible_zip(data: bytes) -> bytes:
    """Rewrite the .xlsx (a zip file) with fixed timestamps so identical
    content always gives identical bytes."""
    source = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "docProps/core.xml":
                # openpyxl stamps the save time here; replace it with our fixed date.
                content = re.sub(rb"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                                 rb"\g<1>2000-01-01T00:00:00Z\g<2>", content)
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_DEFLATED
            fixed.external_attr = 0o644 << 16
            target.writestr(fixed, content)
    return out.getvalue()


def write_databook(result: RunResult, results_hash: str, path: Path) -> None:
    path.write_bytes(build_databook(result, results_hash))
