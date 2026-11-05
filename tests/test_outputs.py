import csv
import shutil
import subprocess
from dataclasses import replace

import openpyxl
import pytest

from apex_diligence.cli import main
from apex_diligence.money import to_cents
from apex_diligence.outputs import verify, write_outputs
from apex_diligence.pipeline import run


def test_outputs_are_byte_identical_and_verify(demo_inputs, demo_result, tmp_path):
    a = write_outputs(demo_inputs, demo_result, tmp_path / "a")
    b = write_outputs(demo_inputs, run(demo_inputs), tmp_path / "b")
    assert a.databook.read_bytes() == b.databook.read_bytes()
    assert a.review.read_bytes() == b.review.read_bytes()
    assert verify(a.manifest) == []


def test_verify_catches_changed_input_and_edited_databook(demo_files, demo_inputs, tmp_path):
    data = tmp_path / "data"
    shutil.copytree(demo_files["gl"].parent, data)
    inputs = replace(demo_inputs, gl=data / "gl.csv", bank=data / "bank.csv",
                     mapping=data / "mapping.csv", rules=data / "rules.toml")
    written = write_outputs(inputs, run(inputs), tmp_path / "out")

    written.databook.write_bytes(written.databook.read_bytes() + b" ")
    assert any("edited" in p for p in verify(written.manifest))

    with (data / "bank.csv").open("a") as f:
        f.write("2025-12-31,Late fee,10.00,,0\n")
    assert any("changed" in p for p in verify(written.manifest))


def test_review_csv_round_trip(demo_inputs, demo_result, tmp_path):
    """The reviewer edits review.csv and passes it back: decisions take effect
    and are carried into the next review.csv."""
    out = tmp_path / "round1"
    written = write_outputs(demo_inputs, demo_result, out)
    rows = list(csv.DictReader(written.review.open()))
    for row in rows:
        if row["rule"] == "OWN":
            row["decision"] = "accept"
    edited = tmp_path / "decisions.csv"
    with edited.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    inputs = replace(demo_inputs, decisions=edited)
    result = run(inputs)
    owner_total = sum(to_cents(r["proposed_effect"]) for r in rows if r["rule"] == "OWN")
    assert sum(result.adjustments) == owner_total > 0

    second = write_outputs(inputs, result, tmp_path / "round2")
    carried = [r for r in csv.DictReader(second.review.open()) if r["decision"] == "accept"]
    assert len(carried) == 13


def test_databook_has_live_formulas(demo_inputs, demo_result, tmp_path):
    written = write_outputs(demo_inputs, demo_result, tmp_path)
    wb = openpyxl.load_workbook(written.databook)
    assert wb.sheetnames == ["Summary", "P&L", "Adjusted EBITDA", "Adjustments", "Proof of cash",
                             "Working capital", "Customers", "Checks"]
    pl = wb["P&L"]
    assert pl["B9"].value == "=B7-B8"                    # EBITDA
    assert pl["A9"].value == "Reported EBITDA"
    # A = labels, B..Y = 24 months, Z = FY2024, AA = FY2025, AB = LTM
    assert pl["Z4"].value == "FY2024" and pl["AB4"].value == "LTM"
    assert pl["N4"].value == "2025-01" and pl["Y4"].value == "2025-12"
    assert pl["AB9"].value == "=SUM(N9:Y9)"            # exactly the last 12 months
    assert wb["Summary"]["B14"].value.startswith("='Adjusted EBITDA'!")


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_formulas_evaluate_to_python_values(demo_inputs, demo_result, tmp_path):
    """Open the databook in LibreOffice and check its formulas give the same
    answers as the Python pipeline."""
    accept = [[r.candidate.id, "accept"] for r in demo_result.reviewed if r.candidate.rule in ("OWN", "CUT")]
    decisions = tmp_path / "d.csv"
    decisions.write_text("candidate_id,decision\n" + "\n".join(",".join(r) for r in accept) + "\n")
    inputs = replace(demo_inputs, decisions=decisions)
    demo_result = run(inputs)
    assert sum(demo_result.adjustments) != 0
    written = write_outputs(inputs, demo_result, tmp_path)
    subprocess.run(["soffice", "--headless", "--convert-to", "csv", "--outdir", str(tmp_path),
                    str(written.databook)], check=True, capture_output=True, timeout=180)
    summary = {row[0]: row[1] for row in csv.reader((tmp_path / "databook.csv").open()) if len(row) > 1}
    s = demo_result.statements
    assert to_cents(summary["Revenue"]) == s.ltm(s.pl["revenue"])
    assert to_cents(summary["Reported EBITDA"]) == s.ltm(s.pl["ebitda"])
    assert to_cents(summary["Adjusted EBITDA"]) == s.ltm(demo_result.adjusted_ebitda)
    assert to_cents(summary["Accepted adjustments"]) == s.ltm(demo_result.adjustments)
    assert summary["Months where proof of cash does not tie"] == "3"
    assert summary["Candidates accepted"] == "14"
    assert summary["Candidates pending"] == "25"


def test_cli_demo_and_verify(tmp_path, capsys):
    assert main(["demo", "--out", str(tmp_path)]) == 0
    assert "LTM adjusted EBITDA" in capsys.readouterr().out
    assert main(["verify", str(tmp_path / "run.json")]) == 0


def test_cli_reports_bad_input(tmp_path, capsys):
    (tmp_path / "gl.csv").write_text("date,journal_id\n2025-01-01,J1\n")
    code = main(["run", "--gl", str(tmp_path / "gl.csv"), "--bank", str(tmp_path / "gl.csv"),
                 "--mapping", str(tmp_path / "gl.csv"), "--out", str(tmp_path / "out")])
    assert code == 1
    assert "missing required column" in capsys.readouterr().err
