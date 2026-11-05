# Apex Diligence

Turns a company's general ledger and bank statements into a reviewed
quality-of-earnings databook: monthly P&L and EBITDA, proof of cash, working
capital, customer concentration, and proposed EBITDA adjustments, each traced
back to the exact source row.

- **Deterministic.** All money is integer cents. No floats, no clock, no randomness: the same inputs always give byte-identical outputs, and `apex verify` proves it.
- **Traceable.** Every finding cites its source, e.g. `gl.csv:1437`.
- **Reviewer in control.** Rules only *propose* adjustments. Nothing changes adjusted EBITDA until a reviewer accepts it.
- **Gated.** Unbalanced journals or unmapped accounts stop the run. Cash gaps and other findings are flagged on the front page.
- **Offline.** No network calls. Runs on a laptop or an air-gapped server.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

apex demo --out demo_output        # builds a sample company and runs everything
apex verify demo_output/run.json   # re-runs and proves the result is identical
pytest                             # 54 tests
```

Open `demo_output/databook.xlsx`. The sample company, Bayside Trading Corp.,
has seven planted problems (see `src/apex_diligence/synthetic.py`); the
databook should surface all of them.

## Running on real files

```bash
apex run --gl gl.csv --bank bank.csv --mapping mapping.csv \
         --rules rules.toml --company "Target Co." --out out/
```

Then open `out/review.csv`, fill the `decision` column (`accept` / `reject`,
optional `amount_override`), and re-run with `--decisions out/review.csv`
(write the next run to a new `--out` folder).

| File | Required columns (aliases accepted) |
| --- | --- |
| General ledger | `date`, `journal_id`, `account_code`, `debit`, `credit`; optional `account_name`, `description`, `counterparty`, `entry_type` (`auto` / `manual` / `opening`) |
| Bank statement | `date` and either `amount`, or `deposit` + `withdrawal`; optional `description`, `balance` |
| Mapping | `account_code`, `category` (see `taxonomy.py`) |
| Rules (optional) | TOML; see `data/rules.toml` written by `apex generate` |

Dates must be `YYYY-MM-DD` (or real Excel dates). Ambiguous formats such as
`03/04/2025` are refused rather than guessed.

## Layout

```
src/apex_diligence/
  money.py          integer-cents parsing
  models.py         data shapes
  ingest.py         reading and validating files, with row lineage
  taxonomy.py       the standard chart of accounts
  statements.py     monthly P&L, balances, working capital, customers
  checks.py         blocker and warning checks
  proof_of_cash.py  book vs bank cash by month
  rules.py          adjustment rule settings (TOML)
  adjustments.py    rules that propose adjustments
  review.py         applying reviewer decisions
  pipeline.py       one run, start to finish
  databook.py       Excel output with live formulas
  outputs.py        writing files, manifest, verify
  synthetic.py      sample company with planted problems
  cli.py            the `apex` command
tests/
```

Exit codes: `0` done, `1` unreadable input, `2` blocked by checks, `3` verification failed.
