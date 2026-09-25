# Run Bob from your phone

You don't have to open the GitHub Actions UI to run Bob. The daily workflow
accepts three optional inputs on a manual run, and any of them can be fired from
your phone with a single tap plus a typed ticker.

## The three inputs

| Input | Use it for | Window |
|---|---|---|
| `results_ticker` | "What were HPG's last results?" — pulls the most recent half/full-year report and runs the full analysis | ~6 months back |
| `manual_ticker` | A ticker not in your portfolio, everything it's announced lately | last 7 days |
| `force_rerun_tickers` | Re-run a portfolio name you've already been emailed about | full history |

`results_ticker` is the one for the pub scenario. It surfaces **only** the
latest results card — not six months of headlines — and if the company hasn't
reported in the window you get a plain "no results found" note.

A `results_ticker` / `manual_ticker` run **emails you the analysis but does not
touch the portfolio dashboard or that evening's scheduled digest** — it's a
throwaway query, so fire as many as you like.

## Option A — iOS Shortcut (recommended, one tap + type)

This makes a home-screen icon that asks for a ticker and runs Bob on it.

**One-time setup**

1. Create a GitHub token that's allowed to start the workflow:
   - GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate.
   - Repository access: **Only select repositories** → `JohnnyM77/Reporting-Agent`.
   - Repository permissions: **Actions → Read and write** (this is what lets it
     dispatch a workflow). Leave everything else at No access.
   - Copy the token (starts `github_pat_…`). Treat it like a password.
2. Open the **Shortcuts** app → **+** → add these actions in order:
   - **Ask for Input** — Prompt: `Ticker to analyse` (e.g. HPG). Input Type: Text.
   - **Text** — put your token in it (this keeps it out of the URL/headers view).
     Value: `github_pat_your_token_here`.
   - **Get Contents of URL**:
     - URL: `https://api.github.com/repos/JohnnyM77/Reporting-Agent/actions/workflows/daily.yml/dispatches`
     - Method: **POST**
     - Headers:
       - `Accept` = `application/vnd.github+json`
       - `Authorization` = `Bearer ` followed by the **Text** action from above
     - Request Body: **JSON**
       - `ref` (Text) = `main`
       - `inputs` (Dictionary):
         - `results_ticker` (Text) = the **Provided Input** (the ticker you typed)
   - **Show Notification** — Text: `Bob is analysing [Provided Input] — email inbound.`
3. Name it "Ask Bob", pick an icon, and **Add to Home Screen**.

**Using it:** tap the icon → type `HPG` → done. Bob runs on GitHub's servers and
the PDF lands in your inbox a couple of minutes later. Nothing runs on the phone.

To make a second button for the 7-day sweep, duplicate the Shortcut and change
the one input key from `results_ticker` to `manual_ticker`.

## Option B — any phone, a bookmarkable command

If you'd rather not build a Shortcut, the same call works from any HTTP client
(Android's Tasker/HTTP Shortcuts, a Termux `curl`, etc.):

```bash
curl -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer github_pat_your_token_here" \
  https://api.github.com/repos/JohnnyM77/Reporting-Agent/actions/workflows/daily.yml/dispatches \
  -d '{"ref":"main","inputs":{"results_ticker":"HPG"}}'
```

Swap `results_ticker` for `manual_ticker` or `force_rerun_tickers` as needed.
A `204 No Content` response means it started; you'll get the email shortly.

## Option C — GitHub mobile app (no token)

The official GitHub app can do it without a token, just more taps: **Actions →
Daily Announcement Digest → Run workflow →** fill `results_ticker` **→ Run**.
Fine as a fallback; the Shortcut is faster for the pub.

## Notes

- The token only needs Actions read/write on this one repo. If it ever leaks,
  revoke it in Developer settings and generate a new one — nothing else is at risk.
- `results_ticker` accepts a comma-separated list (`HPG,PME`) if you want a few
  at once.
- ASX tickers only; use the bare code (`HPG`), no `.AX` suffix.
