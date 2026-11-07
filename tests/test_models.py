"""LBO, DCF and projection maths, checked against small cases worked out by hand."""

from decimal import Decimal as D

import pytest

from apex_diligence.dcf import DCFAssumptions, run_dcf
from apex_diligence.lbo import LBOAssumptions, run_lbo
from apex_diligence.projection import OperatingAssumptions, project
from apex_diligence.workspace import ModelError

FLAT = OperatingAssumptions(years=1, revenue_growth=(D(0),), ebitda_margin=D("0.10"), da_pct_revenue=D(0),
                            capex_pct_revenue=D(0), nwc_pct_revenue=D(0), tax_rate=D(0))


def lbo(operating=FLAT, **kw):
    params = dict(entry_multiple=D(5), exit_multiple=D(5), senior_multiple=D(3), senior_rate=D("0.10"),
                  senior_amortisation=D(0), cash_sweep=D(1), sub_multiple=D(0), sub_rate=D("0.12"), fees_pct=D(0))
    params.update(kw)
    return LBOAssumptions(operating=operating, **params)


def test_projection_grows_and_tracks_nwc():
    a = OperatingAssumptions(years=2, revenue_growth=(D("0.10"), D("0.10")), ebitda_margin=D("0.2"),
                             da_pct_revenue=D("0.05"), capex_pct_revenue=D("0.03"), nwc_pct_revenue=D("0.1"),
                             tax_rate=D("0.25"))
    nwc0, years = project(100000, a)  # revenue 1,000.00
    assert nwc0 == 10000
    assert [y.revenue for y in years] == [110000, 121000]
    assert years[0].ebitda == 22000 and years[0].ebit == 22000 - 5500
    assert [y.nwc_change for y in years] == [1000, 1100]


def test_lbo_hand_worked_one_year_case():
    # EV 500, debt 300, equity 200. Year 1: EBITDA 100, interest 30, FCF 70, all swept.
    # Exit: EV 500, debt 230, equity 270 -> 1.35x, IRR 35%.
    r = run_lbo(100000, 10000, lbo())
    assert (r.entry_ev, r.senior_debt, r.equity) == (50000, 30000, 20000)
    y = r.years[0]
    assert (y.interest, y.free_cash_flow, y.sweep, y.senior_close, y.cash_close) == (3000, 7000, 7000, 23000, 0)
    assert (r.exit_ev, r.exit_equity) == (50000, 27000)
    assert r.moic == D("1.3500") and r.irr == D("0.350000")
    assert r.bridge == {"ebitda_growth": 0, "multiple_change": 0, "debt_paydown_and_cash": 7000,
                        "fees": 0, "equity_gain": 7000}


def test_lbo_tax_amortisation_sweep_and_sub_debt():
    op = OperatingAssumptions(years=2, revenue_growth=(D(0), D(0)), ebitda_margin=D("0.10"),
                              da_pct_revenue=D(0), capex_pct_revenue=D(0), nwc_pct_revenue=D(0), tax_rate=D("0.25"))
    r = run_lbo(100000, 10000, lbo(op, senior_amortisation=D("0.10"), cash_sweep=D("0.5"),
                                   sub_multiple=D(1), fees_pct=D("0.02")))
    # Entry: EV 500, fees 10, senior 300, sub 100, equity 110.
    assert (r.fees, r.sub_debt, r.equity) == (1000, 10000, 11000)
    y1 = r.years[0]
    # interest 30 + 12 = 42; tax 25% x (100 - 42) = 14.50; FCF 100 - 14.50 - 42 = 43.50
    # mandatory 10% x 300 = 30; spare 13.50; sweep 50% = 6.75; senior 263.25; cash 6.75
    assert (y1.interest, y1.tax, y1.free_cash_flow) == (4200, 1450, 4350)
    assert (y1.mandatory_repayment, y1.sweep, y1.senior_close, y1.cash_close) == (3000, 675, 26325, 675)
    assert y1.sub_close == 10000
    gain = sum(v for k, v in r.bridge.items() if k != "equity_gain")
    assert gain == r.bridge["equity_gain"] == r.exit_equity - r.equity


def test_lbo_refuses_impossible_structures():
    with pytest.raises(ModelError, match="equity would be zero or negative"):
        run_lbo(100000, 10000, lbo(senior_multiple=D(6)))
    with pytest.raises(ModelError, match="EBITDA is zero or negative"):
        run_lbo(100000, 0, lbo())
    with pytest.raises(ModelError, match="entry_multiple"):
        run_lbo(100000, 10000, lbo(entry_multiple=D(0)))


def test_lbo_flags_cash_shortfall_and_wipeout():
    op = OperatingAssumptions(years=1, revenue_growth=(D("-0.5"),), ebitda_margin=D("0.01"), da_pct_revenue=D(0),
                              capex_pct_revenue=D(0), nwc_pct_revenue=D(0), tax_rate=D(0))
    r = run_lbo(100000, 10000, lbo(op, senior_multiple=D("4.5"), senior_rate=D("0.2")))
    assert any("cash shortfall" in w for w in r.warnings)
    assert r.irr is None and r.moic == 0


def test_dcf_hand_worked_case():
    # FCF 100 in year 1, WACC 10%, no growth: PV 90.91, TV 1,000 -> PV 909.09, EV 1,000.00
    a = DCFAssumptions(operating=FLAT, wacc=D("0.10"), terminal_method="gordon", terminal_growth=D(0),
                       exit_multiple=D(0), mid_year=False)
    r = run_dcf(10000, 100000, a)
    assert r.years[0].present_value == 9091
    assert (r.terminal_value, r.pv_terminal, r.enterprise_value) == (100000, 90909, 100000)
    assert r.implied_ev_to_ltm_ebitda == D("10.00")


def test_dcf_mid_year_and_exit_multiple():
    a = DCFAssumptions(operating=FLAT, wacc=D("0.10"), terminal_method="exit_multiple", terminal_growth=D(0),
                       exit_multiple=D(8), mid_year=True)
    r = run_dcf(10000, 100000, a)
    assert r.years[0].discount_factor > D("0.95")  # discounted half a year
    assert r.terminal_value == 80000


def test_dcf_validation():
    a = DCFAssumptions(operating=FLAT, wacc=D("0.03"), terminal_method="gordon", terminal_growth=D("0.04"),
                       exit_multiple=D(0), mid_year=False)
    with pytest.raises(ModelError, match="below the WACC"):
        run_dcf(10000, 100000, a)
