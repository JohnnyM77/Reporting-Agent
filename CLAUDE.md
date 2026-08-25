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
or non-numeric is dropped — GBX pence strings (`500.00p`) are deliberately
ignored rather than mis-flagged, which is why `watchlists/tii_watchlist.yaml`
omits the buy prices for AUTO/LSEG/RMV (their quotes and buy prices are in
pence, and a raw `float("...p")` would fail anyway). The TII buy prices come
from `config/tii_portfolio_targets.yaml` (`buy_below`).

`TickerScreenResult` gained `near_low`, `target_price`, `below_target` and
`distance_to_target_pct` (all defaulted, so the empty/error constructors in
`main.py` still work). They flow into `outputs/` JSON via `to_dict()`, into
`docs/data/wally.json`, and onto the dashboard as a **Buy Price** column plus a
**Trigger** cell (below-buy hits highlighted green as a buying opportunity). The
email flagged table gained matching **Target** and **Trigger** columns
(`_flagged_row` / `_trigger_reasons` in `wally/email_report.py`).

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
