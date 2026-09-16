# Reporting-Agent — Claude Notes

## Agent behaviour — no polling loops

Do not subscribe to PR activity and do not schedule self check-ins
(`send_later`, routines, `/loop`) unless I explicitly ask. This repo has no CI
that runs on `pull_request`, so there is nothing for a watcher to catch — open
the PR, tell me, and stop. If you think something genuinely needs watching, ask
first and say what signal you expect.

Every scheduled wake-up replays the whole conversation to the model, so a
"nothing changed" check-in on a long session costs nearly as much as a useful
turn. Ten of them cost real money and caught nothing.

## Environment

### User's Windows PC (self-hosted GitHub Actions runner)
- **Windows username**: `mr_co`
- **Home directory**: `C:\Users\mr_co`
- **Python path**: `C:\Users\mr_co\AppData\Local\Programs\Python\Python314`
- **Actions runner**: installed at `C:\actions-runner`, service name `actions.runner.JohnnyM77-Reporting-Agent.mr_co-runer`
- **Runner name in GitHub**: `mr_co-runer` (note: typo in name, registered as-is)
- **Git Bash**: installed (required for `shell: bash` in workflow)

### GitHub repo
- Owner: `JohnnyM77`
- Repo: `Reporting-Agent` (public)

## Bob the Bot — results output architecture (V3)

Applies to the `RESULTS_HY_FY` path only (`deep_results_analysis` and its
callers in `agent.py`). Acquisitions, capital raises, trading updates and the
FYI/Material two-liners are unchanged; the same pattern is intended to extend
to the acquisition and capital-raise memos in a later pass.

### The email is a glance, the Doc is the analysis
A results announcement used to dump a long prose memo into the email. It now
renders a ~360px card: a five-row metric table, a summary of five sentences or
fewer, and two links. The long-form analysis moved to a native Google Doc.

**Why a native Doc, not a PDF or docx**: a Doc reflows on a phone. A PDF does
not. The Doc is created by uploading HTML with
`mimeType: application/vnd.google-apps.document`, which makes Drive convert it
on upload (`create_analysis_doc`). Markdown from the model is rendered to a
small HTML subset first (`_markdown_to_html`: headings, paragraphs, lists,
pipe tables) — enough for what `RESULTS_HYFY_PROMPT` emits, deliberately not a
general markdown engine.

**Drive auth is OAuth, not service account**: a service account has zero Drive
storage quota of its own. Writing into a plain My Drive folder — even one
that's been *shared with* the service account as Editor — returns HTTP 403
"Service Accounts do not have storage quota". This silently killed every Bob
upload for several weeks after the redesign landed: the try/except in `main()`
swallowed the 403, the digest looked fine, and nothing landed in Drive.

`drive_service()` now prefers OAuth2 user credentials (`GDRIVE_CLIENT_ID` +
`GDRIVE_CLIENT_SECRET` + `GDRIVE_REFRESH_TOKEN`) — same pattern Sunday Sally
uses. Authenticated as the human user, files are owned by *them* and use
*their* quota, so plain My Drive folders work. Falls back to service account
with a loud warning only when OAuth secrets aren't set.

Every Drive API call also passes `supportsAllDrives=True`. Without it the
Drive v3 API silently refuses to touch Shared Drive contents; passing it costs
nothing for My Drive writes and future-proofs the code.

**Raw PDF uploads were removed.** The ASX link in the email already points at
the same PDF ASX hosts, so copying it to Drive was pure duplication. Drive is
used only for the analysis Doc now — one file per results item, and that
file is worth opening.

**Drive failures are visible.** When `create_analysis_doc` raises, the results
card renders a red "⚠️ Drive save failed" warning row with the actual error
text, and the failure is stashed on the item's dashboard JSON as `doc_error`.
No more silent Drive.

### The five metrics are locked
Revenue, Underlying NPAT, Underlying EPS, Ordinary dividend, Operating cash
flow — in that order, defined once in `RESULTS_METRIC_ROWS`. Underlying, never
statutory, with the basis labelled. A figure that cannot be found in the source
renders `n/a`; the prompt forbids guessing, inferring or back-solving, because
a wrong number is worse than a missing one now that the number *is* the
product.

Currency comes from the report and is never converted. Non-AUD reporters get an
explicit prefix (`US$4,180m`) so a USD figure can't be read as Australian
dollars; AUD stays a bare `$` because the card's context line already says
"reported in A$". AUD conversion is a later pass.

### One structured LLM call per results item
`deep_results_analysis` makes exactly one Anthropic call returning strict JSON
(`ticker`, `period`, `period_type`, `currency`, `metrics`, `summary`,
`full_analysis`). This matters under `MAX_LLM_CALLS_PER_RUN` during reporting
season, when several portfolio names report on the same morning. The cap is now
env-driven (`MAX_LLM_CALLS`, default 25, set in `daily.yml`) rather than a
hardcoded 15.

**Output-token budget is per-caller.** The first live run of the redesign hit
`parse_error` for both BHP and CSL — `full_analysis` for a large reporter blew
past the shared `max_tokens=4096` and the JSON was truncated mid-string. The
results path now sends its own budget (`CLAUDE_RESULTS_MAX_TOKENS`, default
50000, plumbed through `llm_chat_with_pdfs` / `llm_chat`), while every other
path keeps the smaller `CLAUDE_MAX_TOKENS` default (4096) — a two-liner does
not need a 50k ceiling. When the API reports `stop_reason=max_tokens`,
`_call_anthropic` logs a warning and stashes it on `counters` so the
parse-error path names truncation as the cause instead of the generic
"not valid JSON" — the two need different fixes and shouldn't look identical.

**Long calls must stream, not `.create()`.** The Anthropic Python SDK refuses
non-streaming `messages.create` calls whose expected duration exceeds ~10
minutes — a client-side check to avoid HTTP read-timeouts on long
generations. At Sonnet's output rate, the 50k results budget hits that
threshold, so the first attempt after raising `max_tokens` came back with
"Streaming is required for operations that may take longer than 10 minutes".
`_call_anthropic` now routes any call with `max_tokens >= _STREAMING_MIN_TOKENS`
(8192) through `client.messages.stream`; below that threshold the short
`.create` path stays. Streaming keeps the connection alive with periodic
events, so the SDK-side ceiling doesn't apply.

`strawman_post` was left as the existing no-op shim — folding a Strawman draft
into the same JSON would save a call but Strawman output is not currently in
the digest, so there is nothing to save.

### Native PDF in, extracted text as fallback
The results path sends the report (and the deck, if present) as native
Anthropic `document` blocks via `shared/pdf_llm.py`, because table extraction
is exactly where `pypdf` degrades. `extract_pdf_text` remains the *fallback*,
not the default — used when a PDF is missing, malformed, or would push the
request past Claude's 32MB / 100-page whole-request limit. When that fallback
fires, the prompt is told the text came from pypdf so the model prefers `n/a`
over a misread figure.

### Three manual run modes (workflow_dispatch inputs)
`daily.yml` exposes three optional inputs, each mapping to an env var read in
`main()`. Precedence when more than one is set: **RESULTS_TICKER > MANUAL_TICKER
> FORCE_RERUN_TICKERS / normal**.

| Input / env | Window | What it surfaces |
|---|---|---|
| `force_rerun_tickers` | full ASX history | re-runs listed tickers ignoring seen_state; adds non-portfolio names to the portfolio run |
| `manual_ticker` | last 7 days | exclusive; every announcement as FYI plus any results |
| `results_ticker` | last ~6 months (`RESULTS_LOOKBACK_DAYS`, default 180) | exclusive; ONLY the latest HY/FY results, full deep analysis, no FYI noise |

`RESULTS_TICKER` is the "someone at the pub mentioned HPG — what were their last
numbers?" path: a company may have last reported a month or two ago, outside the
24h/7d windows. `most_recent_results_cluster` narrows the 6-month pull to the
single newest reporting event (release + presentation + Appendix 4D/4E within
~14 days of each other) so an old half-year is never bundled with a newer
full-year. No results in the window → a plain "no results found in the last N
days" note, never a blank card.

**One-off modes never touch shared state.** When `RESULTS_TICKER` or
`MANUAL_TICKER` is set, `main()` emails the analysis but skips both the
`bob.json` write and the seen_state save. Two reasons: a single ad-hoc ticker
must not clobber the portfolio dashboard, and — because the bob.json write
stamps `last_run = today` — a morning phone query would otherwise trip the
evening scheduled digest's catch-up guard (`_already_sent_today`) and stand the
real digest down. An ad-hoc query must never cost that night's portfolio digest.
This matches the log line manual mode always claimed ("one-off, not saved to
state") but the code previously contradicted.

### Results detection is pattern-based, not a phrase list
`looks_like_results_title` gates the entire results path: no match, no
`deep_results_analysis`, no card, no PDF. It used to be a list of literal
phrases (`"full year results"`, `"results presentation"`, …), which silently
lost Brambles' FY26 report on 2026-08-20 — BXB styles its headlines
"2026 Full-Year Result **presentation**" (singular "Result"), "Full Year
Statutory Accounts" and "2026 Full-Year ASX & Media Release". All five BXB
documents downloaded, none was recognised, and the digest showed BXB only
under MATERIAL/FYI while MVP the same morning analysed fine. Nothing in the
run looked like an error, which is what made it expensive to spot.

It is now a regex list: a reporting period (`half`/`full year`, `FY26`,
`1H FY2026`, `interim`, `annual`) beside a results-document noun (`result(s)`,
`report`, `accounts`, `release`, `presentation`), plus standalone hard yeses
(Appendix 4D/4E, `preliminary final report`, `statutory accounts`). Loosening
`results` to match the singular means AGM **voting** results now need an
explicit hard-no, alongside the existing transcript/webcast exclusions.
Real headlines from that morning are pinned as tests in
`tests/test_agent_gate_and_rerun.py` — both the ones that must match and the
governance statement / dividend notice / substantial-holding notice that
must not.

### JSON repair recovers content, it never invents it
`_parse_analysis_json` tries four things, cheapest first: a straight parse, the
outermost `{...}` span (ignoring prose either side), that span with string
bodies sanitised, and finally unclosed brackets closed.

The sanitising step exists because `full_analysis` is a multi-thousand-character
markdown blob inside one JSON string, and one slip in a long generation fails
the whole item — a raw newline instead of `\n`, or a markdown escape like `\%`
that JSON rejects. SPZ died this way on 2026-08-19 with the generic
"not valid JSON". Re-encoding those characters loses nothing the model wrote.

Bracket-closing is different and is deliberately fenced off: it only runs when
the API did **not** report `stop_reason=max_tokens`. Balancing brackets on a
truncated response yields an object whose `full_analysis` stops mid-sentence,
which would render as a clean card — the one outcome this design forbids.
Truncation stays a `parse_error` that names the cap. In practice a truncated
response ends inside an unterminated string, so there is nothing to close
anyway; the guard is there so a future change can't quietly turn truncation
into a plausible-looking result.

### Four distinct outcomes, none of which can look clean
`deep_results_analysis` tags its return with `_status`:

| `_status` | Cause | Email block shows |
|---|---|---|
| `ok` | JSON parsed | the normal card |
| `skipped` | `MAX_LLM_CALLS_PER_RUN` reached | `Analysis skipped: run-call cap reached` (not an error) |
| `failed` | Anthropic API exception | `ANALYSIS FAILED` badge + the real error class/status |
| `parse_error` | model replied, but not valid JSON | `ANALYSIS FAILED` badge; raw text preserved in the Doc |
| `no_content` | no usable PDF or text; no LLM call made | "open manually" |

The rule: a half-broken digest must never look like a clean one. A parse
failure never falls back to a plausible-looking placeholder — the raw model
output goes to the Doc verbatim under a "Raw model output (unparseable)"
heading so nothing is lost.

### Email HTML constraints (do not "modernise" this)
The results card is table-based with inline styles only, and there is a test
asserting no `display:flex`, `display:grid`, `<style>` or `class=` appears in
it. Email clients strip `<style>` blocks and mishandle flexbox/grid; a
two-column `<table>` (label `<td>` + value `<td>`) is the only reliable way to
get label-left / value-right rows. Change values are coloured with an inline
`style="color:…"` span — green up, red down, muted grey for `n/a` / `n/m`.

Digest blocks may now be either a plain string (escaped, pre-wrap) or a
`{"text": ..., "html": ...}` dict for blocks that build their own email-safe
HTML (`_block_text` / `_block_html` in `agent.py`).

## Singapore Slinger — Singapore Exchange announcements (V4)

The SGX bot is Singapore Slinger — "slings information" from SGX. It's a
distinct persona from ASX Bob (their JSON shapes overlap by design, but
the two are independent bots on the dashboard now, not "Bob with a
regional badge"). The display name changed; the underlying `sgx_*`
module and `BOB_SG_*` constant names stayed to avoid a churny rename
across every import.

Round 1 landed the fetch (`sgx_fetch.py`). Round 2 added the full daily
digest: classifier + PDF fetcher + LLM analysis (results only) + email,
all wired into `sgx_daily.yml` with a morning SGT cron. Round 3 wires
the dashboard: Slinger writes `docs/data/slinger.json` after each real
run, the sgx_daily workflow commits + pushes it, and
`.github/workflows/theo-pages.yml` picks up the `Singapore Slinger Daily`
`workflow_run` completion event and republishes the site.

Layout — five top-level `sgx_*` modules keep the SGX path a clean
horizontal split from `agent.py` (which stays ASX-only, untouched):

| File | Role |
|---|---|
| `sgx_fetch.py` | Playwright prime + REST replay (Round 1) |
| `sgx_classify.py` | Category-code -> bucket (RESULTS_HY_FY / DIVIDEND / SHARE_BUYBACK / ACQUISITION / …), title-regex fallback |
| `sgx_pdf.py` | `links.sgx.com` URL -> local PDF(s). Handles both direct-PDF and HTML-landing-page cases |
| `sgx_email.py` | Digest HTML/text builder — same table-only inline-styles shape as `agent.py`, `S$` prefix, "SGX" badge in header, full markdown analysis rendered inline in the results card |
| `sgx_agent.py` | Orchestrator: fetch → classify → PDF → LLM (for results) → email with PDFs attached → seen-state → dashboard JSON |

**No coupling to `agent.py`.** The ~40 lines of Anthropic-streaming
plumbing are duplicated in `sgx_agent.py` rather than imported. Reason:
`import agent` drags in `weasyprint`, `pypdf`, `bs4`, and other deps the
SGX box doesn't need for anything else, and any change to `agent.py`
becomes an SGX regression risk. Round 3+ can promote the shared bits
into `shared/` when a third caller appears.

**No Google Drive on the SGX path.** ASX Bob writes a native Google Doc
per results item; SGX Bob deliberately doesn't.

The email body carries the metric card, the short summary, and clickable
**links** to the source PDFs on SGX (rendered from the `source_url` on
each `FetchedPdf` — see `sgx_pdf.py`). The deep analysis (`full_analysis`
markdown) does NOT render in the email body; it goes into a standalone
**PDF attached** to the email so the user can forward one file to
friends. This split is intentional:

- Source PDFs → **linked** (not attached) so the email stays lightweight
  and Gmail doesn't warn about size; the originals live on SGX anyway.
- Analysis PDF → **attached** so it forwards cleanly as a self-contained
  file rather than an inline HTML block that reflows when re-emailed.

The analysis PDF is rendered by `_render_analysis_pdf` in `sgx_agent.py`
via Playwright + installed Chrome (`channel='chrome'`, headless,
`page.pdf()`). Chrome is already provisioned on the self-hosted Windows
runner for `sgx_fetch`'s token-prime step, so this adds no dependencies.
The HTML for that PDF is built by `build_analysis_pdf_html` in
`sgx_email.py` — a complete `<!doctype html>` document with an `@page A4`
rule, the same metric table as the email card, the summary, and the full
markdown analysis. Attachment total is capped at 20MB but the analysis
PDFs are ~50-200KB each, so the cap is really just a safety net.

**Metric JSON keys are load-bearing.** `sgx_email._results_card_html`
reads `dividend_ordinary` (not `ordinary_dividend`) and `change_pct`
(not `change`) — those are the keys the `RESULTS_HYFY_PROMPT` schema
tells the model to emit. Earlier code used the wrong names and the
first live SGX results email came back with a blank YoY column and
`n/a` in the dividend row despite the model returning real numbers.
`tests/test_sgx_email.py::TestResultsCard::test_card_shows_yoy_change_column`
and `test_card_renders_dividend_row` pin the correct schema.

**SGX classification is metadata-driven, not title-regex.** Every SGX
row carries `sub` (e.g. `ANNC17`), `cat` (e.g. `ANNC`), and
`category_name` (e.g. "Financial Statements and Related"). We map those
directly to Bob's buckets. Title regex is fallback only for the
`ANNC18` General Announcement bucket. This is much cleaner than ASX's
title regex (which lost BXB's FY26 release before being rewritten) — SGX
gives us structured signal for free.

**Seen-state file: `state_seen_sgx.json`.** Separate from ASX's
`state_seen.json` — different exchange, different retention window,
independent lifecycle. `sgx_daily.yml` restores/saves it via
`actions/cache`, same pattern `daily.yml` uses for ASX.

**Cron: `15 0 * * *` UTC = 08:15 SGT.** ~1h15m after SGX opens, long
enough for the ~7am pre-open batch to have landed.

Round 1 was fetch-only: `sgx_fetch.py` pulled the announcements list
for every ticker in `tickers.yaml`'s `sgx:` section and wrote JSON to
`outputs/sgx/announcements.json`. Round 2 keeps that path (via the
`fetch_only=true` workflow dispatch input) as a diagnostic mode.

### Why SGX must run on the self-hosted Windows runner
Two independent SGX-side filters, both discovered during the spike
sequence (#158 → #166):

1. **TLS fingerprint filter.** `api.sgx.com` returns 403 to raw
   `requests` calls (and to GitHub-hosted ubuntu runner IPs, even from
   headless Chromium). Only a browser session that matches installed
   Google Chrome gets through.
2. **Signed `authorizationtoken` header.** `investors.sgx.com` is a
   Flutter Web app that computes a ~130-char signed token client-side
   and injects it on each `api.sgx.com` request. Without it, the
   announcements list endpoint returns 401 even when everything else
   about the request is right. There is no way to reproduce this token
   from Python alone — it has to be captured from a live browser.

`sgx_fetch.py` handles both by launching Playwright with
`channel='chrome'` (installed Chrome, not bundled Chromium) and a real
Chrome User-Agent (Playwright otherwise stamps `HeadlessChrome`), then
navigating to the announcements page and intercepting the first
`api.sgx.com` request that carries a non-empty `authorizationtoken`.

**The token is not ticker-bound** (verified in #165), so one Playwright
prime does the whole portfolio — subsequent REST calls use
`context.request` with the captured token forwarded as a header.

### The URL shape that returns real data
Getting the query params right was itself a diagnostic round (#166 vs.
the earlier zero-item response). The endpoint Flutter actually fires:

```
GET /announcements/v1.1/securitycode
    ?value=D05&securityCodeParams=D05
    &pagestart=0&pagesize=25
    &periodstart=YYYYMMDD_HHMMSS&periodend=YYYYMMDD_HHMMSS
    &exactsearch=true
```

Both `value` and `securityCodeParams` must echo the ticker (not the
literal string `"securitycode"`). No `cat=` or `sub=` filters — those
narrow the result set and return zero for a full company view. Both
period bounds are compact SGT timestamps. All of that is encoded in
`_list_url` and pinned by `tests/test_sgx_fetch.py::test_matches_flutter_shape`.

### Row shape
Each returned announcement dict carries the ASX-compatible core keys
(`exchange`, `ticker`, `date`, `time`, `title`, `url`) so future
downstream code doesn't need to branch on exchange, plus the SGX-native
extras (`ref_id`, `id`, `sub`, `cat`, `category_name`, `issuer_name`,
`submission_ts_ms`) that the eventual SGX classifier will key off.

SGX classifies natively via `category_name` and `sub` codes (e.g.
`ANNC13`=Share Buy Back, `ANNC15`=Employee Stock Option, `ANNC17`=
Financial Statements, `ANNC18`=General Announcement) — much cleaner
signal than ASX's title regex when Round 2 lands.

### Workflow
`.github/workflows/sgx_daily.yml` (workflow name: **Singapore Slinger
Daily**) — manual dispatch AND daily cron (`15 0 * * *` UTC). Runs on
`[self-hosted, Windows]` using the PowerShell + machine-Python pattern
from `ned_transcript.yml`. Reads `state_seen_sgx.json` via
`actions/cache` for dedup across runs, runs `sgx_agent.py`, emails the
digest via the same Gmail SMTP secrets ASX Bob uses, then commits +
pushes `docs/data/slinger.json` and a rebuilt `docs/index.html` so the
site republishes. Dispatch inputs: `tickers` (override the yaml
portfolio), `hours_back` (default 24), `dry_run` (preview only, no
email, no state change, no dashboard write), `fetch_only` (Round 1
mode — just dump JSON, no LLM/email), `results_ticker` (pull LAST HY/FY
report + deep analysis), and `results_hint` (free-form context appended
to every LLM call — use it when the report doesn't make the shape of
the business obvious, e.g. "Haw Par's main asset is its UOB stake, not
Tiger Balm trading").

### Dashboard integration
Slinger writes `docs/data/slinger.json` (same shape as `bob.json`:
`last_run` + `high_impact` / `material` / `fyi` arrays) after each
non-dry-run. Written on both portfolio and results-ticker modes — a
one-off `results_ticker` query does show up on the site until the next
scheduled portfolio run overwrites it. This is a deliberate departure
from ASX Bob's "one-off never touches the dashboard" rule; Bob has a
catch-up cron the dashboard write would trip (see the `_already_sent_today`
guard in `agent.py`), Slinger doesn't.

`scripts/build_dashboard.py` renders `_slinger_section` between Bob's
card and Wally's. High-impact items reuse Bob's `_render_analysis_sections`
because the metrics JSON schema (`dividend_ordinary` + `change_pct`) is
identical, and Slinger's card adds a **Source PDFs** link list (SGX
hosts them, so the dashboard just links back). Material/FYI items only
carry `ticker` + `title` + `url`, so they render as compact rows.

**Last-two-runs history.** `_update_slinger_history` keeps
`docs/data/slinger_history.json` — the two newest distinct runs,
newest first — mirroring Bob's `bob_history.json`. `_slinger_section`
renders the current run open and the previous one collapsed in a
`<details>` block. The sgx_daily workflow's dashboard-commit step
`git add`s `slinger_history.json` alongside `slinger.json` and the
rebuilt `index.html`. Deduping is signature-based so a rebuild
triggered by another agent (Theo/Bob/Ned) with no new Slinger data
doesn't churn the file.

**PowerShell + Unicode gotcha (do not "modernise" the commit
message).** The Slinger dashboard-commit step runs in PowerShell on
the self-hosted Windows runner, which reads workflow scripts as
cp1252. A bare em dash `—` in a commit message like `"Dashboard update
— Slinger [skip ci]"` breaks the parser mid-file with `The string is
missing the terminator: "`, kills the commit + push, and the site
never picks up Slinger's data even though the run's other steps
succeeded (email sent, `slinger.json` written to disk, then thrown
away when the runner cleans up). Keep the step's git commit messages
ASCII-only — the fix that got LCC's second run to publish uses `--`,
not em dash.

The Publish site workflow (`theo-pages.yml`) has `Singapore Slinger
Daily` in its `workflow_run` triggers so a Slinger run completing kicks
off a Pages redeploy — same pattern Bob/Ned/Wally/Sally use. Note the
Pages workflow fires on completion regardless of the triggering run's
conclusion (deliberate — a run that emailed then tripped on a later
step has still committed data worth publishing), so a red Slinger
whose commit step failed will still trigger a redeploy — it just
deploys the pre-Slinger state.

## Wally the Watcher — target ("buy") prices

Wally flags a ticker on two independent triggers now, not one:
`flagged = near_low or below_target`. `near_low` is the original within-5%-of-
52-week-low screen; `below_target` fires when the current price is at or below a
per-ticker target price. Below-target tickers get exactly the same downstream
treatment as near-low ones (range + value charts, email detail, dashboard row),
because everything keys off `row.flagged` — the only change in `wally/main.py`
is passing `target_price=wl.target_prices.get(ticker)` into `screen_snapshot`.

Target prices live in the watchlist YAML, loaded by `wally/watchlist_loader.py`
into `Watchlist.target_prices` (`dict[str, float]`, empty by default so plain
string lists behave exactly as before). Three accepted forms, mixable in one
file: a per-entry mapping with `target_price:` (or the `buy_price:` alias the
TII list uses), or a top-level `targets: { TICKER: price }` block. Keys are
`.strip().upper()`-normalised; prices are coerced to float and anything `<= 0`
or non-numeric is dropped. GBX pence buy prices are written the way the source
spreadsheet holds them — `"500.00p"`, `"9,500.00p"` — and `_coerce_price`
strips the thousands comma and trailing `p` to a numeric pence value; so
`watchlists/tii_watchlist.yaml` carries AUTO/LSEG/RMV buy prices in pence.
Whatever unit a buy price is in, the quote Wally screens it against must match:
UK names are quoted in pence on their home exchange, so a pence buy price
compares correctly there.

There are five standard watchlists, de-duplicated in priority order
**Income → JM → Aussie Tech → Cornerstone → Nap Taker**: a ticker that appears
in more than one is kept only in the highest-priority list. **Buy prices follow
the ticker**, not the list — a global price map (Cornerstone's Buy-Below
preferred, else Nap Taker's) is attached to whichever list each ticker lands in,
so a name that moves lists still carries its buy price and stays flagged.

The lists and where their names/prices come from:

- **Income Watchlist** (`income_watchlist.yaml`) — the best dividend payers
  pulled out of every other list (except TII75): names with a ~4%+ cash yield
  plus the franked blue-chip anchors (big-four banks, BHP, Telstra, Transurban,
  the income REITs). Yield traps (GQG, ADH, IPH — high yield only because the
  price collapsed) and broken theses (LAU) are deliberately excluded. Highest
  priority, so these names live here rather than in JM/Cornerstone/Nap Taker.

- **JM Watch List** (`jm_watchlist.yaml`) and **Aussie Tech Watchlist**
  (`aussie_tech_watchlist.yaml`) — the user's own ticker lists (from
  `Watchlists_Aug_26`), no native buy prices; they inherit prices via the map.
- **Cornerstone Watchlist** (`tii_watchlist.yaml`) — the list formerly named
  "TII Watchlist", renamed so the source's buy prices aren't published under
  their brand. Filename kept as `tii_watchlist.yaml`; the display `name:` is
  "Cornerstone Watchlist" and `_load_portfolio_targets` maps that name to
  `config/tii_portfolio_targets.yaml`. Buy prices from the TII sheet's
  "Buy Below" column, GBX pence as written.
- **Nap Taker Investing** (`naptaker_watchlist.yaml`) — analyst buy
  recommendations under two years old, merged from the five Motley Fool
  scorecard tabs (Buy status only), deliberately not named after the source.
  Buy price = the price at the date of recommendation. US names are stored bare
  (no `.AX`); a few source tickers needed correcting off the intact concatenated
  name column (e.g. AMD, GOAT, SGLLV, HWM, TTD).

`tii75_watchlist.yaml` is a separate canonical list and stays untouched at
exactly 30. `config/tii_portfolio_targets.yaml` (`buy_below`, used for the
email's Portfolio Targets block) is a separate, older source and may lag.

`TickerScreenResult` gained `near_low`, `target_price`, `below_target` and
`distance_to_target_pct` (all defaulted, so the empty/error constructors in
`main.py` still work). They flow into `outputs/` JSON via `to_dict()`, into
`docs/data/wally.json`, and onto the dashboard as a **Buy Price** column plus a
**Trigger** cell (below-buy hits highlighted green as a buying opportunity). The
email flagged table gained matching **Target** and **Trigger** columns
(`_flagged_row` / `_trigger_reasons` in `wally/email_report.py`).

### Watchlists live in `watchlists/`, and only there
`wally/config.py` used to point `STANDARD_WATCHLISTS` at `.github/Watchlist/`,
a second copy dating from March 2026. Every improvement — the Aug-26 buy-price
sync, the TII → JM → Aussie Tech de-duplication — was written to
`watchlists/`, which nothing loaded. Wally screened the old 74-name TII list,
returned `target_price: null` on every row, and the below-target trigger was
dead for as long as it existed, while the run reported success and the email
looked normal. `.github/Watchlist/` has been deleted so there is one place to
edit, and `tests/test_wally_watchlist_paths.py` fails if a second copy
reappears or if the TII list stops carrying prices.

## GitHub Pages — one publisher, triggered by the agents

The site at `https://johnnym77.github.io/Reporting-Agent/` has two halves that
share one deploy: the combined agent dashboard (`docs/index.html`, generated by
`scripts/build_dashboard.py`) at `/`, and Theo's slides at `/theo/`. A Pages
deploy replaces the entire site in one shot, so both halves must be built and
uploaded by the same workflow. `.github/workflows/theo-pages.yml` ("Publish
site") is that workflow, and it is the only one allowed to deploy. A second
workflow publishing either half on its own would silently delete the other.

### Pages source must be "GitHub Actions"
Settings → Pages → Build and deployment → Source. Anything else breaks the
site, and the two failure modes look nothing alike:

- **Deploy from a branch, `main` / `(root)`** — Jekyll finds no `index.html` at
  the repo root and renders `README.md` instead. The site turns into a plain
  README page. This is what "the styling disappeared" looks like.
- **Deploy from a branch, `main` / `docs`** — the dashboard renders correctly,
  but `/theo/` 404s. `site/` is in `.gitignore`; Theo's slides only ever exist
  inside a CI run, so a branch deploy cannot see them.

Neither can be fixed from the repo — it is a settings toggle. Flipping it back
to "GitHub Actions" then re-running the workflow restores the site.

### Why the deploy hangs off `workflow_run`, not off a commit
Bob, Ned, Wally and Sally each write their own `docs/data/*.json`, re-run
`build_dashboard.py`, and commit `docs/index.html` with **`[skip ci]`**. That
tag is what keeps four agents from triggering each other into a loop — and it
also stops the Pages workflow from ever seeing those pushes. The result was a
site frozen at the last thesis change while `docs/index.html` moved in git
every morning: Bob ran, the email arrived, the website did not move. Nothing
looked like an error, which is what made it expensive to spot.

Adding `docs/**` to the `push` paths does not fix that on its own — `[skip ci]`
suppresses the event before any path filter is consulted. So the workflow
listens for the agent *runs* completing instead:

| Trigger | When |
|---|---|
| `Daily Announcement Digest` (Bob) | daily, 23:13 UTC |
| `Ned News Agent` | daily, 23:30 UTC |
| `Wally Watchlist Screening` | Friday, 22:30 UTC |
| `Selling Sally Weekly Review` | Sunday, 00:00 UTC |

Two details this depends on. `workflow_run` only fires for workflows defined on
the default branch, so these triggers do nothing until the change is merged to
`main`. And `github.sha` on a `workflow_run` event is the default branch's tip
as of when the event fired, which can predate the dashboard commit the
triggering run made moments earlier — the checkout pins `ref:` to the branch
name (`TARGET_BRANCH`) so the deploy always carries the newest data.

The trigger is not gated on the agent run succeeding. A run that emailed its
digest and then tripped over on a later step has still committed data worth
publishing, and re-deploying unchanged content costs nothing.
