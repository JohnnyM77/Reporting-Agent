# Master Engine: status

**Scaffolding. No workflow runs it.** Keep it importable and tested. Don't
wire it into production just because it exists.

## What is live

- **`shared/events.py` → `InvestorEvent`** (moved here from
  `master_engine/schemas.py`). Harry writes one `InvestorEvent` per report
  to his private store on every production run (`hindsight/emit.py`). It's
  the canonical internal event shape for Bob, Ned, Wally, Harry and future
  agents.

## What is built but not run

| Module | Does |
|---|---|
| `aggregator.py` | collects events from per-agent collectors and de-duplicates them |
| `prioritizer.py` | scores by event-type severity and sorts |
| `linker.py` | adds Yahoo / Market Index / ASX links |
| `renderer.py` | HTML, markdown and JSON digest |
| `notifier.py` | saves the digest and emails it via `shared/email_service.py` |
| `bob_emit.py`, `ned/emit.py`, `wally/emit.py` | turn each agent's dashboard JSON into `InvestorEvent`s |
| `hindsight/emit.py` `collect_events` | Harry's events as a fourth collector |

These are covered by `tests/test_aggregator.py`, `test_prioritizer.py`,
`test_linker.py`, `test_renderer.py` and `test_schemas.py`.

## What no longer exists

The README and `docs/WALLY_AND_MASTER_ENGINE_CHANGES.md` used to describe a
`master_engine_alert.yml` workflow, a `run_master_investor.py` entry point
and an `agents/super_investor/scoring.py` scorer. None of them are in the
repo. The empty `agents/` package was removed in the 2026-09 cleanup, along
with the prioritiser's import of the missing scorer (that import always
failed, so the severity scorer was already the one in use).

## If you wire it up later

The emitters already read the agents' published `docs/data/*.json`, the same
way Harry's adapters do. Keep it that way: Master Engine should consume
agent outputs and never import agent internals.
