#!/usr/bin/env bash
# ned-cli.sh — trigger Ned's transcript workflows from the terminal.
#
# Usage:
#     ned pod <podcast-url>          # podcast (mp3, Apple, RSS)
#     ned yt  <youtube-url>          # YouTube episode
#
# Installation:
#     Append the following to your ~/.zshrc or ~/.bashrc, adjusting the
#     REPO path to where this repo is checked out:
#
#         export NED_CLI_REPO="$HOME/code/Reporting-Agent"
#         source "$NED_CLI_REPO/scripts/ned-cli.sh"
#
# Prerequisites:
#     * gh CLI installed and authenticated: `gh auth status`
#     * You're a collaborator on JohnnyM77/Reporting-Agent (you are).
#
# The gh CLI POSTs to the same workflow_dispatch endpoint the "Run workflow"
# button uses — no PAT or extra secret to manage, since gh reuses whatever
# credentials `gh auth login` set up.

_ned_owner="JohnnyM77"
_ned_repo="Reporting-Agent"
_ned_ref="main"

_ned_need_gh() {
    if ! command -v gh >/dev/null 2>&1; then
        echo "ned: gh CLI is not installed. See https://cli.github.com/" >&2
        return 1
    fi
    if ! gh auth status >/dev/null 2>&1; then
        echo "ned: gh is not authenticated. Run: gh auth login" >&2
        return 1
    fi
}

ned() {
    local sub="$1"
    shift || true
    local url="$1"

    if [ -z "$sub" ] || [ -z "$url" ]; then
        cat >&2 <<EOF
Usage:
    ned pod <podcast-url>    # dispatch Ned Podcast Transcript Fetcher
    ned yt  <youtube-url>    # dispatch Ned Transcript Fetcher (YouTube)

Both workflows run on GitHub Actions; you'll get an email when they finish.
See "Recent runs":
    gh run list --repo $_ned_owner/$_ned_repo --workflow ned_podcast.yml -L 5
    gh run list --repo $_ned_owner/$_ned_repo --workflow ned_transcript.yml -L 5
EOF
        return 2
    fi

    _ned_need_gh || return $?

    case "$sub" in
        pod|podcast)
            gh workflow run ned_podcast.yml \
                --repo "$_ned_owner/$_ned_repo" \
                --ref "$_ned_ref" \
                -f podcast_url="$url"
            ;;
        yt|youtube)
            gh workflow run ned_transcript.yml \
                --repo "$_ned_owner/$_ned_repo" \
                --ref "$_ned_ref" \
                -f youtube_url="$url"
            ;;
        *)
            echo "ned: unknown subcommand '$sub' (expected 'pod' or 'yt')" >&2
            return 2
            ;;
    esac

    local rc=$?
    if [ $rc -eq 0 ]; then
        echo "ned: dispatched. Watch it here:"
        echo "  https://github.com/$_ned_owner/$_ned_repo/actions"
    fi
    return $rc
}
