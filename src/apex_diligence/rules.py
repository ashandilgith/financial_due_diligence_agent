"""Settings for the adjustment rules, read from a TOML file.

Defaults live in code so the tool works with no config file. A client file
only needs the keys it wants to change. Unknown keys are an error, so a typo
like `mad_multipler` cannot silently fall back to the default.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .money import to_cents


@dataclass(frozen=True)
class RuleConfig:
    owner_keywords: tuple[str, ...] = ("owner", "personal", "family", "vacation", "shareholder")
    related_parties: tuple[str, ...] = ()
    unusual_min_amount: int = to_cents("100000")   # cents
    unusual_mad_multiplier: float = 6.0
    unusual_min_history: int = 6
    cutoff_days: int = 5
    cutoff_min_amount: int = to_cents("50000")     # cents
    fiscal_year_end_month: int = 12
    cash_tolerance: int = to_cents("1.00")         # cents
    currency: str = "PHP"


# TOML keys use currency units; these are converted to cents on load.
_MONEY_KEYS = {"unusual_min_amount", "cutoff_min_amount", "cash_tolerance"}


def load_rules(path: str | Path | None) -> tuple[RuleConfig, bytes]:
    """Return the config and the raw bytes (the bytes go into the run manifest)."""
    if path is None:
        return RuleConfig(), b""
    raw = Path(path).read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"{Path(path).name}: not valid TOML: {e}") from None

    allowed = {f.name: f for f in fields(RuleConfig)}
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValueError(f"{Path(path).name}: unknown setting(s): {', '.join(unknown)}")

    values = {}
    for key, value in data.items():
        if key in _MONEY_KEYS:
            values[key] = to_cents(value)
        elif key in ("owner_keywords", "related_parties"):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of strings")
            values[key] = tuple(v.strip() for v in value if v.strip())
        else:
            expected = type(getattr(RuleConfig(), key))
            ok = isinstance(value, expected) and not isinstance(value, bool)
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                ok, value = True, float(value)
            if not ok:
                raise ValueError(f"{key} must be a {expected.__name__}, got {value!r}")
            values[key] = value
    config = RuleConfig(**values)
    if not 1 <= config.fiscal_year_end_month <= 12:
        raise ValueError("fiscal_year_end_month must be 1-12")
    return config, raw
