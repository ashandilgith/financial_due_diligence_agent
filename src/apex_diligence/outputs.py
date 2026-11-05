"""Writing a run's outputs and proving it can be reproduced.

Each run writes three files:
  databook.xlsx  the Excel databook
  review.csv     every adjustment candidate, with columns for the reviewer's decision
  run.json       the manifest: fingerprints of every input and output

`verify` re-runs from the manifest and confirms the results and the databook
come out byte-for-byte identical.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .databook import build_databook
from .money import to_decimal
from .pipeline import RunInputs, RunResult, run, sha256_bytes

MANIFEST = "run.json"
DATABOOK = "databook.xlsx"
REVIEW = "review.csv"

REVIEW_HEADERS = ["candidate_id", "rule", "finding", "date", "account", "counterparty", "description",
                  "amount_involved", "proposed_effect", "evidence", "why_flagged",
                  "decision", "amount_override", "note"]


def review_csv(result: RunResult, decisions_path: Path | None) -> bytes:
    """The reviewer's worksheet. Existing decisions are carried over, so the
    same file can be edited, passed back with --decisions, and re-run."""
    previous = {}
    if decisions_path:
        from .ingest import load_decisions
        previous = load_decisions(decisions_path)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REVIEW_HEADERS)
    for rc in result.reviewed:
        c = rc.candidate
        d = previous.get(c.id)
        writer.writerow([
            c.id, c.rule, c.title, c.date.isoformat(), f"{c.account_code} {c.account_name}".strip(),
            c.counterparty, c.description, to_decimal(c.amount_involved), to_decimal(c.proposed_effect),
            " ".join(str(e) for e in c.evidence), c.rationale,
            ("accept" if d.accepted else "reject") if d else "",
            to_decimal(d.amount_override) if d and d.amount_override is not None else "",
            d.note if d else "",
        ])
    return buffer.getvalue().encode("utf-8")


def _file_entry(path: Path, base: Path) -> dict:
    return {"path": _relative(path, base), "sha256": sha256_bytes(path.read_bytes())}


def _relative(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


@dataclass(frozen=True)
class Written:
    databook: Path
    review: Path | None
    manifest: Path
    results_hash: str


def write_outputs(inputs: RunInputs, result: RunResult, out_dir: Path) -> Written:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_hash = result.results_hash()

    databook_path = out_dir / DATABOOK
    databook_bytes = build_databook(result, results_hash)
    databook_path.write_bytes(databook_bytes)

    review_path: Path | None = out_dir / REVIEW
    if inputs.decisions and Path(inputs.decisions).resolve() == review_path.resolve():
        review_path = None  # never overwrite the reviewer's own file while it is an input
    else:
        review_path.write_bytes(review_csv(result, inputs.decisions))

    files = {"gl": inputs.gl, "bank": inputs.bank, "mapping": inputs.mapping,
             "rules": inputs.rules, "decisions": inputs.decisions}
    manifest = {
        "tool": "apex-diligence",
        "version": __version__,
        "company": inputs.company,
        "inputs": {name: _file_entry(Path(p), out_dir) for name, p in files.items() if p},
        "results_sha256": results_hash,
        "outputs": {"databook": {"path": DATABOOK, "sha256": sha256_bytes(databook_bytes)}},
        "checks": [{"name": c.name, "severity": c.severity, "passed": c.passed} for c in result.checks],
    }
    manifest_path = out_dir / MANIFEST
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return Written(databook_path, review_path, manifest_path, results_hash)


def verify(manifest_path: Path) -> list[str]:
    """Re-run from a manifest. Returns a list of problems; empty means verified."""
    manifest_path = Path(manifest_path)
    base = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []

    paths = {}
    for name, entry in manifest["inputs"].items():
        path = (base / entry["path"]).resolve()
        paths[name] = path
        if not path.exists():
            problems.append(f"input '{name}' is missing: {path}")
        elif sha256_bytes(path.read_bytes()) != entry["sha256"]:
            problems.append(f"input '{name}' has changed since the run: {path}")
    if problems:
        return problems

    inputs = RunInputs(gl=paths["gl"], bank=paths["bank"], mapping=paths["mapping"],
                       rules=paths.get("rules"), decisions=paths.get("decisions"),
                       company=manifest["company"])
    result = run(inputs)
    results_hash = result.results_hash()
    if results_hash != manifest["results_sha256"]:
        problems.append("results differ from the original run")
    expected = manifest["outputs"]["databook"]["sha256"]
    if sha256_bytes(build_databook(result, results_hash)) != expected:
        problems.append("databook differs from the original run")
    recorded = base / manifest["outputs"]["databook"]["path"]
    if recorded.exists() and sha256_bytes(recorded.read_bytes()) != expected:
        problems.append("the databook on disk was edited after the run")
    return problems
