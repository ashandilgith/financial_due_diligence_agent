"""One run, start to finish: read -> check -> compute -> review -> results.

The pipeline is a straight line of plain function calls. No hidden state, no
clock, no randomness: the same input files always give the same results.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from . import checks
from .adjustments import find_candidates
from .ingest import load_bank, load_decisions, load_gl, load_mapping
from .models import CheckResult, ReviewedCandidate
from .proof_of_cash import CashMonth, proof_of_cash
from .review import adjustments_by_month, apply_decisions
from .rules import RuleConfig, load_rules
from .statements import Statements, build_statements, revenue_by_customer


class BlockedRun(Exception):
    """A blocker check failed: the numbers cannot be trusted, so nothing is produced."""

    def __init__(self, results: list[CheckResult]):
        self.results = results
        failed = "\n".join(f"  - {r.name}: {r.detail}" for r in results if not r.passed)
        super().__init__(f"run blocked by failed checks:\n{failed}")


@dataclass(frozen=True)
class RunInputs:
    gl: Path
    bank: Path
    mapping: Path
    rules: Path | None = None
    decisions: Path | None = None
    company: str = "Target company"


@dataclass(frozen=True)
class RunResult:
    company: str
    config: RuleConfig
    statements: Statements
    adjustments: list[int]        # accepted adjustments per month
    adjusted_ebitda: list[int]
    reviewed: list[ReviewedCandidate]
    cash: list[CashMonth]
    customers: list[tuple[str, int]]   # last-12-month revenue by customer
    checks: list[CheckResult]
    stale_decisions: list[str]

    def to_dict(self) -> dict:
        """Every computed number, in plain JSON types. Its hash proves reproducibility."""
        s = self.statements
        return {
            "company": self.company,
            "months": s.months,
            "pl": s.pl,
            "balances": s.balances,
            "nwc": s.nwc,
            "adjustments": self.adjustments,
            "adjusted_ebitda": self.adjusted_ebitda,
            "candidates": [
                {"id": r.candidate.id, "rule": r.candidate.rule, "month": r.candidate.month,
                 "amount_involved": r.candidate.amount_involved,
                 "proposed_effect": r.candidate.proposed_effect,
                 "status": r.status, "final_effect": r.final_effect,
                 "evidence": [str(e) for e in r.candidate.evidence]}
                for r in self.reviewed
            ],
            "cash": [[c.month, c.book_net, c.bank_net, c.difference, c.ties] for c in self.cash],
            "customers": [[name, amount] for name, amount in self.customers],
            "checks": [[c.name, c.severity, c.passed, c.detail] for c in self.checks],
            "stale_decisions": self.stale_decisions,
        }

    def results_hash(self) -> str:
        return sha256_bytes(canonical_json(self.to_dict()))


def canonical_json(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(inputs: RunInputs) -> RunResult:
    config, _ = load_rules(inputs.rules)
    gl = load_gl(inputs.gl)
    bank = load_bank(inputs.bank)
    mapping = load_mapping(inputs.mapping)
    decisions = load_decisions(inputs.decisions) if inputs.decisions else {}

    gate = checks.blocking_checks(gl, mapping)
    if not all(c.passed for c in gate):
        raise BlockedRun(gate)

    statements = build_statements(gl, mapping)
    cash = proof_of_cash(gl, bank, mapping, config.cash_tolerance)
    candidates = find_candidates(gl, mapping, config)
    reviewed, stale = apply_decisions(candidates, decisions)
    adjustments = adjustments_by_month(reviewed, statements.months)
    adjusted = [e + a for e, a in zip(statements.pl["ebitda"], adjustments)]
    customers = revenue_by_customer(gl, mapping, statements.months[-12:])

    all_checks = gate + [
        checks.no_duplicate_lines(gl),
        checks.bank_balance_continuity(bank),
        checks.proof_of_cash_ties(cash, config.cash_tolerance),
        CheckResult("Reviewer decisions match current candidates", not stale, checks.WARNING,
                    "all decisions match" if not stale else f"{len(stale)} decision(s) for unknown candidates: "
                    + ", ".join(stale[:5])),
    ]
    return RunResult(
        company=inputs.company,
        config=config,
        statements=statements,
        adjustments=adjustments,
        adjusted_ebitda=adjusted,
        reviewed=reviewed,
        cash=cash,
        customers=customers,
        checks=all_checks,
        stale_decisions=stale,
    )
