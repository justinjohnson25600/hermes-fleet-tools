#!/usr/bin/env python3
"""Fewer dev-pair checks than a peer means skipped checks ONLY when both ran the same suite.

A box whose dev-pair copy is held (local edits) keeps older code, so it runs an older suite with
fewer checks. Comparing it with peers on the new suite reported "checks are being skipped" — a
false diagnosis (trigkey-g4, 2026-10-20: 590 checks on 1.2.5's suite vs 605 on 1.2.6's). Counts
are now compared within the same platform AND the same suite file; a different suite is reported
as a note naming both versions, and same-suite shortfalls still fail.

    python3 -m pytest tests/test_deploy_check_counts.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.deploy import check_count_findings  # noqa: E402


def row(name, checks, platform="windows", suite="new", version="1.2.8"):
    return {"name": name, "checks": checks, "platform": platform, "suite": suite, "version": version}


def test_same_suite_fewer_checks_is_a_skip_and_fails():
    warns, notes = check_count_findings([row("a", 605), row("b", 605), row("c", 590)])
    assert [n for n, _ in warns] == ["c"]
    assert "590 checks vs 605" in warns[0][1] and "skipped" in warns[0][1]


def test_held_box_on_an_older_suite_is_a_note_not_a_skip():
    warns, notes = check_count_findings([row("a", 605), row("b", 605),
                                         row("held", 590, suite="old", version="1.2.5")])
    assert warns == []
    assert len(notes) == 1
    assert "held" in notes[0] and "1.2.5" in notes[0] and "590" in notes[0] and "1.2.8" in notes[0]


def test_a_skip_inside_the_older_suite_group_still_fails():
    warns, _ = check_count_findings([row("a", 605), row("h1", 590, suite="old", version="1.2.5"),
                                     row("h2", 580, suite="old", version="1.2.5")])
    assert [n for n, _ in warns] == ["h2"]


def test_rows_without_a_suite_hash_compare_as_before():
    warns, _ = check_count_findings([row("a", 605, suite=None), row("b", 590, suite=None)])
    assert [n for n, _ in warns] == ["b"]


def test_cross_platform_difference_is_a_note_only():
    warns, notes = check_count_findings([row("w", 605), row("m", 601, platform="macos")])
    assert warns == []
    assert any("differ by platform" in n for n in notes)
