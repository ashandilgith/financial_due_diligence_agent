"""Rules that propose EBITDA adjustments.

Each rule looks at ledger lines and proposes a Candidate with its evidence.
Nothing here changes EBITDA: a reviewer accepts or rejects every candidate.

Rules run in priority order and each ledger line can produce at most ONE
candidate. That stops the same transaction being added back twice when two
rules both match it.
"""

from __future__ import annotations

import calendar
import hashlib
import re
from statistics import median

from .models import Candidate, GLLine
from .money import fmt
from .rules import RuleConfig
from .statements import month_of
from .taxonomy import EBITDA_COSTS


def candidate_id(rule: str, line: GLLine) -> str:
    """A stable id: the same line and rule give the same id on every run,
    so a reviewer's decision file keeps working after the data is re-run."""
    key = f"{rule}|{line.journal_id}|{line.account_code}|{line.date}|{line.net}|{line.description}"
    return f"{rule}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def _make(rule: str, title: str, line: GLLine, effect: int, rationale: str) -> Candidate:
    return Candidate(
        id=candidate_id(rule, line),
        rule=rule,
        title=title,
        month=month_of(line.date),
        date=line.date,
        account_code=line.account_code,
        account_name=line.account_name,
        counterparty=line.counterparty,
        description=line.description,
        amount_involved=abs(line.net),
        proposed_effect=effect,
        rationale=rationale,
        evidence=(line.source,),
    )


def _words(terms: tuple[str, ...]) -> re.Pattern | None:
    if not terms:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)


# ------------------------------------------------------------------ rules

def owner_expense(line: GLLine, category: str, config: RuleConfig) -> Candidate | None:
    """OWN: costs that look personal to the owner -> propose adding them back."""
    pattern = _words(config.owner_keywords)
    if category not in EBITDA_COSTS or line.net <= 0 or pattern is None:
        return None
    hit = pattern.search(f"{line.description} {line.counterparty}")
    if not hit:
        return None
    return _make("OWN", "Owner or personal expense", line, +line.net,
                 f"matched '{hit.group(0)}'; add back if not a business cost")


def related_party(line: GLLine, category: str, config: RuleConfig) -> Candidate | None:
    """REL: any P&L line with a related party -> review at arm's length.
    The proposed effect is 0 because the right adjustment (market rate minus
    actual) needs judgement; the reviewer enters it as an override."""
    pattern = _words(config.related_parties)
    if pattern is None or category not in EBITDA_COSTS + ("revenue",):
        return None
    hit = pattern.search(line.counterparty)
    if not hit:
        return None
    return _make("REL", "Related-party transaction", line, 0,
                 f"counterparty '{line.counterparty}' is a related party; compare to market terms")


def revenue_cutoff(line: GLLine, category: str, config: RuleConfig) -> Candidate | None:
    """CUT: large manual revenue entries in the last days of the fiscal year -> deduct."""
    if category != "revenue" or line.entry_type != "manual":
        return None
    amount = -line.net  # revenue is a credit
    if amount < config.cutoff_min_amount or line.date.month != config.fiscal_year_end_month:
        return None
    last_day = calendar.monthrange(line.date.year, line.date.month)[1]
    if last_day - line.date.day >= config.cutoff_days:
        return None
    return _make("CUT", "Year-end revenue cut-off risk", line, -amount,
                 f"manual revenue entry {last_day - line.date.day} day(s) before year end; "
                 "check delivery happened in this period")


class UnusualAmount:
    """UNU: a cost far larger than that account's normal lines -> possible one-off.

    'Far larger' uses the median and the median absolute deviation (MAD),
    which a single huge value cannot drag upwards the way it drags a mean.
    """

    def __init__(self, gl: list[GLLine], mapping: dict[str, str], config: RuleConfig):
        self.config = config
        self.limits: dict[str, int] = {}
        by_account: dict[str, list[int]] = {}
        for line in gl:
            if mapping[line.account_code] in EBITDA_COSTS and line.net > 0 and line.entry_type != "opening":
                by_account.setdefault(line.account_code, []).append(line.net)
        for account, amounts in by_account.items():
            if len(amounts) < config.unusual_min_history:
                continue
            mid = median(amounts)
            mad = median(abs(a - mid) for a in amounts)
            # When every line is the same (MAD = 0), anything above double the norm is unusual.
            limit = mid + config.unusual_mad_multiplier * mad if mad else 2 * mid
            self.limits[account] = max(int(limit), config.unusual_min_amount)

    def __call__(self, line: GLLine, category: str, config: RuleConfig) -> Candidate | None:
        limit = self.limits.get(line.account_code)
        if limit is None or category not in EBITDA_COSTS or line.net <= limit:
            return None
        return _make("UNU", "Unusually large cost (possible one-off)", line, +line.net,
                     f"{fmt(line.net)} is above this account's usual range (limit {fmt(limit)}); "
                     "add back only if it will not recur")


def find_candidates(gl: list[GLLine], mapping: dict[str, str], config: RuleConfig) -> list[Candidate]:
    rules = [owner_expense, related_party, revenue_cutoff, UnusualAmount(gl, mapping, config)]
    found = []
    for line in gl:
        if line.entry_type == "opening":
            continue
        category = mapping[line.account_code]
        for rule in rules:
            candidate = rule(line, category, config)
            if candidate is not None:
                found.append(candidate)
                break  # one candidate per line
    found.sort(key=lambda c: (c.date, c.rule, c.id))
    return found
