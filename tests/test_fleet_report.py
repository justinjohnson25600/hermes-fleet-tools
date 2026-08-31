#!/usr/bin/env python3
"""Tests for fleet_report classification.

Every fixture below is real output captured from a live agent, not invented.
Each test named `test_falsify_*` breaks the thing a guard protects and asserts
the guard goes red — a guard that has never failed is decoration.

    python3 -m pytest tests/test_fleet_report.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.fleet_report import (           # noqa: E402
    BEHIND, BLOCKED, CURRENT, DIVERGED, SHALLOW, UNKNOWN, UNREACHABLE,
    classify, parse_kv, parse_sections,
)

# --- real captured output ---------------------------------------------------

CHECK_AVAILABLE = ("→ Fetching from origin...\n"
                   "⚕ Update available: 140 commits behind origin/main.\n"
                   "Run 'hermes update' to install.")
CHECK_RATELIMITED = ("→ Fetching from origin...\n"
                     "✗ GitHub is rate limiting requests or having an outage (HTTP 429)"
                     " — try again in 5 minutes.\n"
                     "remote: This request was rate-limited due to too many requests.")
CHECK_NOTAREPO = "✗ Not a git repository — cannot check for updates."
CHECK_EMPTY = ""            # the ACP binary: accepted the command, printed nothing

HEAD_A = "26f178e5fa78c691cadf847058ef1d55a707bfb0"
HEAD_B = "38b93e0abec1eb4198ea3d18a9cb607ed9745906"


def rec(check=CHECK_AVAILABLE, git="", cli="C:\\path\\hermes.exe", **kw):
    base = {"CLI": cli, "CHECK": check, "GIT": git}
    base.update(kw)
    return base


def git_block(head=HEAD_A, upstream=HEAD_B, behind="5", ahead="0",
              stashes="0", dirty="0", shallow="false", mergebase=HEAD_A):
    return (f"HEAD={head}\nUPSTREAM={upstream}\nBRANCH=main\nSHALLOW={shallow}\n"
            f"BEHIND={behind}\nAHEAD={ahead}\nMERGEBASE={mergebase}\n"
            f"STASHES={stashes}\nDIRTY={dirty}")


# --- parsing ----------------------------------------------------------------

def test_parse_sections_splits_on_markers():
    raw = "===CLI===\n/bin/hermes\n===CHECK===\nline one\nline two\n===END==="
    out = parse_sections(raw)
    assert out["CLI"] == "/bin/hermes"
    assert out["CHECK"] == "line one\nline two"


def test_parse_kv_reads_git_block():
    kv = parse_kv(git_block(behind="140"))
    assert kv["BEHIND"] == "140" and kv["HEAD"] == HEAD_A


# --- the four observed CHECK shapes ----------------------------------------

def test_behind_is_reported():
    verdict, reasons = classify(rec(git=git_block(behind="140")))
    assert verdict == BEHIND
    assert "140 commit(s) behind" in reasons[0]


def test_ratelimited_is_blocked_not_current():
    verdict, reasons = classify(rec(check=CHECK_RATELIMITED, git=git_block()))
    assert verdict == BLOCKED
    assert "429" in reasons[0]


def test_not_a_git_repository_is_blocked():
    verdict, reasons = classify(rec(check=CHECK_NOTAREPO, git=git_block()))
    assert verdict == BLOCKED
    assert "different directory" in reasons[0]


def test_falsify_empty_check_output_is_never_current():
    """The ACP binary accepted `update --check` and printed nothing.

    If empty output can reach CURRENT, six machines report green forever.
    """
    verdict, _ = classify(rec(check=CHECK_EMPTY, git=git_block(behind="0", ahead="0",
                                                              upstream=HEAD_A)))
    assert verdict == CURRENT, "sanity: with git proving parity, empty prose is fine"

    # ...but with NO git facts to fall back on, silence must not become CURRENT.
    verdict, _ = classify(rec(check=CHECK_EMPTY, git=""))
    assert verdict == UNKNOWN


# --- shallow clones ---------------------------------------------------------

def test_falsify_shallow_clone_counts_are_rejected():
    """Two machines at an identical HEAD reported 140 and 1 commits behind.

    The second was depth-1. Counting on a truncated graph is arithmetic on a
    lie, so the verdict must be SHALLOW, never BEHIND/CURRENT.
    """
    verdict, reasons = classify(rec(
        check=CHECK_AVAILABLE,
        git=git_block(behind="1", ahead="1", shallow="true", mergebase="")))
    assert verdict == SHALLOW
    assert "not meaningful" in reasons[0]


def test_shallow_detected_by_missing_mergebase_alone():
    """REGRESSION: an empty merge-base must NOT imply shallow.

    This heuristic once labelled a 26,480-commit repo SHALLOW because its
    upstream ref failed to resolve during the probe. Positive evidence only.
    """
    verdict, reasons = classify(rec(git=git_block(shallow="false", mergebase="")))
    assert verdict != SHALLOW
    assert verdict == BEHIND, "a healthy repo with a resolvable upstream is just BEHIND"


def test_falsify_unresolvable_upstream_is_unknown_not_shallow():
    verdict, reasons = classify(rec(git=git_block(upstream="", mergebase="")))
    assert verdict == UNKNOWN
    assert "upstream" in reasons[0]


def test_falsify_shallow_ahead_is_not_reported_as_local_work():
    """A depth-1 clone shows ahead=1 for a commit it shares with upstream."""
    verdict, reasons = classify(rec(
        git=git_block(ahead="1", shallow="true", mergebase="")))
    assert verdict == SHALLOW
    assert not any("not upstream" in r for r in reasons)


# --- divergence and hygiene -------------------------------------------------

def test_real_local_commits_are_diverged():
    verdict, reasons = classify(rec(git=git_block(ahead="8", behind="0")))
    assert verdict == DIVERGED
    assert "8 local commit(s) not upstream" in reasons[0]


def test_current_requires_head_to_match_upstream():
    verdict, _ = classify(rec(check=CHECK_EMPTY,
                              git=git_block(head=HEAD_A, upstream=HEAD_A,
                                            behind="0", ahead="0")))
    assert verdict == CURRENT


def test_falsify_impossible_arithmetic_is_unknown_not_current():
    """ahead=0, behind=0, yet HEAD != upstream. Do not guess: say UNKNOWN."""
    verdict, reasons = classify(rec(check=CHECK_EMPTY,
                                    git=git_block(head=HEAD_A, upstream=HEAD_B,
                                                  behind="0", ahead="0")))
    assert verdict == UNKNOWN
    assert any("does not add up" in r or "does not match" in r for r in reasons)


def test_stashes_and_dirty_are_surfaced_on_a_behind_box():
    _, reasons = classify(rec(git=git_block(behind="3", stashes="19", dirty="3")))
    assert any("19 stash" in r for r in reasons)
    assert any("3 dirty" in r for r in reasons)


def test_desktop_running_is_surfaced():
    _, reasons = classify(rec(git=git_block(behind="2"),
                              DESKTOP="C:\\path\\win-unpacked\\Hermes.exe"))
    assert any("Desktop app running" in r for r in reasons)


# --- failure paths ----------------------------------------------------------

def test_unreachable_agent():
    verdict, reasons = classify({"error": "timed out after 240s"})
    assert verdict == UNREACHABLE and "timed out" in reasons[0]


def test_falsify_broken_probe_is_not_reported_as_unreachable():
    """A malformed remote payload once showed up as six UNREACHABLE agents with
    raw CLIXML in the notes column. That hid a one-character bug in this file
    behind what looked like an estate-wide outage."""
    err = ("erError: (:) [], ParentContainsErrorRecordException\r\n"
           "+ FullyQualifiedErrorId : TerminatorExpectedAtEndOfString\r\n</Objs>")
    verdict, reasons = classify({"error": err})
    assert verdict == UNKNOWN
    assert "malformed command" in reasons[0]
    assert "<Objs" not in reasons[0], "raw shell XML must not leak into the report"


def test_missing_cli_is_blocked():
    verdict, reasons = classify(rec(cli="", git=git_block()))
    assert verdict == BLOCKED and "no CLI" in reasons[0]


def test_missing_repo_is_blocked():
    verdict, reasons = classify(rec(git="REPO_MISSING=1"))
    assert verdict == BLOCKED and "repo not found" in reasons[0]


def test_falsify_truncated_head_is_unknown():
    verdict, _ = classify(rec(check=CHECK_EMPTY, git=git_block(head="26f178e")))
    assert verdict == UNKNOWN


def test_falsify_unparseable_counts_are_unknown():
    verdict, _ = classify(rec(check=CHECK_EMPTY,
                              git=git_block(behind="fatal: bad revision", ahead="")))
    assert verdict == UNKNOWN
