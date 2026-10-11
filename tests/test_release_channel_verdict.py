#!/usr/bin/env python3
"""Stable-release policy (owner decision 2026-10-10): classify() must take the node's own
`hermes update --check` verdict on a release channel instead of demanding HEAD == origin/main.

On the stable channel a node moves forward-only: it lands on the latest release as a DETACHED
HEAD (so @{u} does not resolve) or holds where it is when already past the release, so trailing
origin/main is the correct state. These contracts pin that: release-current is CURRENT whatever
main did, a newer release is BEHIND, local commits are still DIVERGED, and branch-channel nodes
keep the original git arithmetic.

The CHECK texts are the CLI's real output (mac-mini, 2026-10-11) and the exact
update_cmd_check.py verdict lines.

    python3 -m pytest tests/test_release_channel_verdict.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.fleet_report import (  # noqa: E402
    BEHIND, CURRENT, DIVERGED, UNKNOWN, UNTRACKED, classify, release_verdict,
)

HEAD = "d4d2ab7b5d104166e7e22a02cafd12f1fe456a31"
ORIGIN = "8f3c2e1d0b9a8f7e6d5c4b3a2f1e0d9c8b7a6f5e"

HELD = ("→ Update channel: stable\n"
        "✓ No newer release: stable v0.21.6; this checkout is newer and waits for the next "
        "release (`hermes update --set-channel main` follows every commit).")
ON_RELEASE = "→ Update channel: stable\n✓ Up to date with the latest release (stable v0.21.7)."
RELEASE_WAITING = ("→ Update channel: stable\n→ Selected release available: stable v0.21.7\n"
                   "  Run `hermes update` to install it.")
BRANCH_BEHIND = "⚕ Update available: 12 commits behind origin/main.\n  Run 'hermes update' to install."


def rec(check: str, *, upstream: str | None = ORIGIN, behind: int = 12, ahead: int = 0,
        branch: str = "main", stashes: int = 0, dirty: int = 0) -> dict:
    git = {
        "HEAD": HEAD, "BRANCH": branch, "SHALLOW": "false",
        "STASHES": str(stashes), "DIRTY": str(dirty),
        "ORIGINHEAD": ORIGIN, "BEHINDORIGIN": str(behind), "AHEADORIGIN": str(ahead),
    }
    if upstream:
        git.update(UPSTREAM=upstream, BEHIND=str(behind), AHEAD=str(ahead), MERGEBASE=HEAD)
    else:  # detached HEAD: `git rev-parse @{u}` fails, so the probe line carries no hash
        git.update(UPSTREAM="fatal: HEAD does not point to a branch", BEHIND="", AHEAD="")
    return {"CLI": "C:\\Users\\u\\AppData\\Local\\hermes\\hermes-agent\\.hermes\\bin\\hermes.cmd",
            "CHECK": check, "GIT": "\n".join(f"{k}={v}" for k, v in git.items()), "DESKTOP": ""}


def test_verdict_parser_reads_the_cli_text():
    assert release_verdict(HELD) == "current"
    assert release_verdict(ON_RELEASE) == "current"
    assert release_verdict(RELEASE_WAITING) == "available"
    assert release_verdict(BRANCH_BEHIND) is None
    assert release_verdict("") is None


def test_holding_past_the_release_is_current_while_main_moves_on():
    verdict, reasons = classify(rec(HELD, behind=12))
    assert verdict == CURRENT
    assert "12 commit(s) behind main" in reasons[0]


def test_detached_at_the_release_is_current_not_untracked():
    verdict, _ = classify(rec(ON_RELEASE, upstream=None, branch="HEAD", behind=40))
    assert verdict == CURRENT


def test_a_waiting_release_is_behind():
    verdict, _ = classify(rec(RELEASE_WAITING, upstream=None, branch="HEAD", behind=3))
    assert verdict == BEHIND


def test_local_commits_stay_diverged_on_a_release_channel():
    verdict, _ = classify(rec(HELD, ahead=2))
    assert verdict == DIVERGED


def test_unreadable_origin_arithmetic_is_unknown_never_current():
    r = rec(HELD)
    r["GIT"] = r["GIT"].replace("AHEADORIGIN=0", "AHEADORIGIN=fatal: bad revision")
    assert classify(r)[0] == UNKNOWN


def test_falsify_branch_channel_keeps_git_arithmetic():
    # The release branch must not swallow branch-channel nodes: a main-channel node behind
    # origin/main is still BEHIND, and a detached one with no release verdict stays UNTRACKED.
    assert classify(rec(BRANCH_BEHIND, behind=12))[0] == BEHIND
    assert classify(rec(BRANCH_BEHIND, upstream=None, branch="HEAD", behind=12))[0] == UNTRACKED
