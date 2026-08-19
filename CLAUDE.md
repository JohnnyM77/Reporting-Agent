# Reporting-Agent — Claude Notes

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
