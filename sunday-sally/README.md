# Sunday Sally

Sunday Sally is a weekly valuation review agent that reuses Bob's portfolio universe from `tickers.yaml` and runs every Sunday morning (Asia/Singapore).

## Mission

Sunday Sally flags portfolio names near 52-week highs, checks for rich valuation relative to company history, builds evidence packs, and sends a skeptical review memo + spreadsheet. She never auto-sells.

## Run locally

```bash
pip install -r sunday-sally/requirements.txt
python -m sunday-sally.src.main
```

or from folder:

```bash
cd sunday-sally
python -m src.main
```

## Output structure

Sunday Sally writes outputs to:

`data/outputs/YYYY/YYYY-MM-DD Weekly Review/`

Each run includes:
- `summary_email.md`
- `run_log.json`
- one folder per flagged ticker with:
  - `memo.md`
  - `valuation_review.xlsx`
  - `handoff_payload.json`
  - `source_docs/announcement_index.json`

## Scheduler

GitHub Actions workflow:

- `.github/workflows/sunday_sally_weekly_review.yml`
- Cron: `0 0 * * 0` (08:00 Singapore Sunday)

## Environment

Use `.env.example` for required variables.


### Portfolio source resolution

Sunday Sally hard-pins portfolio file selection to Bob's repo-root `tickers.yaml`, and path handling works whether run from repo root or from `sunday-sally/`.

Optional overrides still available for ticker key/suffix and config location:

- `SALLY_PORTFOLIO_SOURCE_KEY`
- `SALLY_PORTFOLIO_EXCHANGE_SUFFIX`
- `SALLY_PORTFOLIO_CONFIG_PATH`



## Delivery mode

Google Drive uploads are disabled for Sunday Sally. All generated outputs for each run are attached directly to the email summary.
