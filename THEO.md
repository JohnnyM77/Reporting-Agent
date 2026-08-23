# Theo — thesis accountability manager

Bob reads announcements. Wally watches lists. Sally does the weekly review.
Theo does the thing none of them do: it holds the record of **why** each
position was bought, **what would make it a sell**, and it says out loud when
the reasoning has moved.

```
python -m theo.cli check          # validate every thesis (CI gate)
python -m theo.cli list           # one line per thesis
python -m theo.cli drift --all    # what has moved
python -m theo.cli show BHP --current --format png
python -m theo.cli season --today 2026-07-01
python -m theo.cli publish        # build site/index.html
python -m theo.cli new WES        # scaffold a blank thesis
```

---

## The design principle

**Thesis files contain judgement only. Never numbers.**

No prices, no share counts, no cost basis, no returns in the markdown. Every
figure on a slide is pulled from the transaction ledger at render time.

Two reasons, and both of them are about failure modes that have already
happened elsewhere in this repo:

1. **A slide cannot go stale.** If the number lives in the file, the file is
   wrong the day after it is written and nobody notices for two years.
2. **A typo cannot become permanent.** Type `$24.94` instead of `$24.49` once
   and it is in the record forever, quoted back at you by your own system.

There is a third reason that matters more. The story you tell yourself about a
position drifts toward the flattering version, and the ledger is the only thing
in the room that does not. BHP is the worked example: from memory it is *"I
bought BHP when the Samarco dam broke."* From the ledger it is three purchases,
two of them before the dam failed at all. The file records the judgement, the
ledger records what actually happened, and the slide puts them side by side
where the difference is visible.

**The ledger is optional.** `data/portfolio.xlsx` is not in the repo. Without
it everything still runs — slides render the judgement with dashes where the
figures go, the site shows a `—` in the IRR columns, and the gap list is
suppressed because Theo cannot know what it is missing. Drop the file in and
every number fills itself in with no other change.

---

## The file format

One file per holding: `theses/<TICKER>.md`, YAML frontmatter and then free
prose. The prose is yours; the frontmatter is what Theo reasons over.

| Field | Notes |
|---|---|
| `ticker`, `name` | |
| `archetype` | `COMPOUNDING_MACHINE`, `CYCLICAL_TRADE`, `STRUCTURAL_WINNER`, `SPECULATIVE_PREPROFIT`, `VALUE_TRAP`, `MELTING_ICE_CUBE` |
| `status` | `HELD` / `EXITED` |
| `evidence_grade` | **A** contemporaneous · **B** reconstructed with documents · **C** memory only |
| `conviction`, `horizon` | free text |
| `origin` | `OWN` / `BORROWED` / `HYBRID` |
| `draft` | true while the file is still admittedly incomplete |
| `sources[]` | `name`, `outlet`, `url`, `alignment` (`ALIGNED` / `DIVERGED` / `INDEPENDENT`), `note` |
| `the_bet` | one sentence, 60 words max — the reason to own it |
| `what_i_know` | what you know that the other side of the trade does not |
| `pillars[]` | `id`, `claim`, `evidence`, `kill_condition`, `status` (`INTACT` / `STRAINED` / `BREACHED`) |
| `pre_mortem[]`, `pre_mortem_hindsight` | it is three years out and this was a mistake — what happened? |
| `alternative_considered` | `name`, `why_rejected` |
| `management_score`, `management_verdict` | |
| `valuation_note` | what price was paid, against what |
| `hold_thesis` | why it is **still** held, where that differs from why it was bought |
| `resolution_date`, `resolution_criterion` | by that date, what tells you this worked? |
| `reviews[]` | `date`, `verdict`, `pillar_status{}`, `expected_vs_actual`, `process_score`, `outcome_score`, `amendments[]` |
| `amendments[]` | `pillar`, `change`, `trigger`, `direction` (`LOOSENED` / `TIGHTENED` / `NEUTRAL`) |
| `exit` | `date`, `reason`, `pillar_failed`, `kill_condition_fired`, `sell_thesis`, `capital_went_to` |
| `exit.reason` | `THESIS_BROKEN`, `THESIS_PLAYED_OUT`, `BETTER_USE`, `RISK_MANAGEMENT`, `LOST_PATIENCE`, `NEEDED_CASH` |

`amendments[].direction` is the field that does the real work. A thesis that
survives by being edited looks identical to a thesis that survives on merit —
unless you record which way each edit went.

---

## Validation rules

`python -m theo.cli check` exits non-zero on any of these. They are hard
failures, not warnings, and each one is deliberate.

| Rule | Why |
|---|---|
| `the_bet` is empty, or over 60 words | If it cannot be said in a sentence, the reason was never actually articulated. Sixty words is generous. |
| More than 4 pillars | Six reasons to own something is none. Nothing is load-bearing when everything is. |
| Any pillar without a `kill_condition` | An untestable pillar is a feeling. This is the rule the whole system rests on. |
| `origin` is `BORROWED`/`HYBRID` with no `sources` | Where the idea came from is exactly the thing worth knowing three years later. |
| An `exit` block with no `sell_thesis` | The reason capital moved is the only part of an exit worth keeping. |

Softer checks (unknown archetype, unknown pillar status, a review pointing at a
pillar that does not exist, a bad amendment direction) also fail the build —
they are almost always typos.

Note what is *not* a failure: a `kill_condition` of `"NEEDS WORK"` passes. NHC
ships that way on all three pillars. The file is allowed to be honest about
being incomplete; what it is not allowed to do is be silently incomplete. The
season pack then asks about each one by name until it is fixed.

---

## Drift

Drift is not the price going the wrong way. Drift is the *reasoning* moving.

| Finding | Severity | Fires when |
|---|---|---|
| `PILLAR_BREACHED` | ALERT | A pillar is marked breached and the position is still open |
| `KILL_CONDITIONS_LOOSENED` | ALERT | 2+ kill conditions have been loosened after the fact |
| `PILLAR_PERSISTENTLY_STRAINED` | ALERT | A pillar has been strained at 3 consecutive reviews without being called either way |
| `RESOLUTION_OVERDUE` | WARN | The resolution date passed and nothing was written down |
| `PRE_SEASON_REVIEW_DUE` | WARN | Inside the January/July prep window with no review logged this month |
| `STALE` | WARN | Last reviewed 12+ months ago |
| `NEVER_REVIEWED` | WARN | Written and never revisited |
| `AMENDMENT_RATE` | WARN | More than one amendment per review — the thesis is being edited to survive |
| `DIVERGENCE_OPEN` | INFO | Held against a source who has since diverged |
| `GRADE_C` | INFO | Reconstructed from memory, so treat the confident parts with suspicion |

**Verdict** — `DRIFTING` if any alert, `WATCH` if any warning, `CLEAN` if
neither. It appears on the site table, on every non-entry slide, and orders the
season pack worst-first.

`PRE_SEASON_REVIEW_DUE` is a calendar fact rather than a property of the
thesis, so it lives in `theo/season.py`. Call `season.assess()` rather than
`drift.check()` unless you deliberately want the calendar ignored.

---

## The season cadence, and why it runs a month early

The ASX reports in **February and August**. Theo runs on **1 January and
1 July**.

A thesis reviewed *after* the result is reviewed with the answer already on the
page. It is worth nothing. It will find whatever it needs to find to make the
last six months look intended, every time, and it will feel like rigour while
it does it. The only review that can actually be scored is the one committed to
before the number lands.

So the pack does not ask "was the thesis right". Per pillar it asks:

> **What number in this result would you need to see to keep calling this
> intact?**

Then it asks about every open divergence — someone whose view you took and who
has since changed their mind — and about any gap between why you bought and why
you hold, because a hold thesis written after the position worked is the single
most common way a thesis becomes unfalsifiable.

Holdings with capital and no thesis are listed at the bottom, ranked by size.

`theo-season.yml` builds the pack, emails it over SMTP using the same
`EMAIL_FROM` / `EMAIL_TO` / `EMAIL_APP_PASSWORD` secrets Bob uses, and uploads
it as an artifact so a missing secret never loses the pack.

---

## Signals — what happens when Sally says trim and you say no

Sally flags a holding. You decline. **That decline is the single most
important thing in the system and it is the one thing that never gets written
down** — a year later it is remembered as conviction rather than as "I waved
off a valuation flag three times running."

So a signal from another agent opens a **question** against the thesis:

- **It never blocks.** A blocking prompt gets clicked through, and then you
  have trained yourself to ignore it.
- **The question stays open until answered**, and an open question is a drift
  finding (`SIGNAL_UNANSWERED`, warn). Silence gets recorded.
- **Declining is a fine answer.** It just has to be written down — a dated
  `reviews[]` entry with `trigger: SALLY_TRIM` and `decision: DECLINED`.
- **Declining three times without ever tightening a kill condition is an
  alert** (`SIGNAL_REFUSED_REPEATEDLY`). Not "you were wrong to hold", but
  "you have refused this question three times and still have not said what
  would change your mind." Writing the kill condition clears it.

```
python -m theo.cli signals
```

prints each open question plus a paste-ready `reviews:` block. Theo does not
edit your thesis files — a tool that rewrites your prose to add a field is a
tool you stop trusting with your prose.

The value is not in any single answer, it is in the **diff between answers over
time**. Four declines with four different reasons is a thesis being edited to
survive, which is why `amendments[].direction` exists.

**Coupling is one-way.** `theo/signals.py` only ever *reads*
`docs/data/sally.json`. Sally is not modified and does not know Theo exists.
Adding Bob is a new `from_bob()` function and nothing else. The step in Sally's
workflow is `continue-on-error` so a bug in Theo can never take down a working
agent's weekly run.

### The worked example, on the day this shipped

Sally flagged BHP and PWH, both Tier 3 trim candidates.

- **BHP** — Sally says trim on valuation. Gaurav Sodhi has published a formal
  sell, also on valuation. Two independent valuation-based sell signals, and
  BHP's three pillars contain **no valuation kill condition at all**: the entry
  case was cost curve, capital discipline, cash release. Nothing says at what
  price you stop holding. That gap is what both signals are pointing at.
- **PWH** — no thesis exists. Capital committed, a signal raised, and no
  written reason to own it.

---

## The combined dashboard

Two surfaces, deliberately:

- **`/`** — the existing agent dashboard (`scripts/build_dashboard.py`). Bob,
  Wally, Sally and now Theo, one card each. Theo's card is a glance: open
  questions first, then drift verdicts, then holdings flagged with no thesis.
- **`/theo/`** — Theo's own site. The slides are a page each; that is not a
  dashboard thing.

`python -m theo.cli dashboard` writes `docs/data/theo.json`, the same pattern
every other agent uses. `build_dashboard.py` reads it. Both Sally's workflow
and `theo-pages.yml` regenerate it, and both commit with `[skip ci]` so the
push cannot re-trigger the workflow that made it.

---

## Slides and the site

Three kinds, and the difference between them is the point.

- **entry** — what was thought at the time of the buy, and *deliberately
  nothing else*. No current value, no drift banner, all pillars shown intact.
  A record that knows how it turned out cannot score the decision; it reads as
  either obvious foresight or obvious stupidity, and it was neither.
- **review** — one per logged review: pillar statuses as recorded on that date,
  and the amendments made.
- **current** — live pillar status, drift check, today's numbers, and the
  `hold_thesis` in place of `the_bet` where one exists.
- **exit** — the sell thesis, which pillar failed, and where the capital went.

Rendered from Jinja2 (`theo/templates/`) to a one-page A4-landscape HTML slide,
then to PNG or PDF via Playwright at `device_scale_factor=3`, screenshotting the
`.slide` element. `_styles.html` and `_slide.html` are fragments so the site can
reuse them; `slide.html` is the standalone wrapper. The columns are hard-clipped
— a slide is one page, and if a thesis outgrows the box the fix is to write less
on the slide, not to let it bleed over the metric strip.

If Playwright's bundled Chromium does not match the installed package (some CI
images pin their own), set `THEO_CHROMIUM_PATH` to the browser binary.

`python -m theo.cli publish` builds a **single self-contained `site/index.html`**
— every slide pre-rendered at build time and embedded as JSON, switched with
plain JS. No framework, no fetch, no CDN, so it works from `file://` as happily
as from Pages. Deep links are `#BHP/current`. Clicking a table row opens that
ticker.

**Dollar amounts are suppressed by default** because this deploys publicly.
IRRs, multiples and historical share prices stay — they say how the decisions
went without saying how much money is involved. `--show-amounts` includes them.

### One thing to know about Pages

GitHub Pages for this repo currently serves the agent dashboard out of `docs/`.
An Actions-based Pages deploy takes that URL over. So `theo-pages.yml` stages
the artifact as **`docs/` at the root plus Theo at `/theo/`** — the dashboard
keeps the URL it has, and Theo lives one level down. `docs/` is only ever read,
never written.

If you would rather Theo owned the root, delete the staging step and point
`upload-pages-artifact` at `site` — but check where the dashboard is being
served from first.

---

## Build order — what is left

1. **Backfill the ~30 unwritten holdings, ranked by capital.** `theo.cli gaps`
   produces the list the moment the ledger is present. Biggest positions first,
   because an unwritten thesis on the largest holding is the most expensive gap
   in the system. Grade C is fine — an honest C beats a flattering B.
2. **Feed Bob's announcement output into pillar-level observations.** Sally is
   wired in; Bob is not. Bob already reads and analyses every results
   announcement for these tickers. The missing link is mapping a result back to
   the pillar it bears on: BHP's WAIO unit costs land on P1, not on "BHP". The
   signal plumbing exists — this is a `from_bob()` in `theo/signals.py` plus a
   way to say which pillar a finding hits.
3. **Capture season pack answers back into the files as dated review blocks.**
   Right now the pack is a text file you answer somewhere else, which means the
   answers are lost. The answers *are* the review — they should land in
   `reviews[]` with the date, the pillar statuses and any amendments, so next
   season's drift check can see them.
4. **Exit post-mortems, scored at 12 and 36 months.** `process_score` and
   `outcome_score` already exist in the schema and are unused. The interesting
   question about a sale is not whether the price went up afterwards, it is
   whether the *decision* was sound given what was knowable — and you cannot
   answer that on the day.
5. **A Telegram `/thesis BHP` command.** The whole point of the one-page slide
   is that it is glanceable. Being able to pull one up on a phone during a
   trading halt is when it earns its keep.

---

## Layout

```
theo/
  thesis.py        parse + validate the markdown files
  ledger.py        OPTIONAL — rebuild every BUY as a Decision, XIRR by bisection
  drift.py         the honesty mechanism
  signals.py       Sally's flags become questions that will not go away
  season.py        the January/July review pack
  render.py        slide contexts + Playwright rendering
  publish.py       build the self-contained site
  cli.py           command line
  templates/       _styles.html, _slide.html, slide.html, site.html
theses/            one markdown file per holding
tests/test_theo.py plain script, no pytest needed
.github/workflows/theo-pages.yml    publish on push to main
.github/workflows/theo-season.yml   cron 0 23 1 1,7 *
```

`slides/` and `site/` are generated and gitignored.
