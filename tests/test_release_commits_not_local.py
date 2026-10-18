#!/usr/bin/env python3
"""Release commits are not local work (fleet-nightly-report, 2026-10-17 02:31).

Upstream cut v0.22.2 on 2026-10-15 and recorded it as an ancestor of main only on 2026-10-17
00:57 BST. Every stable node sat exactly on the v0.22.2 tag, yet the report counted
`origin/main..HEAD` = 11 and called all eleven nodes DIVERGED. The collector now also reports
LOCALONLY: commits reachable from HEAD that are in neither origin/main nor any release tag.

    python3 -m pytest tests/test_release_commits_not_local.py -q
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.fleet_report import CURRENT, DIVERGED, UNKNOWN, classify  # noqa: E402

ON_RELEASE = "→ Update channel: stable\n✓ Up to date with the latest release (stable v0.22.2)."


def rec(*, ahead: int, localonly: str | None) -> dict:
    git = {"HEAD": "d923a5eb48" + "0" * 30, "BRANCH": "HEAD", "SHALLOW": "false",
           "STASHES": "0", "DIRTY": "0", "ORIGINHEAD": "0612f3017b" + "0" * 30,
           "BEHINDORIGIN": "0", "AHEADORIGIN": str(ahead),
           "UPSTREAM": "fatal: HEAD does not point to a branch", "BEHIND": "", "AHEAD": ""}
    if localonly is not None:
        git["LOCALONLY"] = localonly
    return {"CLI": "hermes", "CHECK": ON_RELEASE,
            "GIT": "\n".join(f"{k}={v}" for k, v in git.items()), "DESKTOP": ""}


def test_replay_2026_10_17_release_not_yet_on_main_is_current():
    verdict, reasons = classify(rec(ahead=11, localonly="0"))
    assert verdict == CURRENT, reasons


def test_local_work_on_top_of_a_release_is_still_diverged():
    verdict, reasons = classify(rec(ahead=12, localonly="1"))
    assert verdict == DIVERGED
    assert reasons[0].startswith("1 local commit")


def test_an_older_collector_keeps_the_origin_main_arithmetic():
    verdict, _ = classify(rec(ahead=11, localonly=None))
    assert verdict == DIVERGED


def test_an_unreadable_local_count_is_unknown_never_current():
    verdict, _ = classify(rec(ahead=11, localonly="fatal: bad revision"))
    assert verdict == UNKNOWN


GIT = shutil.which("git")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.mark.skipif(GIT is None, reason="git not installed")
def test_the_collector_expression_counts_only_unreleased_local_commits(tmp_path):
    """The exact rev-list both probes run, against a real repository shaped like 2026-10-17."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")

    def commit(msg: str) -> None:
        (repo / "f").write_text(msg)
        _git(repo, "add", "f")
        _git(repo, "commit", "-q", "-m", msg)

    commit("main 1")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    for i in range(3):                        # release branch commits main has not recorded
        commit(f"release {i}")
    _git(repo, "tag", "v0.22.2")
    expr = ["rev-list", "--count", "HEAD", "--not", "origin/main", "--tags=v[0-9]*"]
    assert _git(repo, "rev-list", "--count", "origin/main..HEAD") == "3"
    assert _git(repo, *expr) == "0"           # sitting on the release: no local work
    commit("a real local patch")
    assert _git(repo, *expr) == "1"
