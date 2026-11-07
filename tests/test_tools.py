"""The tool layer: validation, outputs, determinism and Excel exports."""

import csv
import shutil
import subprocess
from decimal import Decimal as D

import openpyxl
import pytest

from apex_diligence.money import to_cents
from apex_diligence.schema import ToolInputError, canonical, validate
from apex_diligence.tools import TOOLS, Toolbox
from apex_diligence.workspace import open_workspace

SAMPLE_CALLS = [
    ("get_financials", {}), ("get_financials", {"period": "FY2024"}), ("get_financials", {"period": "2025-Q2"}),
    ("get_financials", {"period": "2025-03"}), ("get_financials", {"period": "all"}),
    ("monthly_series", {"metric": "adjusted_ebitda"}), ("monthly_series", {"metric": "debt", "start": "2025-06"}),
    ("list_adjustments", {"rule": "OWN"}), ("proof_of_cash", {}), ("run_checks", {}),
    ("nwc_analysis", {"peg_months": 6}), ("customer_analysis", {}), ("credit_metrics", {}),
    ("purchase_price_bridge", {"enterprise_value": 60000000}),
    ("cash_flow_13_week", {}), ("cash_flow_13_week", {"as_of": "2025-09-30", "min_cash": 13000000}),
    ("lbo_model", {"entry_multiple": 6}), ("dcf_valuation", {"wacc_pct": 12}),
    ("sensitivity", {"model": "lbo", "base": {"entry_multiple": 6},
                     "x": {"param": "exit_multiple", "values": [5, 6, 7]},
                     "y": {"param": "revenue_growth_pct", "values": [0, 5]}}),
    ("calculator", {"expression": "(1,000 + 250.5) / 3", "decimals": 4}),
]


@pytest.fixture(scope="module")
def toolbox(demo_inputs, tmp_path_factory):
    return Toolbox(open_workspace(demo_inputs, tmp_path_factory.mktemp("tools")))


@pytest.mark.parametrize("name, arguments", SAMPLE_CALLS)
def test_every_tool_runs_and_is_deterministic(toolbox, name, arguments):
    first, error = toolbox.call(name, arguments)
    assert not error, first
    second, _ = toolbox.call(name, arguments)
    assert canonical(first) == canonical(second)


def test_tool_specs_are_valid_for_the_api():
    import re
    for tool in TOOLS:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", tool.name)
        assert tool.schema["type"] == "object" and tool.description


@pytest.mark.parametrize("name, arguments, message", [
    ("lbo_model", {}, "missing required field 'entry_multiple'"),
    ("lbo_model", {"entry_multiple": "six"}, "must be a number"),
    ("lbo_model", {"entry_multiple": 6, "leverage": 4}, "unknown field"),
    ("lbo_model", {"entry_multiple": 6, "years": 2.5}, "whole number"),
    ("lbo_model", {"entry_multiple": 6, "revenue_growth_pct": [5, 5]}, "years is 5"),
    ("lbo_model", {"entry_multiple": 2, "senior_debt_multiple": 3}, "equity would be zero"),
    ("monthly_series", {"metric": "vibes"}, "must be one of"),
    ("monthly_series", {"metric": "cash", "start": "June"}, "expected format"),
    ("get_financials", {"period": "last summer"}, "not understood"),
    ("explain_adjustment", {"candidate_id": "OWN-00000000"}, "no candidate"),
    ("calculator", {"expression": "__import__('os').system('ls')"}, "only numbers"),
    ("calculator", {"expression": "1/0"}, "division by zero"),
    ("sensitivity", {"model": "lbo", "x": {"param": "exit_multiple", "values": [5, 6]},
                     "y": {"param": "exit_multiple", "values": [5, 6]}, "base": {"entry_multiple": 6}}, "different"),
    ("sensitivity", {"model": "dcf", "x": {"param": "terminal_method", "values": [1, 2]},
                     "y": {"param": "wacc_pct", "values": [10, 12]}}, "numeric"),
    ("purchase_price_bridge", {}, "exactly one"),
    ("dcf_valuation", {"wacc_pct": 3, "terminal_growth_pct": 4}, "below the WACC"),
    ("nope", {}, "unknown tool"),
])
def test_bad_inputs_become_clear_errors(toolbox, name, arguments, message):
    output, error = toolbox.call(name, arguments)
    assert error and message in output["error"]


def test_explain_adjustment_shows_the_journal(toolbox):
    listed, _ = toolbox.call("list_adjustments", {"rule": "UNU"})
    out, _ = toolbox.call("explain_adjustment", {"candidate_id": listed["candidates"][0]["id"]})
    assert out["candidate"]["proposed_effect"] == "950000.00"
    assert {l["account"] for l in out["journal_lines"]} == {"6400 Professional fees", "1000 Cash at bank"}


def test_sensitivity_matches_single_runs(toolbox):
    grid, _ = toolbox.call("sensitivity", {"model": "lbo", "base": {"entry_multiple": 6},
                                           "x": {"param": "exit_multiple", "values": [5, 7]},
                                           "y": {"param": "senior_debt_multiple", "values": [2, 3]}})
    single, _ = toolbox.call("lbo_model", {"entry_multiple": 6, "exit_multiple": 7, "senior_debt_multiple": 3})
    assert grid["rows"][1]["values"][1] == single["returns"]["irr_pct"]
    irr = [[D(v) for v in row["values"]] for row in grid["rows"]]
    assert irr[0][0] < irr[0][1] and irr[0][0] < irr[1][0]  # more exit value and more leverage raise IRR


def test_lbo_reports_every_assumption_and_its_source(toolbox):
    out, _ = toolbox.call("lbo_model", {"entry_multiple": 6, "senior_rate_pct": 10})
    sources = {a["name"]: a["source"] for a in out["assumptions"]}
    assert sources["entry_multiple"] == sources["senior_rate"] == "given"
    assert sources["ebitda_margin"] == "derived" and sources["capex_pct_revenue"] == "default"
    assert any("pending review" in c for c in out["caveats"])


def test_schema_validator_unit():
    schema = {"type": "object", "required": ["a"], "properties": {
        "a": {"anyOf": [{"type": "number"}, {"type": "array", "items": {"type": "number"}, "maxItems": 2}]}}}
    validate({"a": 1.5}, schema)
    validate({"a": [1, 2]}, schema)
    for bad in ({"a": [1, 2, 3]}, {"a": True}, {"a": float("nan")}, {}):
        with pytest.raises(ToolInputError):
            validate(bad, schema)


def test_export_writes_reproducible_workbooks(toolbox):
    a, _ = toolbox.call("export_model", {"model": "lbo", "inputs": {"entry_multiple": 6}})
    b, _ = toolbox.call("export_model", {"model": "lbo", "inputs": {"entry_multiple": 6}})
    assert a == b and a["path"].startswith("exports/lbo-")
    wb = openpyxl.load_workbook(toolbox.ws.out_dir / a["path"])
    assert wb["LBO"]["B56"].value.startswith('=IF(B54<=0')
    bad, error = toolbox.call("export_model", {"model": "lbo", "inputs": {}})
    assert error and "entry_multiple" in bad["error"]


def _recalculate(path, tmp_path):
    subprocess.run(["soffice", "--headless", "--convert-to", "csv", "--outdir", str(tmp_path), str(path)],
                   check=True, capture_output=True, timeout=180)
    return list(csv.reader((tmp_path / (path.stem + ".csv")).open()))


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_lbo_workbook_formulas_match_python(toolbox, tmp_path):
    inputs = {"entry_multiple": 6.5, "exit_multiple": 7, "sub_debt_multiple": 1, "senior_amortisation_pct": 10,
              "cash_sweep_pct": 75, "revenue_growth_pct": [3, 5, 6, 6, 4], "fees_pct": 2.5}
    model, _ = toolbox.call("lbo_model", inputs)
    exported, _ = toolbox.call("export_model", {"model": "lbo", "inputs": inputs})
    rows = {r[0]: r[1:] for r in _recalculate(toolbox.ws.out_dir / exported["path"], tmp_path) if r}
    assert to_cents(rows["Sponsor equity"][0]) == to_cents(model["entry"]["sponsor_equity"])
    assert to_cents(rows["Exit equity"][0]) == to_cents(model["returns"]["exit_equity"])
    for i, year in enumerate(model["years"], start=1):
        assert to_cents(rows["Free cash flow"][i]) == to_cents(year["free_cash_flow"])
        assert to_cents(rows["Senior debt, closing"][i]) == to_cents(year["senior_debt_close"])
        assert to_cents(rows["Cash, closing"][i]) == to_cents(year["cash_close"])
    irr = D(rows["IRR"][0].rstrip("%")) if rows["IRR"][0].endswith("%") else D(rows["IRR"][0]) * 100
    assert abs(irr - D(model["returns"]["irr_pct"])) < D("0.01")
    assert to_cents(rows["Equity gain"][0]) == to_cents(model["value_creation_bridge"]["equity_gain"])


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_cashflow_workbook_formulas_match_python(toolbox, tmp_path):
    inputs = {"as_of": "2025-09-30", "min_cash": 13500000}
    model, _ = toolbox.call("cash_flow_13_week", inputs)
    exported, _ = toolbox.call("export_model", {"model": "cash_flow_13_week", "inputs": inputs})
    rows = {r[0]: r[1:] for r in _recalculate(toolbox.ws.out_dir / exported["path"], tmp_path) if r}
    closing = [to_cents(v) for v in rows["Closing cash"][:13]]
    assert closing == [to_cents(w["closing"]) for w in model["weeks"]]
    flagged = [i + 1 for i, v in enumerate(rows["Below minimum?"][:13]) if v == "BELOW"]
    assert flagged == model["weeks_below_minimum"]
