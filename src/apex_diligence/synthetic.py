"""A fake company with known, planted problems.

Real client data is confidential and you rarely know the 'right answer' for
it. A synthetic company fixes both: anyone can share it, and because we
planted the problems ourselves, tests can check the engine finds every one.

Planted in "Bayside Trading Corp." (24 months, Jan 2024 - Dec 2025, PHP):
  1. Owner's personal car lease booked as a business cost (12 months of 2025)
  2. A family holiday booked as travel (Jul 2025)
  3. Rent paid to a company the owner controls (every month)
  4. A one-off legal settlement in professional fees (May 2025)
  5. A manual revenue entry two days before year end with no delivery (Dec 2025)
  6. A cash withdrawal on the bank statement that is not in the books (Mar 2025)
  7. A cheque booked in December 2024 that only cleared the bank in January 2025
Everything is driven by a seeded random generator, so the files are identical every time.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

from .money import fmt, to_cents

COMPANY = "Bayside Trading Corp."
OWNER = "J. Santos (Owner)"
RELATED = "Santos Holdings Inc."
START, END = date(2024, 1, 1), date(2025, 12, 31)

ACCOUNTS = [
    ("1000", "Cash at bank", "asset_cash"),
    ("1100", "Accounts receivable", "asset_ar"),
    ("1200", "Inventory", "asset_inventory"),
    ("1500", "Equipment", "asset_other"),
    ("1510", "Accumulated depreciation", "asset_other"),
    ("2000", "Accounts payable", "liability_ap"),
    ("2100", "Bank loan", "liability_debt"),
    ("3000", "Owner's equity", "equity"),
    ("4000", "Sales", "revenue"),
    ("5000", "Cost of goods sold", "cogs"),
    ("6000", "Salaries and wages", "opex"),
    ("6100", "Rent", "opex"),
    ("6200", "Utilities", "opex"),
    ("6300", "Marketing", "opex"),
    ("6400", "Professional fees", "opex"),
    ("6500", "Travel and meals", "opex"),
    ("6600", "Vehicle expenses", "opex"),
    ("7000", "Depreciation", "depreciation"),
    ("7100", "Interest expense", "interest"),
    ("8000", "Income tax", "tax"),
]
NAMES = {code: name for code, name, _ in ACCOUNTS}

CUSTOMERS = [("Metro Retail Group", 35), ("Northpoint Supermarkets", 20), ("Cebu Home Depot", 15),
             ("Island Pharmacy Chain", 12), ("Davao Fresh Mart", 10), ("Luzon Hardware Co.", 8)]
SEASON = {1: 0.90, 2: 0.92, 3: 0.98, 4: 1.00, 5: 1.02, 6: 1.00,
          7: 0.97, 8: 0.99, 9: 1.01, 10: 1.04, 11: 1.08, 12: 1.15}
OPENING_BANK = to_cents("2500000")


class Books:
    """Collects ledger lines and the bank lines that cash entries produce."""

    def __init__(self):
        self.gl: list[list] = []
        self.bank: list[tuple[date, int, str, int]] = []  # (date, order, description, cents)
        self.count = 0

    def entry(self, when: date, lines: list[tuple[str, int, int]], description: str,
              counterparty: str = "", entry_type: str = "auto",
              bank_date: date | None = None, to_bank: bool = True) -> None:
        """lines = [(account, debit_cents, credit_cents), ...]; must balance."""
        assert sum(d - c for _, d, c in lines) == 0, "unbalanced entry"
        self.count += 1
        journal = f"JE-{self.count:05d}"
        for account, debit, credit in lines:
            self.gl.append([when.isoformat(), journal, account, NAMES[account], description, counterparty,
                            _amount(debit), _amount(credit), entry_type])
            if account == "1000" and to_bank and entry_type != "opening":
                self.bank.append((bank_date or when, len(self.bank), description, debit - credit))

    def simple(self, when: date, debit_account: str, credit_account: str, cents: int, description: str,
               counterparty: str = "", **kwargs) -> None:
        self.entry(when, [(debit_account, cents, 0), (credit_account, 0, cents)], description, counterparty, **kwargs)


def _amount(cents: int) -> str:
    return "" if cents == 0 else f"{cents // 100}.{cents % 100:02d}"


def _pesos(rng: random.Random, low: int, high: int) -> int:
    return rng.randint(low, high) * 100


def _months():
    year, month = START.year, START.month
    while date(year, month, 1) <= END:
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def build_books(seed: int = 7) -> tuple[Books, dict]:
    rng = random.Random(seed)
    books = Books()
    planted: dict = {}

    books.entry(START, [("1000", OPENING_BANK, 0), ("1200", to_cents("900000"), 0),
                        ("1500", to_cents("3600000"), 0), ("2100", 0, to_cents("2000000")),
                        ("3000", 0, to_cents("5000000"))],
                "Opening balances", entry_type="opening")

    invoice = po = 0
    for i, (year, month) in enumerate(_months()):
        first = date(year, month, 1)
        last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))

        # Sales on credit, collected about 30 days later.
        month_revenue = 0
        base = 4_000_000 * (1.01 ** i) * SEASON[month]
        for customer, weight in CUSTOMERS:
            invoice += 1
            cents = round(base * weight / 100 * rng.uniform(0.9, 1.1)) * 100
            month_revenue += cents
            when = first + timedelta(days=rng.randint(0, 24))
            books.simple(when, "1100", "4000", cents, f"Invoice INV-{invoice:05d}", customer)
            paid = when + timedelta(days=30)
            if paid <= END:
                books.simple(paid, "1000", "1100", cents, f"Collection INV-{invoice:05d}", customer)

        # Stock purchased on credit and paid about 30 days later; cost of sales at month end.
        po += 1
        cogs = round(month_revenue * 0.58 / 100) * 100
        purchase = round(cogs * rng.uniform(0.97, 1.03) / 100) * 100
        bought = date(year, month, 10)
        books.simple(bought, "1200", "2000", purchase, f"Purchase PO-{po:04d}", "Pacific Wholesale Supply")
        pay = bought + timedelta(days=30)
        if (year, month) == (2024, 11):   # planted 7: cheque booked 28 Dec, cleared 3 Jan
            planted["timing_cheque"] = purchase
            books.simple(date(2024, 12, 28), "2000", "1000", purchase, f"Payment PO-{po:04d} (cheque)",
                         "Pacific Wholesale Supply", bank_date=date(2025, 1, 3))
        elif pay <= END:
            books.simple(pay, "2000", "1000", purchase, f"Payment PO-{po:04d}", "Pacific Wholesale Supply")
        books.simple(last, "5000", "1200", cogs, "Cost of sales for the month")

        # Running costs paid from the bank.
        salaries = to_cents("650000") if year == 2024 else to_cents("680000")
        books.simple(date(year, month, 25), "6000", "1000", salaries, f"Payroll {year}-{month:02d}", "Employees")
        books.simple(date(year, month, 5), "6100", "1000", to_cents("180000"),
                     "Office and warehouse rent", RELATED)                                   # planted 3
        books.simple(date(year, month, 15), "6200", "1000", _pesos(rng, 40000, 60000), "Electricity and water", "Meralco")
        books.simple(date(year, month, 20), "6300", "1000", _pesos(rng, 80000, 150000), "Online ads", "Brightside Media")
        books.simple(date(year, month, 12), "6400", "1000", _pesos(rng, 30000, 50000), "Bookkeeping", "Reyes & Partners CPAs")
        books.simple(date(year, month, 18), "6500", "1000", _pesos(rng, 20000, 40000), "Sales team travel", "Cebu Pacific")
        books.simple(date(year, month, 22), "6600", "1000", _pesos(rng, 15000, 25000), "Delivery van fuel", "Petron")
        books.simple(last, "7000", "1510", to_cents("60000"), "Monthly depreciation", entry_type="manual", to_bank=False)
        books.simple(date(year, month, 28), "7100", "1000", to_cents("15000"), "Loan interest", "BPI")
        if month in (1, 4, 7, 10):
            books.simple(date(year, month, 15), "8000", "1000", to_cents("120000"), "Quarterly income tax", "BIR")

        if year == 2025:                                                                    # planted 1
            books.simple(date(year, month, 8), "6600", "1000", to_cents("45000"),
                         "Car lease - personal vehicle", OWNER)

    books.simple(date(2025, 7, 14), "6500", "1000", to_cents("180000"),
                 "Family vacation - Boracay", OWNER)                                        # planted 2
    books.simple(date(2025, 5, 14), "6400", "1000", to_cents("950000"),
                 "Legal settlement - supplier dispute", "Cruz Law Office")                  # planted 4
    books.simple(date(2025, 12, 30), "1100", "4000", to_cents("1200000"),
                 "Revenue accrual - January 2026 order", "Metro Retail Group",
                 entry_type="manual", to_bank=False)                                        # planted 5
    books.bank.append((date(2025, 3, 18), len(books.bank), "ATM withdrawal", -to_cents("250000")))  # planted 6

    planted.update({
        "owner_lines": 13,
        "related_lines": 24,
        "cutoff_dates": ["2025-12-30"],
        "unusual_dates": ["2025-05-14"],
        "cash_gaps": {
            "2024-12": planted["timing_cheque"],
            "2025-01": -planted["timing_cheque"],
            "2025-03": -to_cents("250000"),
        },
    })
    return books, planted


def generate(out_dir: str | Path, seed: int = 7) -> dict[str, Path]:
    """Write gl.csv, bank.csv, mapping.csv, rules.toml and planted.json into out_dir."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    books, planted = build_books(seed)

    gl_path = out / "gl.csv"
    with gl_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["date", "journal_id", "account_code", "account_name", "description",
                    "counterparty", "debit", "credit", "entry_type"])
        w.writerows(books.gl)

    bank_path = out / "bank.csv"
    balance = OPENING_BANK
    with bank_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["Date", "Description", "Withdrawal", "Deposit", "Balance"])
        for when, _, description, cents in sorted(books.bank):
            balance += cents
            w.writerow([when.isoformat(), description, _amount(-cents) if cents < 0 else "",
                        _amount(cents) if cents > 0 else "", fmt(balance)])

    mapping_path = out / "mapping.csv"
    with mapping_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["account_code", "account_name", "category"])
        w.writerows(ACCOUNTS)

    rules_path = out / "rules.toml"
    rules_path.write_text(
        '# Adjustment rule settings for Bayside Trading Corp.\n'
        'currency = "PHP"\n'
        'related_parties = ["Santos Holdings"]\n'
        'owner_keywords = ["owner", "personal", "family", "vacation", "shareholder"]\n'
        'fiscal_year_end_month = 12\n'
        'cash_tolerance = 1.00\n',
        encoding="utf-8",
    )

    planted_path = out / "planted.json"
    planted_path.write_text(json.dumps({"company": COMPANY, **planted}, indent=2) + "\n", encoding="utf-8")
    return {"gl": gl_path, "bank": bank_path, "mapping": mapping_path,
            "rules": rules_path, "planted": planted_path}
