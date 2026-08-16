# Reporting-Agent — Claude Notes

## Environment

### User's Windows PC (self-hosted GitHub Actions runner)
- **Windows username**: `mr_co`
- **Home directory**: `C:\Users\mr_co`
- **Python path**: `C:\Users\mr_co\AppData\Local\Programs\Python\Python314`
- **Actions runner**: installed at `C:\actions-runner`, service name `actions.runner.JohnnyM77-Reporting-Agent.mr_co-runer`
- **Runner name in GitHub**: `mr_co-runer` (note: typo in name, registered as-is)
- **Git Bash**: installed (required for `shell: bash` in workflow)

### Why self-hosted runner?
SWS (SimplyWallSt) uses Cloudflare Bot Management which blocks GitHub-hosted runner IPs (Azure datacenters). The self-hosted runner on `mr_co`'s home PC uses a residential IP that passes Cloudflare.

### SWS Drip Bot
- Workflow: `.github/workflows/sws_drip.yml`
- Runs daily at 23:30 UTC (09:30 AEST) on the self-hosted runner
- Downloads 2 ASX ticker CSVs per day from SWS
- Auth: `SWS_STORAGE_STATE` secret (base64-encoded Playwright storage_state.json)
- Uses `curl-cffi` with Chrome TLS impersonation + `cf_clearance` cookie

### GitHub repo
- Owner: `JohnnyM77`
- Repo: `Reporting-Agent` (private)

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

**Permissions gotcha**: a file created by the service account is *owned by the
service account*, so its `webViewLink` 404s for a human until the file is
shared. `_share_drive_file` adds a reader permission for `GDRIVE_SHARE_EMAIL`
(falling back to `EMAIL_TO`, comma-separated allowed) with
`sendNotificationEmail=False`. The `drive.file` scope is sufficient because the
service account only ever touches files it created itself.

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
