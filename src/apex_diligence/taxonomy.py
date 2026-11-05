"""The standard chart of accounts every client ledger is mapped onto.

Keeping the taxonomy small and fixed is what makes outputs comparable across
deals: every databook has the same rows no matter how the client named things.
"""

from __future__ import annotations

PL_CATEGORIES = [
    "revenue",
    "cogs",
    "opex",
    "depreciation",
    "interest",
    "other_income",
    "tax",
]

BS_CATEGORIES = [
    "asset_cash",
    "asset_ar",
    "asset_inventory",
    "asset_other",
    "liability_ap",
    "liability_other",
    "equity",
]

CATEGORIES = PL_CATEGORIES + BS_CATEGORIES

LABELS = {
    "revenue": "Revenue",
    "cogs": "Cost of goods sold",
    "opex": "Operating expenses",
    "depreciation": "Depreciation and amortisation",
    "interest": "Interest expense",
    "other_income": "Other income (expense)",
    "tax": "Income tax",
    "asset_cash": "Cash",
    "asset_ar": "Accounts receivable",
    "asset_inventory": "Inventory",
    "asset_other": "Other assets",
    "liability_ap": "Accounts payable",
    "liability_other": "Other liabilities",
    "equity": "Equity",
}

# Income categories are naturally credits. We show them as positive numbers.
CREDIT_NATURAL = {"revenue", "other_income"}

# Costs that sit above EBITDA. Adjustment rules only look at these.
EBITDA_COSTS = ("cogs", "opex")


def pl_amount(category: str, net: int) -> int:
    """Turn a debit-positive net amount into the sign a P&L reader expects.

    Revenue of 100 is stored in the ledger as a credit (net = -100);
    on the P&L it should read +100. Costs are already debit-positive.
    """
    return -net if category in CREDIT_NATURAL else net
