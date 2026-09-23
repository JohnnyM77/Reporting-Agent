# Captain Hindsight

Everything is 20:20 in hindsight. Captain Hindsight's job is to give Johnny
that clarity before the loss, not after it.

It is the Chief Sceptic that sits above the other agents. It does not fetch
news, screen stocks or summarise announcements. It reads what Bob, Sally, Wally
and Theo already produced, decides in code whether anything could mean we are
wrong, and only then asks the model three questions:

1. What would have to be true for the current conclusion to be wrong, and is
   there evidence those things are already happening?
2. Would Johnny make the same decision today if he did not already own it?
3. Are we evaluating the business, or defending a decision we already made?

If nothing material has changed it says so: GREEN, "NO MATERIAL CONTRADICTION
FOUND." A sceptic, not a perma-bear.

## What triggers it

| Source | Gate (all in `config/hindsight.yaml`) | Report |
|---|---|---|
| Sally | `Trim candidate` on a holding, or `Hold but stop adding` at ≥95th valuation percentile and ≤3% from the 52-week high | Sell alert |
| Bob | HIGH IMPACT (CRITICAL on raise >10% dilution or >15% discount, guidance cut, CEO/CFO exit, halt, going concern, auditor issue, covenant stress, related party). MATERIAL only for CEO/CFO/Chair changes and board spills. FYI never. | Event review |
| JM Watch List | ±7% day, ±15% since the last review, new Bob event, Wally target crossed, a Theo pillar newly strained, new thesis version, valuation config changed | Triage line, maybe a full review |
| Wally | top 3 by `opportunity_score` above 40 | Opportunity review |
| Calendar | first run of each month, and late January / late July before reporting season | Portfolio review |
| Its own warnings | check date passed, or next results arrived | Autopsy |

No gate, no model call. A JM Watch List name with nothing new costs nothing and
shows as `NO CHANGE`.

## What a report contains

Every full report carries: the report-type headings, the strongest opposing
case, a thesis test (the model's read plus the deterministic git-history red
flags), 7 Powers with both halves of Helmer's test, a Munger scan, the
Lollapalooza check, a verdict with a written severity reason, and falsifiable
predictions for the autopsy. Claim types render as `[Fact]`, `[Interp]`,
`[Mgmt]`, `[Agent]` and `[Hindsight]`.

Rules enforced in code, whatever the model says:

- A FACT without a source ref becomes an INTERPRETATION.
- A bias state of OBSERVED EVIDENCE or CURRENTLY TRIGGERED needs a ref to a
  real record (a decision, a thesis version, a price, a Sally flag, a prior
  report, a response). Without one it becomes UNKNOWN.
- A LOLLAPALOOZA needs three evidenced tendencies, three pushing the same way,
  and a hard behavioural trigger (down 20%+, averaged down in 12 months, thesis
  revised under pressure, price above the original sell target, or 2+
  unanswered Sally flags). Otherwise it is downgraded to RED and logged.
- A warning without a falsifiable prediction is thrown away.

The Munger scan is a checklist for asking better questions, not a
psychological diagnosis.

## Failure is visible

Each analysis ends in exactly one of:

| Status | Meaning | In the email |
|---|---|---|
| `OK` | parsed and validated | the report |
| `SKIPPED_CAP` | `HINDSIGHT_MAX_LLM_CALLS` reached | "SKIPPED: run call cap reached, queued for the next run" |
| `FAILED_API` | the API raised | "ANALYSIS FAILED: API error" + the real error |
| `FAILED_INVALID` | two replies, neither valid | "ANALYSIS FAILED: invalid model output" + the validation error; raw text kept in the store |

There are no placeholder analyses. Deterministic facts are still shown under a
failure, labelled as facts, never as an analysis.

## Running it

```bash
pip install pyyaml pydantic anthropic yfinance pandas openpyxl

# Full sell review of one holding, from Sally's latest output
python -m hindsight sell NHC

# Same, but build the prompt and facts without calling the model or emailing
python -m hindsight sell NHC --dry-run

# The daily run: triage, Sally/Bob/Wally events, autopsies, portfolio review if due
python -m hindsight run

python -m hindsight run --mode portfolio
python -m hindsight run --mode autopsy
python -m hindsight run --mode scorecard
```

`--force` re-runs an event already reviewed. `--no-email` skips the email.

### Recording what you did

```bash
python -m hindsight respond NHC --action held --note "Waiting for FY26 cash conversion"
```

Actions: `held`, `sold`, `trimmed`, `added`, `ignored`. Hindsight also infers
`added` (a new buy in the decision log) and `sold` (gone from `tickers.yaml`)
for names it has reported on. Responses feed the next report's context and the
autopsy.

## Where data lives

The repo is public. Personal data never goes into a tracked path.

| Setting | Where | Use |
|---|---|---|
| `HINDSIGHT_STORE=local` (default locally) | `hindsight_data/` (gitignored), or `HINDSIGHT_DATA_DIR` | dev, tests |
| `HINDSIGHT_STORE=private_repo` (default in CI) | checkout at `HINDSIGHT_PRIVATE_REPO_DIR` | production |

The store holds `hindsight.db` (SQLite), `case_files/<TICKER>.md`,
`reports/<date>/`, `profile/jm_bias_profile.yaml`, `runs/` (email copies and
cost logs) and `emit/` (Master Engine-shaped summary events). Both backends
refuse to start if the directory would be tracked by the public repo, and
`private_repo` without a checkout fails with a clear message rather than
falling back.

The decision log is read in this order: `HINDSIGHT_DECISION_LOG`,
`<store>/data/JM_Decision_Level_IRR.xlsx`, the public money-free
`data/decisions.json`, then `<store>/positions.yaml`:

```yaml
positions:
  NHC:
    sell_target: 9.00          # overrides the "sell above $X" read from the thesis
    tranches:
      - {date: 2024-09-03, price: 4.21, weight: 0.07}
```

## Environment

| Var | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | | required for model calls |
| `HINDSIGHT_MODEL_FULL` | `CLAUDE_MODEL`, else `claude-sonnet-4-6` | full reports and autopsies |
| `HINDSIGHT_MODEL_TRIAGE` | `claude-haiku-4-5` | one-line triage |
| `HINDSIGHT_MAX_LLM_CALLS` | 10 | per run; the rest are queued |
| `HINDSIGHT_MAX_TOKENS_FULL` | 16000 | streamed at or above 8192 |
| `HINDSIGHT_STORE` | `local` | `local` or `private_repo` |
| `HINDSIGHT_DATA_DIR` | `hindsight_data/` | local store location |
| `HINDSIGHT_PRIVATE_REPO_DIR` | | private checkout path |
| `HINDSIGHT_DECISION_LOG` | | path to the private IRR workbook |
| `EMAIL_FROM`, `EMAIL_TO`, `EMAIL_APP_PASSWORD` | | same Gmail secrets as Bob |

## GitHub Actions

`.github/workflows/captain_hindsight.yml` runs daily at 01:30 UTC and on
manual dispatch (`mode`, `ticker`, `force`). Secrets: `ANTHROPIC_API_KEY`, the
email trio, and `HINDSIGHT_PRIVATE_REPO_TOKEN`. Repo variable
`HINDSIGHT_STORE` picks the backend (default `private_repo`).

To set up the private store once:

1. Create a private repo `JohnnyM77/Reporting-Agent-private` with any initial commit.
2. Create a fine-grained token with Contents read/write on that repo only, and
   add it as the `HINDSIGHT_PRIVATE_REPO_TOKEN` secret here.
3. Leave the `HINDSIGHT_STORE` variable unset (or `private_repo`).

To try it before that exists, set the variable to `local`. Runs then email but
keep nothing between runs, so dedup, queues, triage baselines and autopsies
start from scratch each time.

## Boundaries

- Reads Theo's theses, never writes them. Questions for Theo go in the report
  and the case file.
- Never changes Bob, Wally, Sally, Theo or Master Engine behaviour. Every
  existing agent runs exactly as before if Hindsight is disabled or broken.
- Personal data only in the private store.
- No silent placeholders.
