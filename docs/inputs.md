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

1. Fetch and transcribe the episode (podcast: try published RSS
   transcript first, fall back to Whisper; YouTube: read the caption
   track directly).
2. Summarise the transcript with the same Anthropic prompt Ned uses
   for the daily digest.
3. Email you the digest.
4. Append the entry to `docs/data/transcripts.json` and rebuild
   `docs/index.html`, then push. That triggers the Pages workflow,
   which redeploys the site with the new item in the **Transcripts**
   section.
