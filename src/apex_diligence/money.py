"""Money handling.

Every amount in Apex Diligence is an integer number of cents (centavos).
Floats are never used for money: 0.1 + 0.2 != 0.3 in floating point, and a
diligence tool that is off by a centavo cannot claim that its numbers tie out.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Characters and currency codes that may appear around a number in an export.
_NOISE = re.compile(r"[,\s ₱$€£]|PHP|LKR|USD|SGD|AUD", re.IGNORECASE)
# "1234,5" or "1234,50" with no dot: a comma used as the decimal point.
_COMMA_DECIMAL = re.compile(r"^[^.]*,\d{1,2}\)?$")


class AmountError(ValueError):
    """Raised when a value cannot be read as an amount."""


def to_cents(value: object) -> int:
    """Convert a cell value to integer cents.

    Accepts numbers and strings such as "1,234.50", "(500.00)", "-12" and
    "PHP 1,000". Blank cells and a lone "-" (accountants' zero) become 0.
    Comma decimals ("1234,50" or "1.234,50") are refused rather than guessed:
    a silent misread would be off by a factor of 100.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        raise AmountError(f"not an amount: {value!r}")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        # repr() gives the shortest string that round-trips, e.g. 1234.5 -> "1234.5"
        value = repr(value)

    text = str(value).strip().replace("−", "-")  # unicode minus sign
    if text in ("", "-"):
        return 0

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]

    if _COMMA_DECIMAL.search(text) or ("," in text and "." in text and text.rfind(",") > text.rfind(".")):
        raise AmountError(f"comma used as decimal point: {value!r}")

    text = _NOISE.sub("", text)
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise AmountError(f"not an amount: {value!r}") from None
    if not number.is_finite():
        raise AmountError(f"not an amount: {value!r}")

    cents = int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


class Money(int):
    """An int of cents that is marked as money, so it serialises as "1234.50"."""

    def __repr__(self) -> str:
        return f"Money({fmt(self)})"


def mul(cents: int, rate: Decimal | int) -> int:
    """cents x rate, rounded half away from zero to whole cents.
    Matches Excel's ROUND(x, 2), so exported models agree to the centavo."""
    return int((Decimal(cents) * Decimal(rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ratio(numerator: int, denominator: int, places: int = 6) -> Decimal | None:
    """numerator / denominator as a Decimal rounded to `places`; None if dividing by zero."""
    if denominator == 0:
        return None
    q = Decimal(10) ** -places
    return (Decimal(numerator) / Decimal(denominator)).quantize(q, rounding=ROUND_HALF_UP)


def money_str(cents: int) -> str:
    """Cents -> "1234.50" with no separators: unambiguous for machines and models."""
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(int(cents)), 100)
    return f"{sign}{whole}.{part:02d}"


def to_decimal(cents: int) -> Decimal:
    """Cents -> Decimal currency units, exact (used when writing Excel)."""
    return Decimal(cents) / 100


def fmt(cents: int) -> str:
    """Cents -> "1,234.50" / "-1,234.50" for messages and logs."""
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}{whole:,}.{part:02d}"
