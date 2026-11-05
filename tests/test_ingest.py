from datetime import date

import openpyxl
import pytest

from apex_diligence.ingest import IngestError, load_bank, load_decisions, load_gl, load_mapping, parse_date
from conftest import write_csv

GL_HEADER = ["date", "journal_id", "account_code", "description", "debit", "credit"]


def test_gl_rows_keep_their_source_row(tmp_path):
    path = write_csv(tmp_path / "gl.csv", GL_HEADER, [
        ["2025-01-05", "J1", "1000", "cash", "100.00", ""],
        ["", "", "", "", "", ""],                       # blank line is skipped
        ["2025-01-05", "J1", "4000", "sale", "", "100.00"],
    ])
    lines = load_gl(path)
    assert [str(l.source) for l in lines] == ["gl.csv:2", "gl.csv:4"]
    assert lines[0].debit == 10000 and lines[1].credit == 10000
    assert lines[0].entry_type == "auto"


def test_gl_reports_every_problem_with_row_numbers(tmp_path):
    path = write_csv(tmp_path / "gl.csv", GL_HEADER + ["entry_type"], [
        ["05/01/2025", "J1", "1000", "x", "1", "", "auto"],    # ambiguous date
        ["2025-01-05", "", "1000", "x", "1", "", "auto"],      # blank journal
        ["2025-01-05", "J2", "1000", "x", "1", "1", "auto"],   # both sides
        ["2025-01-05", "J3", "1000", "x", "abc", "", "auto"],  # bad amount
        ["2025-02-30", "J4", "1000", "x", "1", "", "auto"],    # impossible date
        ["2025-01-05", "J5", "1000", "x", "1", "", "magic"],   # bad entry type
    ])
    with pytest.raises(IngestError) as err:
        load_gl(path)
    rows = " ".join(err.value.problems)
    for row in ("row 2", "row 3", "row 4", "row 5", "row 6", "row 7"):
        assert row in rows
    assert len(err.value.problems) == 6


def test_gl_missing_columns_named(tmp_path):
    path = write_csv(tmp_path / "gl.csv", ["date", "account"], [["2025-01-01", "1000"]])
    with pytest.raises(IngestError, match="journal_id, debit, credit"):
        load_gl(path)


def test_gl_aliases_and_excel(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Posting Date", "JE", "Account No", "Memo", "Dr", "Cr", "Vendor"])
    ws.append([date(2025, 3, 1), "J1", 6100, "rent", 500.25, None, "Landlord"])
    ws.append([date(2025, 3, 1), "J1", 1000, "rent", None, 500.25, "Landlord"])
    wb.save(tmp_path / "gl.xlsx")
    lines = load_gl(tmp_path / "gl.xlsx")
    assert lines[0].account_code == "6100"          # 6100.0 from Excel becomes "6100"
    assert lines[0].debit == 50025
    assert lines[0].counterparty == "Landlord"
    assert str(lines[1].source) == "gl.xlsx:3"


def test_duplicate_headers_refused(tmp_path):
    path = write_csv(tmp_path / "gl.csv", ["date", "date", "x"], [["a", "b", "c"]])
    with pytest.raises(IngestError, match="duplicate"):
        load_gl(path)


def test_unsupported_and_missing_files(tmp_path):
    (tmp_path / "gl.pdf").write_bytes(b"%PDF")
    with pytest.raises(IngestError, match="unsupported"):
        load_gl(tmp_path / "gl.pdf")
    with pytest.raises(IngestError, match="not found"):
        load_gl(tmp_path / "nope.csv")


def test_bank_amount_or_split_columns(tmp_path):
    a = write_csv(tmp_path / "a.csv", ["date", "description", "amount"], [["2025-01-02", "in", "1,000.00"],
                                                                          ["2025-01-03", "out", "(250)"]])
    b = write_csv(tmp_path / "b.csv", ["Date", "Particulars", "Withdrawal", "Deposit", "Balance"],
                  [["2025-01-02", "in", "", "1000", "1000"], ["2025-01-03", "out", "250", "", "750"]])
    assert [l.amount for l in load_bank(a)] == [100000, -25000]
    assert [l.amount for l in load_bank(b)] == [100000, -25000]
    assert load_bank(b)[1].balance == 75000


def test_bank_needs_amounts(tmp_path):
    path = write_csv(tmp_path / "bank.csv", ["date", "description"], [["2025-01-01", "x"]])
    with pytest.raises(IngestError, match="amount"):
        load_bank(path)


def test_mapping_validation(tmp_path):
    good = write_csv(tmp_path / "m.csv", ["account_code", "category"], [["1000", "asset_cash"], ["4000", "Revenue"]])
    assert load_mapping(good) == {"1000": "asset_cash", "4000": "revenue"}
    bad = write_csv(tmp_path / "bad.csv", ["account_code", "category"],
                    [["1000", "asset_cash"], ["1000", "revenue"], ["2000", "stuff"]])
    with pytest.raises(IngestError) as err:
        load_mapping(bad)
    assert len(err.value.problems) == 2


def test_decisions(tmp_path):
    path = write_csv(tmp_path / "d.csv", ["candidate_id", "decision", "amount_override", "note"], [
        ["OWN-1", "accept", "", "agreed"],
        ["UNU-2", "", "", ""],                 # still pending: ignored
        ["REL-3", "Accept", "30,000", "market rent is 150k"],
        ["CUT-4", "reject", "", ""],
    ])
    d = load_decisions(path)
    assert set(d) == {"OWN-1", "REL-3", "CUT-4"}
    assert d["REL-3"].amount_override == 3000000
    assert d["CUT-4"].accepted is False
    bad = write_csv(tmp_path / "bad.csv", ["candidate_id", "decision"], [["X", "maybe"]])
    with pytest.raises(IngestError, match="accept"):
        load_decisions(bad)


def test_parse_date_formats():
    assert parse_date("2025-12-31") == date(2025, 12, 31)
    assert parse_date("2025/12/31") == date(2025, 12, 31)
    assert parse_date("2025-12-31 00:00:00") == date(2025, 12, 31)
    with pytest.raises(ValueError):
        parse_date("31-12-2025")
