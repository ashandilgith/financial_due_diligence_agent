"""13-week cash flow and the standard analyses, on the synthetic company."""

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from apex_diligence import analysis
from apex_diligence.cashflow import forecast_13_weeks
from apex_diligence.money import to_cents
from apex_diligence.pipeline import run
from apex_diligence.workspace import ModelError, open_workspace
from conftest import write_csv


@pytest.fixture(scope="module")
def ws(demo_inputs, tmp_path_factory):
    return open_workspace(demo_inputs, tmp_path_factory.mktemp("ws"))


def test_forecast_rolls_forward_exactly(ws):
    f = forecast_13_weeks(ws)
    assert f.as_of == date(2025, 12, 31) and len(f.weeks) == 13
    balance = f.opening_cash
    for w in f.weeks:
        assert w.opening == balance
        assert w.net == sum(w.receipts.values()) - sum(w.payments.values())
        assert w.closing == w.opening + w.net
        balance = w.closing
    # Receivables collected exactly once, across the collection lag.
    assert sum(w.receipts["Collections: opening receivables"] for w in f.weeks) == f.collectible_receivables


def test_forecast_starts_from_bank_and_shows_the_book_gap(ws):
    f = forecast_13_weeks(ws)
    assert f.opening_source == "bank statement balance"
    assert f.book_cash - f.opening_cash == to_cents("250000")  # the planted unrecorded ATM withdrawal


def test_payment_patterns_are_detected(ws):
    f = forecast_13_weeks(ws)
    streams = {(s.account_name, s.counterparty): s for s in f.streams}
    payroll = streams[("Salaries and wages", "Employees")]
    assert (payroll.cadence, payroll.day_of_month, payroll.amount) == ("monthly", 25, to_cents("680000"))
    tax = streams[("Income tax", "BIR")]
    assert tax.cadence == "quarterly"
    jan_tax = [w for w in f.weeks if w.start <= date(2026, 1, 15) <= w.end][0]
    assert jan_tax.payments["Income tax"] == to_cents("120000")
    # Payroll lands only in weeks containing the 25th.
    payroll_weeks = [w.number for w in f.weeks if w.payments.get("Salaries and wages")]
    assert len(payroll_weeks) == 3


def test_one_off_payments_are_left_out(ws):
    f = forecast_13_weeks(ws, as_of=date(2025, 6, 30))
    assert any("Legal settlement" in item for item in f.excluded_one_offs)
    assert all(s.counterparty != "Cruz Law Office" for s in f.streams)


def test_backtest_against_actual_bank(ws):
    f = forecast_13_weeks(ws, as_of=date(2025, 9, 30))
    assert f.backtest["weeks_covered"] == 13
    assert f.backtest["actual_net"] == sum(w.actual_net for w in f.weeks)
    assert all(w.variance == w.actual_net - w.net for w in f.weeks)
    assert abs(f.backtest["variance_pct_of_opening_cash"]) < 10


def test_accepted_cutoff_removes_receivable(demo_inputs, demo_result, tmp_path):
    cut = next(r.candidate for r in demo_result.reviewed if r.candidate.rule == "CUT")
    decisions = write_csv(tmp_path / "d.csv", ["candidate_id", "decision"], [[cut.id, "accept"]])
    ws = open_workspace(replace(demo_inputs, decisions=decisions), tmp_path)
    f = forecast_13_weeks(ws)
    assert f.excluded_receivables == to_cents("1200000")


def test_forecast_overrides_and_validation(ws):
    slow = forecast_13_weeks(ws, collection_days=D(70), min_cash=to_cents("17000000"))
    assert slow.collection_lag_weeks == 10 and slow.weeks_below_minimum
    with pytest.raises(ModelError, match="after the last ledger date"):
        forecast_13_weeks(ws, as_of=date(2026, 2, 1))
    with pytest.raises(ModelError, match="days of history"):
        forecast_13_weeks(ws, as_of=date(2024, 2, 1))
    with pytest.raises(ModelError, match="collection_days"):
        forecast_13_weeks(ws, collection_days=D(-1))


def test_nwc_peg(ws):
    out = analysis.nwc_analysis(ws)
    nwc = ws.statements.nwc[-12:]
    assert out["peg"] == round(sum(nwc) / 12)
    excluded = analysis.nwc_analysis(ws, exclude_months=("2025-12",))
    assert excluded["peg"] == round(sum(nwc[:-1]) / 11)
    with pytest.raises(ModelError):
        analysis.nwc_analysis(ws, exclude_months=("2019-01",))


def test_customer_concentration(ws):
    out = analysis.customer_analysis(ws, top_n=3)
    assert out["customer_count"] == 6 and len(out["customers"]) == 3
    assert out["customers"][0]["customer"] == "Metro Retail Group"
    shares = [c["share_pct"] for c in analysis.customer_analysis(ws)["customers"]]
    assert abs(sum(shares) - 100) <= D("0.05")
    assert out["new_customers"] == [] and out["lost_customers"] == []


def test_credit_metrics_and_covenants(ws):
    out = analysis.credit_metrics(ws, total_debt=to_cents("30000000"), interest_rate=D("0.09"),
                                  annual_principal=to_cents("1500000"), max_net_leverage=D(1),
                                  min_interest_cover=D(3))
    assert out["interest"] == to_cents("2700000")
    assert out["net_leverage"] == D("1.28")
    leverage, cover = out["covenants"]
    assert leverage["passes"] is False and cover["passes"] is True


def test_price_bridge_adds_up(ws):
    out = analysis.purchase_price_bridge(ws, ev_multiple=D(6), debt_like_items=[("Bonuses", to_cents("400000"))],
                                         cash_like_items=[("Tax refund", to_cents("100000"))])
    parts = (out["enterprise_value"] + out["less_debt"] + out["plus_cash"] + out["nwc_adjustment"]
             + sum(i["amount"] for i in out["less_debt_like_items"] + out["plus_cash_like_items"]))
    assert parts == out["equity_value"]
    assert out["less_debt"] == -to_cents("2000000")
    with pytest.raises(ModelError, match="exactly one"):
        analysis.purchase_price_bridge(ws)


def test_models_need_twelve_months(demo_inputs, tmp_path):
    lines = demo_inputs.gl.read_text().splitlines()
    eight_months = [l for l in lines[1:] if l[:7] in {f"2025-0{m}" for m in range(1, 9)}]
    gl = tmp_path / "gl.csv"
    gl.write_text("\n".join([lines[0]] + eight_months) + "\n")
    ws = open_workspace(replace(demo_inputs, gl=gl), tmp_path)
    with pytest.raises(ModelError, match="12 months"):
        ws.base()
    assert run(replace(demo_inputs, gl=gl))  # the pipeline itself still runs
