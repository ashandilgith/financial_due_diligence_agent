"""End-to-end tests on the synthetic company: every planted problem must be found."""

from collections import Counter
from dataclasses import replace

import pytest

from apex_diligence.money import to_cents
from apex_diligence.pipeline import BlockedRun, run
from apex_diligence.rules import load_rules
from conftest import write_csv


def test_every_planted_adjustment_is_found(demo_result, planted):
    rules = Counter(r.candidate.rule for r in demo_result.reviewed)
    assert rules["OWN"] == planted["owner_lines"]
    assert rules["REL"] == planted["related_lines"]
    cut = [r.candidate.date.isoformat() for r in demo_result.reviewed if r.candidate.rule == "CUT"]
    unu = [r.candidate.date.isoformat() for r in demo_result.reviewed if r.candidate.rule == "UNU"]
    assert cut == planted["cutoff_dates"]
    assert unu == planted["unusual_dates"]


def test_no_false_alarms(demo_result):
    # 13 owner + 24 related + 1 cut-off + 1 one-off; nothing else is flagged.
    assert len(demo_result.reviewed) == 39


def test_proof_of_cash_finds_exactly_the_planted_gaps(demo_result, planted):
    gaps = {m.month: m.difference for m in demo_result.cash if not m.ties}
    assert gaps == planted["cash_gaps"]


def test_books_balance_identity(demo_result):
    """Accounting identity: closing balance-sheet total (debit-positive) equals
    cumulative net income. If this fails, money was lost somewhere."""
    s = demo_result.statements
    closing = sum(series[-1] for series in s.balances.values())
    assert closing == sum(s.pl["net_income"])


def test_ebitda_arithmetic(demo_result):
    pl = demo_result.statements.pl
    for i in range(len(demo_result.statements.months)):
        assert pl["ebitda"][i] == pl["revenue"][i] - pl["cogs"][i] - pl["opex"][i]


def test_pending_candidates_do_not_change_ebitda(demo_result):
    assert demo_result.adjusted_ebitda == demo_result.statements.pl["ebitda"]


def test_decisions_drive_adjusted_ebitda(demo_inputs, demo_result, tmp_path):
    reviewed = demo_result.reviewed
    own = [r.candidate for r in reviewed if r.candidate.rule == "OWN"]
    unu = next(r.candidate for r in reviewed if r.candidate.rule == "UNU")
    cut = next(r.candidate for r in reviewed if r.candidate.rule == "CUT")
    rel = next(r.candidate for r in reviewed if r.candidate.rule == "REL")
    rows = [[c.id, "accept", "", ""] for c in own]
    rows += [[unu.id, "reject", "", "recurring"], [cut.id, "accept", "", ""], [rel.id, "accept", "30000", "market"]]
    decisions = write_csv(tmp_path / "d.csv", ["candidate_id", "decision", "amount_override", "note"], rows)

    result = run(replace(demo_inputs, decisions=decisions))
    expected = sum(c.proposed_effect for c in own) + cut.proposed_effect + to_cents("30000")
    assert sum(result.adjustments) == expected
    assert sum(result.adjusted_ebitda) == sum(result.statements.pl["ebitda"]) + expected
    status = Counter(r.status for r in result.reviewed)
    assert status == {"accepted": 15, "rejected": 1, "pending": 23}


def test_stale_decision_is_reported(demo_inputs, tmp_path):
    decisions = write_csv(tmp_path / "d.csv", ["candidate_id", "decision"], [["OWN-deadbeef", "accept"]])
    result = run(replace(demo_inputs, decisions=decisions))
    check = next(c for c in result.checks if c.name.startswith("Reviewer decisions"))
    assert not check.passed and "OWN-deadbeef" in check.detail


def test_unbalanced_journal_blocks_the_run(demo_inputs, tmp_path):
    lines = demo_inputs.gl.read_text().splitlines()
    lines.append("2025-06-01,JE-99999,1000,Cash at bank,Broken,,100.00,,auto")
    gl = tmp_path / "gl.csv"
    gl.write_text("\n".join(lines) + "\n")
    with pytest.raises(BlockedRun, match="JE-99999"):
        run(replace(demo_inputs, gl=gl))


def test_unmapped_account_blocks_the_run(demo_inputs, tmp_path):
    rows = [l for l in demo_inputs.mapping.read_text().splitlines() if not l.startswith("6300")]
    mapping = tmp_path / "mapping.csv"
    mapping.write_text("\n".join(rows) + "\n")
    with pytest.raises(BlockedRun, match="6300"):
        run(replace(demo_inputs, mapping=mapping))


def test_rules_file_validation(tmp_path):
    bad = tmp_path / "r.toml"
    bad.write_text("mad_multipler = 3\n")
    with pytest.raises(ValueError, match="unknown setting"):
        load_rules(bad)
    bad.write_text('unusual_min_history = "six"\n')
    with pytest.raises(ValueError, match="must be a int"):
        load_rules(bad)
    good = tmp_path / "g.toml"
    good.write_text("unusual_mad_multiplier = 4\ncash_tolerance = 5\n")
    config, raw = load_rules(good)
    assert config.unusual_mad_multiplier == 4.0 and config.cash_tolerance == 500
    assert raw == good.read_bytes()


def test_runs_are_deterministic(demo_inputs):
    assert run(demo_inputs).results_hash() == run(demo_inputs).results_hash()
