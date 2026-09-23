# Captain Hindsight: architecture (as built)

Captain Hindsight is the Chief Sceptic. It reads what the other agents have
already produced, decides deterministically whether anything is worth a model
call, and when it is, asks one question: are we wrong, and are we defending a
decision instead of evaluating it? It never edits another agent's files.

This document started as the M0 recon note and was updated as the build
landed. Section 1 is what was actually in the repo, section 2 is how
Hindsight plugs into it, section 3 lists every assumption, and section 4 is
what still has to be connected.

## 1. What exists (verified by reading the code, 2026-09-23)

| Agent | Where | What Hindsight reads | Notes |
|---|---|---|---|
| Bob the Bot | `agent.py`, `bob_emit.py` | `docs/data/bob.json` (`high_impact` / `material` / `fyi`, each item `{ticker, title, url, type?, analysis?}`) | Buckets match the brief. Bob also exposes `bob_emit.py`, which already turns `bob.json` into Master Engine `InvestorEvent`s. |
| Sunday Sally | `sunday-sally/src/main.py` | `docs/data/sally.json` (`last_run`, `flagged[]` with `alert_tier`, `sally_verdict`, `valuation_percentile`, `distance_to_high_pct`, PEs) and its git history | **Sally never emits SELL.** Her verdicts are `Trim candidate` (Tier 3), `Hold but stop adding` (Tier 2), `Watch only` (Tier 1). The adapter maps these (section 2.3). |
| Wally the Watcher | `wally/` | `docs/data/wally.json` (`watchlists.<name>.flagged[]` with `below_target`, `distance_to_low_pct`, `distance_to_target_pct`, `target_price`) | `outputs/YYYY-MM-DD/` is gitignored, so it is only present on the runner that produced it. `docs/data/wally.json` is the durable copy. |
| JM Watch List | `watchlists/jm_watchlist.yaml` | ticker list (`.AX` suffixed) | Carries **no** native target prices; Wally attaches them from the global buy-price map. Hindsight takes the target from Wally's JSON row. |
| Theo | `theo/`, `theses/*.md` | `theo.thesis.load_map()` for pillars, kill conditions, reviews, amendments (`direction: LOOSENED/TIGHTENED/NEUTRAL`); `git log` on `theses/` for version history | Theo already has `theo/signals.py`, which turns Sally flags into open questions answered by a dated review with `trigger: SALLY_*`. Hindsight reuses that rule for "unanswered Sally flags". Theo has **no** inbox and **no** SQLite. |
| Decision log | `data/decisions.json` (money-free export), or the private `JM_Decision_Level_IRR.xlsx` | `theo.ledger.load()` | The public export has per-decision date, price, capital weight and normalised flows. No dollar amounts. It is enough to compute average cost, tranches, averaging down and weights. |
| Ned | `ned/` | not read | Optional context only; not wired in this build. |
| Master Engine | `master_engine/` | `InvestorEvent` schema | **There is no `run_master_investor.py` and no Master Engine workflow in the repo**, and nothing writes `master_investor_events.json`. Only the library exists. Hindsight therefore reads Bob's JSON directly and emits its own summary events in `InvestorEvent` shape for when a runner exists. |
| LLM plumbing | `agent.py::_call_anthropic` | pattern only | Anthropic SDK, `CLAUDE_MODEL` env, default `claude-sonnet-4-6`, streaming above 8192 max tokens. Hindsight mirrors this in `hindsight/llm.py` instead of importing `agent.py` (same reason Slinger gave: `import agent` drags in weasyprint, pypdf and Playwright). |
| Email | `email_sender.py::send_summary_email` | reused as-is | Plain text, Gmail SMTP env vars. |

Things the brief expected that are not there: `run_master_investor.py`, a
Master Engine schedule, an event file from Master Engine, a Theo inbox,
`JM-Writing-Style.md`, and target prices inside `jm_watchlist.yaml`.

## 2. How Hindsight plugs in

```
docs/data/{sally,bob,wally}.json ─┐
theses/*.md + git log ────────────┤  adapters.py  ──►  HindsightEvent  ──► dedup (seen table)
data/decisions.json / xlsx ───────┤                                        │
watchlists/jm_watchlist.yaml ─────┘                                        ▼
                                                     gating.py (deterministic, no LLM)
                                                                           │ gate fired
                                                                           ▼
                        behaviour.py + thesis_change.py (deterministic facts, evidence refs)
                                                                           │
                                                                           ▼
                        llm.py (call cap, 2 tiers, JSON + pydantic, retry once)
                                                                           │
                                                                           ▼
                        validators.py (FACT refs, bias refs, predictions, Lollapalooza gate)
                                                                           │
                                                                           ▼
                        render.py ──► one email, markdown reports, case files, SQLite, emit
```

### 2.1 Adapters (`hindsight/adapters.py`)
All read existing outputs; none changes an upstream agent.

- `SallyAdapter`: `docs/data/sally.json`, plus `git log` of that file to count consecutive weekly flags.
- `BobAdapter`: `docs/data/bob.json`, and `outputs/<today>/master_investor_events.json` if a future Master Engine runner writes one.
- `WallyAdapter`: `docs/data/wally.json`, scores every flagged row, keeps top N.
- `TheoAdapter`: `theo.thesis.load_map()` plus `git log --follow theses/<T>.md`.
- `PortfolioAdapter`: holdings from `tickers.yaml`; positions from the private decision workbook if present, else `data/decisions.json`, else `<store>/positions.yaml`.
- `WatchlistAdapter`: `watchlists/jm_watchlist.yaml`, with target prices joined from Wally's JSON.

No emit hook was added to Bob or Sally: their JSON outputs were enough.

### 2.2 Storage (`hindsight/store.py`)
`HINDSIGHT_STORE=local` (default) writes to `hindsight_data/`, which is in
`.gitignore`. `HINDSIGHT_STORE=private_repo` writes to a checkout of a private
repo at `HINDSIGHT_PRIVATE_REPO_DIR`. The store refuses to start if the target
directory would be tracked by the public repo, or if `private_repo` is chosen
and the checkout is missing. There is no fallback between the two.

SQLite (`hindsight.db`) holds one table per record type from the brief. Each
row is `(id, ticker, created_at, status, data_json)`, with the pydantic model
as the schema of `data_json`. Theo has no SQLite conventions to reuse, so this
is the simplest thing that keeps records typed and queryable.

### 2.3 Gating (`hindsight/gating.py`, thresholds in `config/hindsight.yaml`)
- Sally: `Trim candidate` → REDUCE. `Hold but stop adding` → REDUCE only when valuation percentile ≥ `sally.reduce_percentile` (0.95) **and** distance to the 52-week high ≤ `sally.reduce_max_distance_pct` (3%). Otherwise HOLD, and HOLD never triggers. Holdings only.
- Bob: HIGH IMPACT → HIGH, upgraded to CRITICAL by the regex lists in config. MATERIAL → MEDIUM, upgraded to HIGH for CEO / CFO / Chair changes and board spills. FYI → LOW, never triggers.
- JM triage gates: daily move, move since last review, new Bob event, Wally target crossing, a Theo pillar newly strained/breached (a standing STRAINED status is not news) or a new thesis version, valuation config hash change.
- Wally: `opportunity_score` from below-target discount, distance to 52-week low, and whether `valuations/<t>_ax.yaml` exists. Top N above a minimum.

### 2.4 Cost and failure handling
One `LLMClient` per run with a hard call cap (`HINDSIGHT_MAX_LLM_CALLS`,
default 10). Events that do not fit are stored as `QUEUED` and run first next
time; the email says so. Each analysis ends in exactly one of `OK`,
`SKIPPED_CAP`, `FAILED_API` or `FAILED_INVALID`, and the email renders the
three non-OK states differently. There are no placeholders.

## 3. Assumptions made

1. **Sally mapping.** Sally does not say SELL. "Hold but stop adding" at the top of the valuation range and near the high is treated as REDUCE. Thresholds are in config.
2. **Manual sell review.** `python -m hindsight sell NHC` builds a `SELL_SIGNAL` event from Sally's latest row for that ticker, with source `MANUAL`. It runs the full sell analysis whatever the gate says, because a human asked.
3. **Model defaults follow the repo.** Full tier defaults to `CLAUDE_MODEL`, else `claude-sonnet-4-6`. Triage tier defaults to `claude-haiku-4-5`. Both can be overridden by env.
4. **Cost estimate** uses a per-model price table in `config/hindsight.yaml`. It is an estimate to tune, not a bill.
5. **Retry.** A validation retry is allowed even if it pushes the run one call past the cap, so a half-finished analysis never becomes a "skipped".
6. **Evidence refs** are IDs Hindsight builds before the call (`decision:NHC#1`, `thesis:NHC`, `thesis_history:NHC`, `sally:2026-09-20:NHC`, `price:NHC`, `report:<id>`, `response:<id>`, `valuation:NHC`). A bias state counts as evidenced only if it cites one of those exact IDs.
7. **Original sell target** comes from `positions.yaml` if set, else the first `sell above $X` in the thesis file. NHC's is $9.00 from its source note.
8. **Position weight** is share of capital committed (from the money-free export), plus a current-value weight as at the ledger date. No dollar values leave the private store.
9. **Price history** uses yfinance directly, as Wally and Sally do. Wally's `fetch_price_snapshot` returns only a 1-year snapshot, so it was not enough for "since purchase".
10. **Thesis loosening** is taken from Theo's own `direction: LOOSENED` labels, plus two structural checks: a pillar removed, or a kill condition rewritten while its pillar was STRAINED/BREACHED. Detecting "looser" from free text alone was judged too unreliable to automate.
11. **Git history depth.** Thesis version history needs `fetch-depth: 0` in the workflow. A shallow clone sees one version and reports it as such.
12. **Bias profile seed.** The initial "places to look" list lives in `config/hindsight.yaml` as generic tendency names only. The profile with hit rates, flags and evidence is created in the store on first run and never leaves it.
13. **Portfolio review timing.** First run of each month, and once in each of 20-31 January and 20-31 July.
14. **Munger definitions** are written from general knowledge in plain words. The Stripe Press PDF was not downloaded or quoted.
15. **Master Engine emit.** Hindsight writes `hindsight_events.json` (InvestorEvent shape, headline only, no position data) to the store and exposes `hindsight.emit.collect_events()` for a future runner. `master_engine/aggregator.py` was not changed.

## 4. What remains to connect

- **The private repo.** Create `JohnnyM77/Reporting-Agent-private` and add a fine-grained token with contents read/write on it as the `HINDSIGHT_PRIVATE_REPO_TOKEN` secret. The workflow defaults to `private_repo`, so until the token exists the scheduled run fails at the checkout step. That is deliberate. To run without it, set the repo variable `HINDSIGHT_STORE=local`: runs still email, but nothing persists between runs, so dedup, queueing, triage baselines and autopsies start from scratch each time.
- **Schedule.** `workflow_run` and schedules only fire from the default branch, so nothing runs until this is merged to `main`.
- **Master Engine runner.** When one exists, pass `hindsight.emit.collect_events` into `aggregate()` (it needs a fourth collector argument).
- **Private decision workbook.** Drop `JM_Decision_Level_IRR.xlsx` into `<store>/data/` or point `HINDSIGHT_DECISION_LOG` at it. Without it, the public money-free export is used, which is enough for everything except dollar tax figures.
- **Tax.** The tax note is qualitative. There is no cost base in dollars in the public data, so CGT dollars are not computed.
- **Ned.** Not wired.
- **Theo questions.** Theo has no inbox. Questions go in the report under "Questions for Theo" and in the case file.

## 5. Privacy boundary

Public repo: code, prompts, framework YAML, config, docs, tests (fixtures only).
Private store: case files, SQLite, reports, bias profile, responses, positions,
run logs. Workflow logs print counts and statuses only, never report text,
because Actions logs on a public repo are public.

This is a checklist for asking better questions, not a psychological
diagnosis.
