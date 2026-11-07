"""The operating projection shared by the LBO and DCF models.

Revenue grows each year; EBITDA, D&A, capex and working capital are set as a
percentage of revenue. Every step is rounded to whole cents with `mul`, which
matches Excel's ROUND(x, 2), so the exported workbooks agree to the centavo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import Money, mul, ratio
from .workspace import Assumption, AssumptionLog, BaseFigures, ModelError, check_range


@dataclass(frozen=True)
class OperatingAssumptions:
    years: int
    revenue_growth: tuple[Decimal, ...]  # one fraction per year, 0.05 = 5%
    ebitda_margin: Decimal
    da_pct_revenue: Decimal
    capex_pct_revenue: Decimal
    nwc_pct_revenue: Decimal
    tax_rate: Decimal


@dataclass(frozen=True)
class OperatingYear:
    year: int
    revenue: Money
    ebitda: Money
    da: Money
    ebit: Money
    capex: Money
    nwc: Money
    nwc_change: Money


def project(base_revenue: int, a: OperatingAssumptions) -> tuple[Money, list[OperatingYear]]:
    """Returns (year-0 working capital, projected years)."""
    nwc_previous = mul(base_revenue, a.nwc_pct_revenue)
    nwc_0 = Money(nwc_previous)
    revenue = base_revenue
    years = []
    for t, growth in enumerate(a.revenue_growth, start=1):
        revenue = mul(revenue, 1 + growth)
        ebitda = mul(revenue, a.ebitda_margin)
        da = mul(revenue, a.da_pct_revenue)
        capex = mul(revenue, a.capex_pct_revenue)
        nwc = mul(revenue, a.nwc_pct_revenue)
        years.append(OperatingYear(t, Money(revenue), Money(ebitda), Money(da), Money(ebitda - da),
                                   Money(capex), Money(nwc), Money(nwc - nwc_previous)))
        nwc_previous = nwc
    return nwc_0, years


def resolve_operating(base: BaseFigures, log: AssumptionLog, *, years=None, revenue_growth=None,
                      ebitda_margin=None, da_pct_revenue=None, capex_pct_revenue=None,
                      nwc_pct_revenue=None, tax_rate=None) -> OperatingAssumptions:
    """Fill every operating assumption from the inputs, a default, or the company's own LTM figures.
    All rates are fractions (0.05 = 5%). `revenue_growth` may be one rate or one per year."""
    if base.revenue <= 0:
        raise ModelError("LTM revenue is zero or negative; operating projections need positive revenue")

    years = log.pick("years", years, 5)
    if not isinstance(years, int) or not 1 <= years <= 10:
        raise ModelError("years must be a whole number from 1 to 10")

    if isinstance(revenue_growth, (list, tuple)):
        if len(revenue_growth) != years:
            raise ModelError(f"revenue_growth has {len(revenue_growth)} values but years is {years}")
        growth = tuple(Decimal(g) for g in revenue_growth)
        log.items.append(Assumption("revenue_growth", growth, "given"))
    else:
        g = log.pick("revenue_growth", None if revenue_growth is None else Decimal(revenue_growth), Decimal("0.05"))
        growth = tuple([Decimal(g)] * years)
    for g in growth:
        check_range("revenue_growth", g, "-0.5", "1")

    margin = log.pick("ebitda_margin", ebitda_margin, ratio(base.adjusted_ebitda, base.revenue, 4), derived=True,
                      note="LTM adjusted EBITDA / LTM revenue")
    da = log.pick("da_pct_revenue", da_pct_revenue, ratio(base.depreciation, base.revenue, 4), derived=True,
                  note="LTM depreciation / LTM revenue")
    capex = log.pick("capex_pct_revenue", capex_pct_revenue, Decimal("0.02"),
                     note="the ledger has no capex history; replace with the company's plan")
    nwc = log.pick("nwc_pct_revenue", nwc_pct_revenue, ratio(base.nwc, base.revenue, 4), derived=True,
                   note="closing NWC / LTM revenue")
    tax = log.pick("tax_rate", tax_rate, Decimal("0.25"), note="Philippine corporate income tax rate")

    check_range("ebitda_margin", margin, -1, 1)
    check_range("da_pct_revenue", da, 0, 1)
    check_range("capex_pct_revenue", capex, 0, 1)
    check_range("nwc_pct_revenue", nwc, -1, 1)
    check_range("tax_rate", tax, 0, "0.6")
    return OperatingAssumptions(years, growth, Decimal(margin), Decimal(da), Decimal(capex), Decimal(nwc), Decimal(tax))
