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
