# Shared infrastructure: map and rules

Agents own intelligence. Shared infrastructure owns plumbing.

This file maps the repo as it stood before the 2026-09 infrastructure
cleanup, what that cleanup changed, and the rules for extending it. Read it
before you add an agent or touch anything under `shared/` or `dashboard/`.

## 1. Workflows → entry points

| Workflow | Runner | Entry point | Writes |
|---|---|---|---|
| `daily.yml` (Bob) | ubuntu | `python agent.py` | `docs/data/bob.json`, `state_seen.json` |
| `us_daily.yml` (Bob USA) | ubuntu | `python us_agent.py` | `docs/data/us.json` |
| `sgx_daily.yml` (Slinger) | self-hosted Windows | `sgx_agent.py` | `docs/data/slinger.json`, `state_seen_sgx.json` |
| `wally_watchlists.yml` | ubuntu | `python -m wally.main` | `docs/data/wally.json`, `outputs/` |
| `sunday_sally_weekly_review.yml` | ubuntu | `python -m src.main` (cwd `sunday-sally/`) | `docs/data/sally.json` |
| `ned_news.yml` / `ned_podcast.yml` | ubuntu | `python -m ned.main` | `docs/data/ned.json`, `transcripts.json` |
| `ned_transcript.yml` | self-hosted Windows | `python -m ned.main` | `docs/data/transcripts.json` |
| `results_pack_agent.yml` | ubuntu | `python -m results_pack_agent.main` | `outputs/` |
| `captain_hindsight.yml` (Harry) | ubuntu | `python -m hindsight` | `docs/data/harry.json`, private store |
| `theo-season.yml` | ubuntu | `python -m theo.cli season` (emails via `email_sender`) | theses |
| `theo-pages.yml` ("Publish site") | ubuntu | `theo.cli` + `scripts/build_dashboard.py` | GitHub Pages |

Every workflow that writes `docs/data/*.json` then runs
`python scripts/build_dashboard.py` and commits with `[skip ci]`.

## 2. Agent → infrastructure (after the cleanup)

| Agent | Email | LLM | Source retrieval | Drive |
|---|---|---|---|---|
| Bob (`agent.py`) | `shared.email_service` | `shared.llm` (+ `shared.pdf_llm`) | `asx_fetch.py`, `playwright_fetch.py`, `shared.asx` | `shared.gdrive` (OAuth preferred) |
| Bob USA (`us_agent.py`) | `shared.email_service` | `shared.llm` | `us_fetch.py` / `us_docs.py` (SEC) | none |
| Slinger (`sgx_agent.py`) | `shared.email_service` | `shared.llm` | `sgx_fetch.py` / `sgx_pdf.py` | none (by design) |
| Wally (`wally/`) | `shared.email_service` | `shared.llm` | yfinance, `asx_fetch.py` via `wally/asx_news.py` | `shared.gdrive` (OAuth only) |
| Sally (`sunday-sally/src/`) | `shared.email_service` | `shared.llm` | own ASX JSON API call, yfinance | charts via `wally/drive_upload.py` (OAuth); run-folder uploader (service account) exists but is not called |
| Ned (`ned/`) | `shared.email_service` | `shared.llm` | RSS, YouTube, Yahoo | none |
| Results Pack | none | `shared.llm` (+ `shared.pdf_llm`) | `shared/asx_simple_fetcher.py`, `shared.asx` | `shared.gdrive` (service account) |
| Harry (`hindsight/`) | `email_sender.py` → `shared.email_service` | `hindsight/llm.py` (gating, cap, validation) → `shared.llm` transport | reads other agents' JSON via `hindsight/adapters.py` | none |
| Theo (`theo/`) | `email_sender.py` (season workflow) | none | none | none |
| Master Engine (`master_engine/`) | `master_engine/notifier.py` → `shared.email_service` | none | `*/emit.py` adapters | none |

## 3. The shared layer

| Module | Owns | Does NOT own |
|---|---|---|
| `shared/email_service.py` | SMTP config from env, MIME building (plain, HTML, attachments, inline CID images, attachment size cap), SMTP_SSL send | subjects, bodies, templates, which files to attach |
| `shared/llm.py` | Anthropic client creation, API-key lookup, streaming above 8192 tokens, text/stop-reason/usage extraction, error description, retry helper | prompts, models, call caps, JSON parsing, outcome sentinels |
| `shared/pdf_llm.py` | native PDF document blocks, whole-request size/page limits, pypdf fallback, `LLM_SKIPPED`/`LLM_FAILED` | prompts |
| `shared/securities.py` | canonical `Security` (code + exchange), `.AX`/bare/`ASX:`/`LSE:`/`RR.` normalisation, Yahoo symbols | watchlist contents, which exchange a list belongs to |
| `shared/gdrive.py` | OAuth and service-account credentials from env, Drive service build, find/create folder, find file | folder layout, filenames, what to upload and when |
| `shared/asx.py` | ASX endpoint URLs, the browser-like session (headers pinned per caller), relative-link resolution, idsId -> PDF URL | which announcements matter |
| `shared/asx_simple_fetcher.py` | Results Pack's plain-HTTP 6-month announcements fetch | result-pack detection |
| `asx_fetch.py` (repo root) | Bob/Wally recent announcements (HTML + JSON + Playwright fallback) | classification |
| `shared/events.py` | `InvestorEvent`, the canonical internal event shape | event classification (each agent's emitter) |
| `dashboard/` | HTML rendering for each agent's card | reading/writing `docs/data/*.json` (that stays in `scripts/build_dashboard.py`) |

Each agent keeps its own failure policy on top of the shared transport:
Bob and Ned raise when email can't be sent (so `bob.json` is never written
for an unsent digest); Slinger, Bob USA, Sally and Harry log and return
`False`; Wally returns `False` for missing settings but raises on an SMTP
error.
The shared service exposes both behaviours; it never picks one for you.

The canonical internal event shape is `shared.events.InvestorEvent`
(moved from `master_engine/schemas.py`); Harry writes it on every run.
Master Engine is scaffolding: no workflow runs it. See
`master_engine/README.md`.

## 4. What stays duplicated, on purpose

- **Three ASX fetchers** (`asx_fetch.py`, `shared/asx_simple_fetcher.py`,
  Sally's `document_fetcher.py`). They hit different endpoints for different
  jobs; only the plumbing under them is shared.
- **Retry loops in Bob and Results Pack** use `shared.llm.call_with_retry`,
  but the log wording and the sentinel they return stay each agent's own.
- **Wally's and Sally's `VERDICT: / BULL CASE: ...` section parsers.** The
  code is the same, but each parses its own prompt's output format; merging
  them would couple two prompts.
- **Drive auth policies.** Four agents, four different correct answers (see
  `shared/gdrive.py`'s header).
- **`hindsight/cli.py`'s `.replace(".AX", "")`** and Master Engine's link
  helpers (`linker._is_asx`, `_yahoo_ticker`) keep their own ticker rules:
  Harry is protected, and the linker's rules differ deliberately (see its
  docstring).
- **`scripts/export_ledger.py`'s `_ticker`** duplicates
  `shared.securities.ticker_from_symbol`. It's a standalone script run by
  hand against a private workbook, and it imports nothing from the repo.

## 5. Rules

1. **An agent may import from `shared/`; `shared/` never imports an agent.**
2. **Import heavy optional deps lazily.** `shared.llm` imports `anthropic`
   inside functions and `shared.gdrive` imports the Google libraries inside
   functions, so an agent that doesn't use them doesn't need them installed.
3. **Don't import `agent.py` from another agent.** It pulls in weasyprint,
   Playwright, pypdf and bs4. That's why Slinger and Bob USA duplicated the
   streaming plumbing before `shared/llm.py` existed.
4. **Keep behaviour at the edge.** A shared function takes parameters
   (`raise_on_error`, `max_total_bytes`, `maintype`) rather than guessing
   which agent is calling it.
5. **Windows runner.** Anything run by `sgx_daily.yml` or
   `ned_transcript.yml` must read and write text as UTF-8 explicitly and
   keep PowerShell workflow steps ASCII-only (see `CLAUDE.md`).

## 6. Adding a new agent

1. Put its code in its own module or package. Import `shared.email_service`,
   `shared.llm` and `shared.securities` for the plumbing.
2. Write `docs/data/<agent>.json`.
3. Add `dashboard/sections/<agent>.py` exporting one `_<agent>_section(data)`
   function, and wire it into `scripts/build_dashboard.py` (one `_load` and
   one line in the `sections` list in `build_dashboard()`).
4. Add the workflow name to the `workflow_run` triggers in `theo-pages.yml`.
