# Bob the Bot — V2 Baseline

This repository snapshot is the **V2 working baseline**.

## What's new in V2

- Announcement window tightened to the last **24 hours**.
- Consecutive-day duplicate suppression via persisted `state_seen.json` keys.
- GitHub Actions workflow now restores/saves seen-state using `actions/cache`.
- Existing guardrails retained (timeouts, fail-closed date parsing, meaningful-text checks before LLM calls).

## State behavior

- A stable key is generated per announcement using SHA-1 of `ticker|url`.
- Keys are persisted with timestamps and retained for 72 hours.
- If a key is present in state, the announcement is skipped on the next run.

