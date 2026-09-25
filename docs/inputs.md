# Triggering Ned from your phone or terminal

Ned's transcript workflows (YouTube and podcast) are `workflow_dispatch`
Actions in this repo. This doc walks through three faster ways to fire them
than clicking around in the GitHub Actions UI.

- **iOS Shortcut** — share a link from Overcast / YouTube / Safari straight
  to Ned.
- **CLI alias** on your PC — `ned pod <url>` / `ned yt <url>` from any
  terminal.
- **GitHub Actions "Run workflow" button** — the original, still works, no
  setup.

All three end up calling the same GitHub REST API, and any run — regardless
of how it was dispatched — emails you the digest and updates the Transcripts
section on the dashboard.

---

## iOS Shortcut

**One-time setup: get a Personal Access Token (PAT)**

The Shortcut needs to call GitHub as you, so it needs a token.

1. On a computer, open
   <https://github.com/settings/personal-access-tokens/new> (**fine-grained
   tokens**, not classic).
2. Fill in:
    - **Token name:** `Ned iOS Shortcut`
    - **Expiration:** whatever you want — 90 days is fine, or set it to
      "no expiration" if you don't want to redo this in three months.
    - **Repository access:** *Only select repositories* → pick
      `JohnnyM77/Reporting-Agent`. Do not give it access to your other
      repos.
    - **Permissions → Repository permissions → Actions:** *Read and write*.
      This is the only permission needed. Everything else stays *No access*.
3. Click **Generate token**. Copy the token that appears — it's shown
   once. Text it to yourself so you can paste it in the Shortcut in a
   minute.

**Build the Shortcut (on your iPhone)**

1. Open the **Shortcuts** app → tap **+** (top-right) to make a new
   Shortcut.
2. Rename it **Ned Digest** at the top.
3. Tap the "i" info icon at the bottom → tap **Show in Share Sheet**.
    Under "Share Sheet Types" leave everything on so it appears for URLs
    and text.
4. Add these actions in order (use the search box at the bottom to find
   each one):

   - **Get Contents of URL**
     - Tap the URL field and paste the *Shortcut Input*'s text as the URL
       (i.e. long-press the field → "Insert Variable" → Shortcut Input).
     - This action is here so we can force the input to a plain string.

   - **Choose from Menu** (searches as "Menu")
     - Prompt: `What kind of link?`
     - Items: `Podcast`, `YouTube`
     - For each item, add the matching **Get Contents of URL** below:

   **Under "Podcast":**

   - **Get Contents of URL**
     - **URL:** `https://api.github.com/repos/JohnnyM77/Reporting-Agent/actions/workflows/ned_podcast.yml/dispatches`
     - **Method:** `POST`
     - **Headers:** tap → add:
        - `Accept` = `application/vnd.github+json`
        - `Authorization` = `Bearer YOUR_PAT_HERE`  ← paste the PAT you
          made above
        - `X-GitHub-Api-Version` = `2022-11-28`
     - **Request Body:** *JSON*
        - Add field: `ref` (String) = `main`
        - Add field: `inputs` (Dictionary)
          - Inside: `podcast_url` (String) = **Shortcut Input** variable

   **Under "YouTube":** same as above but URL ends in
   `ned_transcript.yml/dispatches` and the input field is `youtube_url`
   (not `podcast_url`).

5. Add a **Show Notification** action after the menu ("Ned dispatched
   ✓") so you get feedback that the tap worked.

**Use it**

In Overcast, tap an episode → share → **Ned Digest** → **Podcast** → the
workflow starts, and a couple of minutes later you get the email.

In the YouTube app, on a video: share → **Ned Digest** → **YouTube**.
(YouTube still needs your PC + self-hosted runner to be on, per
`CLAUDE.md` — the Shortcut fires the workflow, the runner does the work.)

**If the PAT ever leaks or you rotate it:** delete the token at
<https://github.com/settings/personal-access-tokens>, make a new one, and
paste it back into the Shortcut. The old token becomes useless the moment
you delete it.

---

## CLI alias on your PC

**On macOS / Linux (bash or zsh):**

```bash
export NED_CLI_REPO="$HOME/code/Reporting-Agent"   # adjust to your checkout
source "$NED_CLI_REPO/scripts/ned-cli.sh"
```

Add those two lines to `~/.zshrc` or `~/.bashrc` so they persist.

**On Windows (PowerShell):**

```powershell
notepad $PROFILE     # creates the profile if it doesn't exist
```

Add these two lines and save:

```powershell
$env:NED_CLI_REPO = "C:\path\to\Reporting-Agent"
. "$env:NED_CLI_REPO\scripts\ned-cli.ps1"
```

Then open a new terminal.

**Usage** (both platforms):

```
ned pod https://podcasts.apple.com/…
ned yt  https://www.youtube.com/watch?v=…
```

Uses the `gh` CLI, which reuses your existing GitHub authentication — no
extra token to manage. Install: `winget install --id GitHub.cli` on
Windows, `brew install gh` on macOS. Then `gh auth login` once.

---

## What the workflow does with it

Both workflows follow the same path:

1. Fetch and transcribe the episode (podcast: try the published RSS
   transcript first, fall back to Whisper; YouTube: read the caption
   track directly). YouTube runs also look up the real video title and
   channel (oEmbed, no key needed) so the email and PDF aren't named
   after a video ID.
2. Load your portfolio context: holdings from `tickers.yaml`, every
   thesis's pillars and kill conditions from `theses/*.md`, and the
   watchlists in `watchlists/`. About 25k characters; the log prints the
   exact size.
3. Analyse the episode against that context on Claude Sonnet
   (`NED_TRANSCRIPT_MODEL`, default `claude-sonnet-4-6`): relevance to
   your portfolio (High / Medium / Low / None), which holdings or
   watchlist names it supports, challenges or puts a kill condition at
   risk, the 3 to 5 year lens (themes, historical parallels and where they
   break, second-order effects, winners and losers), a sceptic's corner,
   new ideas, and what to watch next. A transcript longer than
   `NED_TRANSCRIPT_LLM_MAX_CHARS` (default 400,000, roughly 7 hours) is
   never clipped: it is read in parts first, and the PDF cover says so.
4. Render the PDF: cover with the relevance call and TL;DR, portfolio
   impact cards, the medium-term lens, the rest of the analysis, then the
   full transcript with time markers. Saved under `docs/transcripts/`.
5. Email you a short version (subject `Ned [Medium] Podcast: <title>`)
   with the PDF attached. If the analysis fails, the subject says
   `[Digest failed]` and the email and PDF give the reason.
6. Append the entry (including the structured analysis) to
   `docs/data/transcripts.json`, prune any transcript PDF older than the
   retention window (default 30 days; override with
   `NED_TRANSCRIPT_PDF_RETENTION_DAYS`), rebuild `docs/index.html`, and
   push. That triggers the Pages workflow, which redeploys the site with
   the new item in the **Transcripts** section, including a relevance
   badge and a green **"📄 Download PDF"** button. The button 404s once
   the retention window has passed and the file's been pruned, which is
   the deliberate "for a short time" behaviour.

### Tuning (optional repo variables)

Set these under **Settings → Secrets and variables → Actions →
Variables** to override the defaults without editing code:

| Variable | Default | What it does |
|---|---|---|
| `NED_TRANSCRIPT_MODEL` | `claude-sonnet-4-6` | Model for the transcript analysis (the daily news scan keeps Haiku) |
| `NED_TRANSCRIPT_MAX_TOKENS` | `6000` | Output budget for the analysis; raise it if a run fails with "cut off at max_tokens" |
| `NED_TRANSCRIPT_LLM_MAX_CHARS` | `400000` | Transcripts longer than this are analysed in ~150k-character parts |

To preview the PDF layout without a live run:
`python scripts/ned_sample_transcript_pdf.py` writes
`outputs/sample_transcript_digest.pdf` from a fixture.
