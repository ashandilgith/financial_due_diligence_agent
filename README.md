# Apex Diligence

Turns a company's general ledger and bank statements into a reviewed
quality-of-earnings databook, standard deal models (13-week cash flow, LBO,
DCF, working-capital peg, price bridge, credit metrics), and a chat analyst
that answers questions by calling those models. Every number traces back to
the exact source row.

- **Deterministic.** All money is integer cents. The same inputs always give byte-identical outputs; `apex verify` and `apex replay` prove it.
- **AI reads, code computes.** The chat model only chooses tools and explains results. It never produces a number itself.
- **Traceable.** Every finding cites its source, e.g. `gl.csv:1437`.
- **Reviewer in control.** Rules only *propose* adjustments. Nothing changes adjusted EBITDA until a reviewer accepts it.
- **Excel you can audit.** The databook and exported models use live formulas that match the Python results to the centavo.
- **Air-gap ready.** Everything except the optional Claude backend runs offline; the chat can use a local model instead.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,agent]"

apex demo --out demo_output        # builds a sample company and runs everything
apex verify demo_output/run.json   # re-runs and proves the result is identical
pytest                             # 129 tests
```

Open `demo_output/databook.xlsx`. The sample company, Bayside Trading Corp.,
has seven planted problems (see `src/apex_diligence/synthetic.py`); the
databook should surface all of them.

## The analyst: models and chat

Run any model directly, no AI needed:

```bash
apex tool                                          # list the tools
apex tool --demo lbo_model --input '{"entry_multiple": 6.5, "exit_multiple": 7}'
apex tool --demo cash_flow_13_week --input '{"min_cash": 15000000}'
apex tool --demo export_model --input '{"model": "lbo", "inputs": {"entry_multiple": 6.5}}'
```

Or chat with it:

```bash
export ANTHROPIC_API_KEY=...                       # Claude (default model claude-sonnet-5-5)
apex chat --demo
apex chat --demo --ask "What IRR do we get at 6.5x in and 7x out with 3.5x senior debt?"

apex chat --demo --backend local --model qwen3:14b # any OpenAI-compatible local server (Ollama, vLLM)
apex replay analyst_output/chat-log.jsonl          # re-runs every tool call and proves the numbers
```

| Tool | What it does |
| --- | --- |
| `get_financials` | P&L for LTM, a fiscal or calendar year, quarter or month; EBITDA bridge by rule; pending review |
| `monthly_series` | Any P&L line or balance month by month |
| `list_adjustments`, `explain_adjustment` | Adjustment candidates and their full journal evidence |
| `proof_of_cash`, `run_checks` | Book vs bank cash by month; data checks |
| `nwc_analysis` | Monthly NWC, DSO / DIO / DPO, cash conversion, NWC peg with exclusions |
| `customer_analysis` | Concentration (top 1/5/10, HHI), growth vs prior year, new and lost customers |
| `credit_metrics` | Gross and net leverage, interest cover, DSCR, covenant headroom, for current or proposed debt |
| `purchase_price_bridge` | EV to equity: debt, cash, debt-like and cash-like items, NWC vs peg |
| `cash_flow_13_week` | Direct-method weekly receipts and payments; payment patterns detected from the ledger; minimum-cash flags; back-test against the bank |
| `lbo_model` | Sources and uses, senior (amortisation + sweep) and subordinated debt, credit stats, IRR, MOIC, value-creation bridge |
| `dcf_valuation` | Unlevered DCF, Gordon or exit-multiple terminal value, mid-year option, implied multiple |
| `sensitivity` | Two-way grid on any two LBO or DCF inputs |
| `calculator` | Exact arithmetic on figures from other tools |
| `export_model` | LBO or 13-week cash flow as an Excel workbook with live formulas |

Every model returns the assumptions it used and where each came from
(`given`, `default`, or `derived` from the company's own figures).

## Running on real files

```bash
apex run --gl gl.csv --bank bank.csv --mapping mapping.csv \
         --rules rules.toml --company "Target Co." --out out/
apex chat --gl gl.csv --bank bank.csv --mapping mapping.csv --rules rules.toml --company "Target Co."
```

Then open `out/review.csv`, fill the `decision` column (`accept` / `reject`,
optional `amount_override`), and re-run with `--decisions out/review.csv`
(write the next run to a new `--out` folder).

| File | Required columns (aliases accepted) |
| --- | --- |
| General ledger | `date`, `journal_id`, `account_code`, `debit`, `credit`; optional `account_name`, `description`, `counterparty`, `entry_type` (`auto` / `manual` / `opening`) |
| Bank statement | `date` and either `amount`, or `deposit` + `withdrawal`; optional `description`, `balance` |
| Mapping | `account_code`, `category` (see `taxonomy.py`; map loans to `liability_debt`) |
| Rules (optional) | TOML; see `data/rules.toml` written by `apex generate` |

Dates must be `YYYY-MM-DD` (or real Excel dates). Ambiguous formats such as
`03/04/2025` are refused rather than guessed.

## Layout

```
src/apex_diligence/
  money.py          integer-cents parsing and rounding
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
  databook.py       Excel databook with live formulas
  outputs.py        writing files, manifest, verify
  workspace.py      loaded data + run, base figures, assumption log
  projection.py     operating projection shared by LBO and DCF
  lbo.py, dcf.py    deal models
  cashflow.py       13-week cash flow
  analysis.py       NWC peg, customers, credit metrics, price bridge
  schema.py         tool input validation and JSON output
  tools.py          the analyst tools
  model_export.py   LBO and cash flow workbooks with live formulas
  agent.py          chat agent, Claude and local backends, chat log, replay
  synthetic.py      sample company with planted problems
  cli.py            the `apex` command
tests/
```

Exit codes: `0` done, `1` unreadable input, `2` blocked by checks, `3` verification failed.
