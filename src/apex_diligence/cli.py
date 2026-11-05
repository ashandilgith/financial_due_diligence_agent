"""Command line: apex demo | generate | run | verify.

Exit codes: 0 = done, 1 = input files could not be read, 2 = blocked by checks,
3 = verification failed.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .ingest import IngestError
from .money import fmt
from .outputs import verify, write_outputs
from .pipeline import BlockedRun, RunInputs, RunResult, run
from .synthetic import COMPANY, generate


def _summary(result: RunResult, written) -> str:
    s = result.statements
    status = Counter(r.status for r in result.reviewed)
    failing = [c for c in result.checks if not c.passed]
    lines = [
        f"{result.company}: {s.months[0]} to {s.months[-1]} ({result.config.currency})",
        f"  LTM revenue            {fmt(s.ltm(s.pl['revenue'])):>18}",
        f"  LTM reported EBITDA    {fmt(s.ltm(s.pl['ebitda'])):>18}",
        f"  Accepted adjustments   {fmt(s.ltm(result.adjustments)):>18}",
        f"  LTM adjusted EBITDA    {fmt(s.ltm(result.adjusted_ebitda)):>18}",
        f"  Candidates: {len(result.reviewed)} "
        f"(accepted {status['accepted']}, rejected {status['rejected']}, pending {status['pending']})",
        f"  Checks: {len(result.checks) - len(failing)} passed, {len(failing)} need attention",
    ]
    lines += [f"    ! {c.name}: {c.detail}" for c in failing]
    lines += ["", f"  Databook: {written.databook}"]
    if written.review:
        lines.append(f"  Review:   {written.review}  (fill 'decision', re-run with --decisions)")
    lines.append(f"  Manifest: {written.manifest}")
    return "\n".join(lines)


def _run_and_write(inputs: RunInputs, out: Path) -> int:
    try:
        result = run(inputs)
    except IngestError as e:
        print(f"Could not read input:\n{e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Invalid settings: {e}", file=sys.stderr)
        return 1
    except BlockedRun as e:
        print(str(e), file=sys.stderr)
        return 2
    written = write_outputs(inputs, result, out)
    print(_summary(result, written))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apex", description="Apex Diligence: quality-of-earnings data packs")
    parser.add_argument("--version", action="version", version=f"apex-diligence {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="generate the sample company and run the full pipeline")
    demo.add_argument("--out", type=Path, default=Path("demo_output"))

    gen = sub.add_parser("generate", help="write the sample company's files")
    gen.add_argument("--out", type=Path, required=True)
    gen.add_argument("--seed", type=int, default=7)

    r = sub.add_parser("run", help="run on a client's files")
    r.add_argument("--gl", type=Path, required=True, help="general ledger (.csv or .xlsx)")
    r.add_argument("--bank", type=Path, required=True, help="bank statement (.csv or .xlsx)")
    r.add_argument("--mapping", type=Path, required=True, help="account mapping (.csv or .xlsx)")
    r.add_argument("--rules", type=Path, help="rule settings (.toml)")
    r.add_argument("--decisions", type=Path, help="reviewer decisions, e.g. an edited review.csv")
    r.add_argument("--company", default="Target company")
    r.add_argument("--out", type=Path, required=True)

    v = sub.add_parser("verify", help="re-run from a run.json and confirm identical results")
    v.add_argument("manifest", type=Path)

    args = parser.parse_args(argv)

    if args.command == "generate":
        files = generate(args.out, args.seed)
        print("\n".join(f"wrote {p}" for p in files.values()))
        return 0

    if args.command == "demo":
        files = generate(args.out / "data")
        inputs = RunInputs(gl=files["gl"], bank=files["bank"], mapping=files["mapping"],
                           rules=files["rules"], company=COMPANY)
        return _run_and_write(inputs, args.out)

    if args.command == "run":
        inputs = RunInputs(gl=args.gl, bank=args.bank, mapping=args.mapping, rules=args.rules,
                           decisions=args.decisions, company=args.company)
        return _run_and_write(inputs, args.out)

    if args.command == "verify":
        try:
            problems = verify(args.manifest)
        except (IngestError, BlockedRun, ValueError, KeyError, FileNotFoundError) as e:
            print(f"Could not verify: {e}", file=sys.stderr)
            return 3
        if problems:
            print("NOT VERIFIED:\n" + "\n".join(f"  - {p}" for p in problems))
            return 3
        print("Verified: inputs unchanged, results and databook identical.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
