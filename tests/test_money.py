import pytest

from apex_diligence.money import AmountError, fmt, to_cents


@pytest.mark.parametrize("value, cents", [
    (None, 0), ("", 0), ("-", 0), (0, 0),
    (12, 1200), (1234.5, 123450), (0.1, 10), (0.105, 11),
    ("1,234.50", 123450), ("(500.00)", -50000), ("-12", -1200),
    ("PHP 1,000", 100000), ("₱2,500.75", 250075), ("  42.01 ", 4201),
    ("−5", -500),
])
def test_to_cents_reads_common_formats(value, cents):
    assert to_cents(value) == cents


@pytest.mark.parametrize("value", ["abc", "12.3.4", "1234,50", "1.234,50", "nan", "inf", True])
def test_to_cents_refuses_what_it_cannot_read_safely(value):
    with pytest.raises(AmountError):
        to_cents(value)


def test_no_float_drift():
    # 0.1 + 0.2 != 0.3 in floats; in cents it is exact.
    assert to_cents("0.1") + to_cents("0.2") == to_cents("0.3")


def test_fmt():
    assert fmt(123456789) == "1,234,567.89"
    assert fmt(-5) == "-0.05"
