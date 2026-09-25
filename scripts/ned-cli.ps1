# ned-cli.ps1 — trigger Ned's transcript workflows from PowerShell.
#
# Usage:
#     ned pod <podcast-url>          # podcast (mp3, Apple, RSS)
#     ned yt  <youtube-url>          # YouTube episode
#
# Installation:
#     Add these two lines to your PowerShell profile ($PROFILE), adjusting
#     the path to where this repo is checked out:
#
#         $env:NED_CLI_REPO = "C:\path\to\Reporting-Agent"
#         . "$env:NED_CLI_REPO\scripts\ned-cli.ps1"
#
#     Run `notepad $PROFILE` to open (or create) the profile file.
#
# Prerequisites:
#     * gh CLI installed and authenticated: `gh auth status`
#       (winget install --id GitHub.cli, then `gh auth login`)
#     * You're a collaborator on JohnnyM77/Reporting-Agent (you are).

$script:NedOwner = "JohnnyM77"
$script:NedRepo  = "Reporting-Agent"
$script:NedRef   = "main"

function ned {
    [CmdletBinding()]
    param(
        [Parameter(Position=0)][string] $Subcommand,
        [Parameter(Position=1)][string] $Url
    )

    if (-not $Subcommand -or -not $Url) {
        Write-Host @"
Usage:
    ned pod <podcast-url>    # dispatch Ned Podcast Transcript Fetcher
    ned yt  <youtube-url>    # dispatch Ned Transcript Fetcher (YouTube)

Both workflows run on GitHub Actions; you'll get an email when they finish.
Recent runs:
    gh run list --repo $script:NedOwner/$script:NedRepo --workflow ned_podcast.yml -L 5
    gh run list --repo $script:NedOwner/$script:NedRepo --workflow ned_transcript.yml -L 5
"@
        return
    }

    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        Write-Error "ned: gh CLI is not installed. See https://cli.github.com/"
        return
    }
    gh auth status *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "ned: gh is not authenticated. Run: gh auth login"
        return
    }

    switch ($Subcommand.ToLower()) {
        { $_ -in "pod", "podcast" } {
            gh workflow run ned_podcast.yml `
                --repo "$script:NedOwner/$script:NedRepo" `
                --ref  "$script:NedRef" `
                -f podcast_url="$Url"
        }
        { $_ -in "yt", "youtube" } {
            gh workflow run ned_transcript.yml `
                --repo "$script:NedOwner/$script:NedRepo" `
                --ref  "$script:NedRef" `
                -f youtube_url="$Url"
        }
        default {
            Write-Error "ned: unknown subcommand '$Subcommand' (expected 'pod' or 'yt')"
            return
        }
    }

    if ($LASTEXITCODE -eq 0) {
        Write-Host "ned: dispatched. Watch it here:"
        Write-Host "  https://github.com/$script:NedOwner/$script:NedRepo/actions"
    }
}
