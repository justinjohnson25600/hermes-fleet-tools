#!/usr/bin/env python3
"""Tests for fleet_report classification.

Every fixture below is real output captured from a live agent, not invented.
Each test named `test_falsify_*` breaks the thing a guard protects and asserts
the guard goes red — a guard that has never failed is decoration.

    python3 -m pytest tests/test_fleet_report.py -q
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools import fleet_report as fr           # noqa: E402
from fleettools.fleet_report import (           # noqa: E402
    BEHIND, BLOCKED, CURRENT, DIVERGED, SHALLOW, UNKNOWN, UNREACHABLE, UNTRACKED,
    classify, parse_kv, parse_sections,
)


# ---------------------------------------------------------------------------
# Staleness guard - the 2026-09-03 incident where a 5.6h-old sweep was served
# as current. The reporter must carry its own age on its face. The sweep
# stamps its ts INSIDE fleet-verdicts.json, and the consumer ages exactly the
# file it serves (embedded ts first, file mtime fallback for legacy stores).
# Three behaviours:
#   1. old sweep   -> banner printed AND the table still printed
#   2. fresh sweep -> NO banner (a guard that cries wolf on fresh data is
#      worse than none)
#   3. missing/malformed store -> no crash, no banner (staleness is
#      unknowable, not false; different failure, different owner)
# ---------------------------------------------------------------------------


def _write_verdicts(state_dir, ts_iso=None, results=None, mtime=None):
    """Current store shape: the sweep stamps its ts INSIDE the store."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "fleet-verdicts.json"
    path.write_text(
        json.dumps({"ts": ts_iso or datetime.now(timezone.utc).isoformat(),
                    "results": results or [HEALTHY_RESULT]}, indent=2),
        encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _snapshot_state(state_dir):
    """Sorted (relative path, bytes) of the WHOLE state dir — the read-only proof.

    rglob + files-only + relative paths: a real state dir can grow subdirs
    (round 2: iterdir() hit IsADirectoryError the moment one existed), and a
    proof that dies on the shape it is proving read-only proves nothing.
    """
    root = Path(state_dir)
    return sorted(
        (str(p.relative_to(root)), p.read_bytes())
        for p in root.rglob("*") if p.is_file()
    )


def test_old_sweep_prints_banner_and_table(tmp_path, capsys):
    old = datetime.now(timezone.utc) - timedelta(hours=6)
    _write_verdicts(tmp_path, ts_iso=old.isoformat())
    fired = fr.emit_staleness_banner(tmp_path, max_age_mins=180)
    out = capsys.readouterr().out
    assert fired is True
    assert "STALE DATA" in out
    assert "6" in out


def test_fresh_sweep_prints_no_banner(tmp_path, capsys):
    now = datetime.now(timezone.utc)
    _write_verdicts(tmp_path, ts_iso=now.isoformat())
    fired = fr.emit_staleness_banner(tmp_path, max_age_mins=180)
    assert fired is False
    assert "STALE DATA" not in capsys.readouterr().out


def test_missing_store_no_crash_no_banner(tmp_path, capsys):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fired = fr.emit_staleness_banner(tmp_path, max_age_mins=180)
    assert fired is False
    assert "STALE DATA" not in capsys.readouterr().out


def test_malformed_ts_fresh_mtime_no_crash_no_banner(tmp_path, capsys):
    """Corrupt embedded ts, fresh file: mtime fallback says fresh, so serve
    quietly. (Round 2 changed this contract: a corrupt ts no longer means
    unknowable age — see test_no_sweep_corrupt_ts_with_old_mtime_banners.)"""
    _write_verdicts(tmp_path, ts_iso="not-a-timestamp")
    fired = fr.emit_staleness_banner(tmp_path, max_age_mins=180)
    assert fired is False
    assert "STALE DATA" not in capsys.readouterr().out


def test_legacy_bare_list_store_ages_by_file_mtime(tmp_path, capsys):
    """Pre-fix state dirs hold a bare list with no embedded ts: their age
    falls back to file mtime, so a day-old legacy store still banners."""
    day_old_mtime = datetime.now().timestamp() - 24 * 3600
    (tmp_path / "fleet-verdicts.json").write_text(
        json.dumps([HEALTHY_RESULT]), encoding="utf-8")
    os.utime(tmp_path / "fleet-verdicts.json", (day_old_mtime, day_old_mtime))
    fired = fr.emit_staleness_banner(tmp_path, max_age_mins=180)
    out = capsys.readouterr().out
    assert fired is True
    assert "STALE DATA" in out and "24" in out


# ---------------------------------------------------------------------------
# WHERE the banner lives. Observed in production 09-05 and 09-06: the banner
# was producer-side, called at the top of main() BEFORE the sweep -- so it
# always read the PREVIOUS run's heartbeat (~24h old in the nightly cron)
# and fired on fresh data every night. A guard that cries wolf nightly is
# worse than none. But post-sweep placement is not the fix either: main()
# unconditionally writes a fresh heartbeat after the sweep (probe() never
# raises), so a post-sweep banner would be unreachable forever -- guard
# deletion. The banner is consumer-side: --no-sweep is the read-only
# interactive view of the state dir, and it labels what it serves.
# ---------------------------------------------------------------------------

HEALTHY_RESULT = {
    "name": "box-a", "verdict": "CURRENT", "reasons": [],
    "head": "26f178e5fa78", "upstream": "26f178e5fa78",
    "behind": 0, "ahead": 0, "stashes": 0, "dirty": 0,
    "shallow": False, "desktop_running": False, "version": "v0.0.0",
    "carried_claim": None, "check_raw": "", "error": None,
}


def _write_state(tmp_path, ts_iso, results=None, with_heartbeat=True):
    """A state dir as the nightly leaves it: stamped verdicts (+ heartbeat).

    with_heartbeat=False reproduces the BLOCKER: a state dir whose heartbeat
    was lost or never written while its verdicts are a day old.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "fleet-verdicts.json").write_text(
        json.dumps({"ts": ts_iso,
                    "results": results or [HEALTHY_RESULT]}, indent=2),
        encoding="utf-8")
    if with_heartbeat:
        (state / "fleet-heartbeat.json").write_text(
            json.dumps({"ts": ts_iso, "agents_total": 1,
                        "verdicts": {"box-a": "CURRENT"}}, indent=2),
            encoding="utf-8")
    return state


def _write_roster(tmp_path):
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps({"agents": [
        {"name": "box-a", "host": "local", "platform": "macos",
         "enabled": True, "cli": "/usr/bin/true"}]}), encoding="utf-8")
    return roster


def _run_main(argv):
    with mock.patch.object(sys, "argv", argv):
        return fr.main()


def _no_sweep_guard():
    """During --no-sweep tests, any probe call is a contract violation."""
    return mock.patch.object(fr, "probe",
                             side_effect=AssertionError("probe ran under --no-sweep"))


def test_sweep_never_banners_even_on_day_old_previous_verdicts(
        tmp_path, capsys):
    """THE NIGHTLY DEFECT (09-05, 09-06): the sweep path read the previous
    run's ~24h-old state and stamped STALE DATA on data it had swept
    moments earlier. The sweep stamps its own fresh ts inside the store, so
    its output is fresh by construction and must NEVER carry the banner."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old)
    roster = _write_roster(tmp_path)
    with mock.patch.object(fr, "probe", return_value=dict(HEALTHY_RESULT)):
        rc = _run_main(["fleet_report.py", "--roster", str(roster),
                        "--state", str(state)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "STALE DATA" not in out, (
        "cry-wolf: the sweep path bannered over its own fresh output")
    # and the sweep proved liveness: fresh ts stamped inside the store
    store = json.loads((state / "fleet-verdicts.json").read_text())
    age_h = (datetime.now(timezone.utc)
             - datetime.fromisoformat(store["ts"])).total_seconds() / 3600
    assert age_h < 1 / 60
    assert store["results"][0]["name"] == "box-a"


def test_no_sweep_on_day_old_state_dir_prints_banner(tmp_path, capsys):
    """--no-sweep is the banner's only home: an interactive read of a stale
    state dir must carry its age (the 2026-09-03 incident class)."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old)
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2, "stale text-mode serve must signal via rc (README rc table)"
    assert "STALE DATA" in out and "24" in out
    assert "box-a" in out, "the stored report itself is still printed"
    # read-only: the WHOLE state dir is byte-identical, and no probe ran
    assert _snapshot_state(state) == before


def test_no_sweep_missing_heartbeat_with_day_old_verdicts_banners(
        tmp_path, capsys):
    """THE BLOCKER (review round on ba57e73): the banner used to age the
    heartbeat while serving the verdicts. Heartbeat missing + day-old
    verdicts -> clean table, rc=0, NO banner: the exact 2026-09-03 incident
    class, inside the mode built to prevent it. The consumer must age what
    it serves — the ts stamped inside fleet-verdicts.json."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old, with_heartbeat=False)
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert "STALE DATA" in out and "24" in out, (
        "missing heartbeat must not let day-old verdicts pass as fresh")
    assert "box-a" in out
    assert _snapshot_state(state) == before


def test_no_sweep_legacy_bare_list_with_day_old_mtime_banners(
        tmp_path, capsys):
    """Legacy state dirs (bare-list store, no embedded ts, no heartbeat)
    still get labelled: age falls back to file mtime."""
    day_old_mtime = datetime.now().timestamp() - 24 * 3600
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "fleet-verdicts.json").write_text(
        json.dumps([HEALTHY_RESULT], indent=2), encoding="utf-8")
    os.utime(state / "fleet-verdicts.json", (day_old_mtime, day_old_mtime))
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2, "stale serve signals rc=2 in text mode too (README table)"
    assert "STALE DATA" in out and "24" in out
    assert _snapshot_state(state) == before


def test_no_sweep_fresh_heartbeat_but_day_old_verdicts_still_banners(
        tmp_path, capsys):
    """A fresh heartbeat over day-old verdicts (crash between the two
    writes, pre-fix) is a lie the consumer must not believe: the embedded
    verdicts ts is authoritative, the heartbeat is not consulted."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    state = _write_state(tmp_path, day_old)
    (state / "fleet-heartbeat.json").write_text(
        json.dumps({"ts": fresh}), encoding="utf-8")
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2, "stale serve signals rc=2 in text mode too (README table)"
    assert "STALE DATA" in out and "24" in out, (
        "a fresh heartbeat must not vouch for day-old verdicts")
    assert _snapshot_state(state) == before


def test_no_sweep_on_fresh_state_dir_prints_no_banner(tmp_path, capsys):
    state = _write_state(tmp_path, datetime.now(timezone.utc).isoformat())
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    assert rc == 0
    assert "STALE DATA" not in capsys.readouterr().out
    assert _snapshot_state(state) == before


def test_no_sweep_missing_state_dir_no_crash_no_banner(tmp_path, capsys):
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep",
                        "--state", str(tmp_path / "nothere")])
    assert "STALE DATA" not in capsys.readouterr().out
    assert rc == 1, "nothing to serve must be a visible failure, not silence"


def test_no_sweep_corrupt_ts_fresh_mtime_no_crash_no_banner(tmp_path, capsys):
    """A corrupt embedded ts with a FRESH mtime: age is unknowable-from-ts but
    the file itself is new, so mtime fallback says fresh — serve, no banner,
    no crash. (The store proved readable; only the stamp is garbage.)"""
    state = _write_state(tmp_path, "not-a-timestamp")
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "STALE DATA" not in out and "box-a" in out
    assert _snapshot_state(state) == before


def test_no_sweep_corrupt_ts_with_old_mtime_banners(tmp_path, capsys):
    """ROUND 2 MAJOR: ts='not-a-timestamp' + 24h-old mtime used to exit 0
    with no banner — fromisoformat raised inside the broad except, so the
    mtime fallback was unreachable and a 24h-old corrupt store served as
    fresh. A readable store with a garbage stamp must age by mtime."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    vpath = state / "fleet-verdicts.json"
    vpath.write_text(json.dumps(
        {"ts": "not-a-timestamp", "results": [HEALTHY_RESULT]}), encoding="utf-8")
    day_old_mtime = datetime.now().timestamp() - 24 * 3600
    os.utime(vpath, (day_old_mtime, day_old_mtime))
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2, "corrupt ts must fall through to mtime, not fail open"
    assert "STALE DATA" in out and "24" in out
    assert "box-a" in out, "the stored report itself is still served"
    assert _snapshot_state(state) == before


def test_verdicts_age_h_corrupt_ts_falls_back_to_mtime(tmp_path):
    """Unit-level: the mtime fallback must be reachable when fromisoformat
    raises, instead of the raise being swallowed into None (fail-open)."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    vpath = state / "fleet-verdicts.json"
    vpath.write_text(json.dumps(
        {"ts": "not-a-timestamp", "results": [HEALTHY_RESULT]}), encoding="utf-8")
    day_old_mtime = datetime.now().timestamp() - 24 * 3600
    os.utime(vpath, (day_old_mtime, day_old_mtime))
    age = fr.verdicts_age_h(state)
    assert age is not None and 23 < age < 25


def test_no_sweep_bare_string_list_is_a_one_line_error_not_a_traceback(
        tmp_path, capsys):
    """Crash shape 1: ["a","b"] is a list, so the old one-level check passed
    and render crashed AFTER the banner. Must be a clean stderr message."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "fleet-verdicts.json").write_text(json.dumps(["a", "b"]),
                                               encoding="utf-8")
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "fleet_report: stored verdicts unreadable" in err
    assert "Traceback" not in err


def test_no_sweep_missing_name_field_is_a_one_line_error_not_a_traceback(
        tmp_path, capsys):
    """Crash shape 2: [{"verdict":"CURRENT"}] passes the old check and dies
    in render on r['name']. Must be a clean stderr message."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "fleet-verdicts.json").write_text(
        json.dumps([{"verdict": "CURRENT"}]), encoding="utf-8")
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "fleet_report: stored verdicts unreadable" in err
    assert "Traceback" not in err


def test_no_sweep_minimal_record_missing_render_keys_is_a_one_line_error(
        tmp_path, capsys):
    """ROUND 2 MAJOR: a record with exactly {name, verdict} passed the old
    validator (it only checked those two) and then KeyError'd on 'head' in
    render, AFTER the banner. Validation must cover the FULL key set render
    consumes — via RENDER_REQUIRED_KEYS, shared with the renderer."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "fleet-verdicts.json").write_text(
        json.dumps([{"name": "box-a", "verdict": "CURRENT"}]), encoding="utf-8")
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "fleet_report: stored verdicts unreadable" in err
    assert "head" in err, "the error must name the missing render key"
    assert "Traceback" not in err


def test_no_sweep_record_with_non_string_name_is_rejected(tmp_path, capsys):
    """A numeric name is a malformed record, not a render-time crash."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "fleet-verdicts.json").write_text(
        json.dumps([dict(HEALTHY_RESULT, name=42)]), encoding="utf-8")
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "fleet_report: stored verdicts unreadable" in err
    assert "Traceback" not in err


def test_render_required_keys_match_validator_constant():
    """Drift-proofing: the renderer's table columns and the validator's key
    set are welded together at import time (see the module assert). This test
    pins the contract from the consumer side so a regression in either half
    is caught by name."""
    from fleettools.fleet_report import RENDER_REQUIRED_KEYS
    assert set(RENDER_REQUIRED_KEYS) >= {
        "name", "verdict", "head", "behind", "ahead", "stashes", "dirty"}


def test_no_sweep_stale_text_mode_exits_2(tmp_path, capsys):
    """ROUND 2 MAJOR: only --json returned 2 while the README exit-code table
    is mode-unqualified. A bannered text-mode serve must also exit 2."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old)
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "STALE DATA" in out
    assert _snapshot_state(state) == before


def test_no_sweep_fresh_text_mode_exits_0(tmp_path, capsys):
    state = _write_state(tmp_path, datetime.now(timezone.utc).isoformat())
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    assert rc == 0
    assert "STALE DATA" not in capsys.readouterr().out


def test_no_sweep_serves_a_single_consistent_read_not_a_racing_re_read(
        tmp_path, capsys):
    """ROUND 2 MAJOR (TOCTOU): serve_stored_state loaded the store, then
    emit_staleness_banner re-read the same file. A sweep landing between the
    two reads replaced the store mid-serve, so the banner aged the NEW file
    while the table served the OLD results — stale data, no banner. The serve
    path must read ONCE and age what it captured."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old)
    fresh_results = [dict(HEALTHY_RESULT, head="ffffffffffff")]

    real_load = fr.load_stored_verdicts
    reads = {"n": 0}

    def racing_load(path):
        reads["n"] += 1
        res = real_load(path)
        if reads["n"] == 1:
            # a sweep lands between serve's read and the banner's re-read
            fresh = datetime.now(timezone.utc).isoformat()
            Path(path).write_text(
                json.dumps({"ts": fresh, "results": fresh_results}), encoding="utf-8")
        return res

    with _no_sweep_guard(), \
         mock.patch.object(fr, "load_stored_verdicts", side_effect=racing_load):
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert reads["n"] == 1, "the serve path must read the store exactly once"
    assert "STALE DATA" in out, (
        "the day-old results being served must be bannered even though a "
        "sweep freshened the file mid-serve")
    assert rc == 2
    # the served table is still the captured (old) read, not the racer's
    assert "26f178e5fa78" in out


def test_no_sweep_json_keeps_changed_and_previous_verdict_keys(
        tmp_path, capsys):
    """ROUND 2 MINOR: popping 'changed'/'previous_verdict' silently changed
    the --json schema between a sweep and a replay. Replay resets them to
    inert values (False / None) but the KEYS must survive."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    stored = [dict(HEALTHY_RESULT, changed=True, previous_verdict="CURRENT")]
    state = _write_state(tmp_path, day_old, results=stored)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--json",
                        "--state", str(state)])
    captured = capsys.readouterr()
    assert rc == 2
    payload = json.loads(captured.out)
    assert payload[0]["changed"] is False
    assert payload[0]["previous_verdict"] is None


def test_snapshot_state_survives_subdirs_in_the_state_dir(tmp_path, capsys):
    """ROUND 2 MINOR: _snapshot_state used iterdir(), which raises
    IsADirectoryError the moment a real state dir contains a subdir — and the
    read-only proof must hold for exactly those dirs."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old)
    nested = state / "archive"
    nested.mkdir()
    (nested / "2026-09-01.json").write_text("{}", encoding="utf-8")
    before = _snapshot_state(state)          # must not raise
    assert ("archive/2026-09-01.json", b"{}") in before
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2 and "STALE DATA" in out
    assert _snapshot_state(state) == before, "read-only proof covers subdirs too"


def test_atomic_write_preserves_0644_store_permissions(tmp_path):
    """ROUND 2 MINOR: mkstemp creates the temp file 0600, silently tightening
    the store's historical 0644 for every secondary reader."""
    from fleettools.fleet_report import _atomic_write_json
    p = tmp_path / "fleet-verdicts.json"
    _atomic_write_json(p, {"ts": "x", "results": []})
    assert oct(p.stat().st_mode & 0o777) == "0o644"


def test_no_sweep_stale_json_exits_2_stdout_still_valid_json(
        tmp_path, capsys):
    """A JSON consumer discards stderr, so staleness must reach it somehow:
    rc=2 is the signal, and stdout stays parseable."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old)
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--json",
                        "--state", str(state)])
    captured = capsys.readouterr()
    assert rc == 2, "stale --json must signal via exit code, not just stderr"
    assert "STALE DATA" in captured.err
    payload = json.loads(captured.out)
    assert isinstance(payload, list) and payload[0]["name"] == "box-a"
    assert _snapshot_state(state) == before


def test_no_sweep_fresh_json_exits_0(tmp_path, capsys):
    state = _write_state(tmp_path, datetime.now(timezone.utc).isoformat())
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--json",
                        "--state", str(state)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "STALE DATA" not in captured.err
    assert json.loads(captured.out)[0]["name"] == "box-a"


def test_no_sweep_footer_names_the_served_file_not_a_heartbeat(
        tmp_path, capsys):
    """Consumer mode writes nothing: the footer must state what was served
    (path + sweep ts), never claim a heartbeat write that never happened."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    state = _write_state(tmp_path, day_old)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2, "stale serve signals rc=2 in text mode too (README table)"
    foot = [l for l in out.splitlines() if "agents need attention" in l]
    assert foot, "footer line missing"
    assert "fleet-verdicts.json" in foot[0]
    assert "heartbeat" not in foot[0]
    assert day_old[:16] in foot[0], "the served sweep ts must be on the face"


def test_no_sweep_does_not_replay_stored_changed_fields(tmp_path, capsys):
    """A stored report is a replay: 'changed' was true one sweep ago, and
    re-announcing it as if it just happened is a lie. --no-sweep treats
    everything as unchanged (a problem agent is still spoken for, as a
    problem, without a '(was X)' arrow)."""
    day_old = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    stored = [dict(HEALTHY_RESULT, verdict="UNREACHABLE", changed=True,
                   previous_verdict="CURRENT")]
    state = _write_state(tmp_path, day_old, results=stored)
    before = _snapshot_state(state)
    with _no_sweep_guard():
        rc = _run_main(["fleet_report.py", "--no-sweep", "--quiet",
                        "--state", str(state)])
    out = capsys.readouterr().out
    assert rc == 2, "stale serve signals rc=2 in text mode too (README table)"
    assert "was CURRENT" not in out, "stored 'changed' fields must not replay"
    assert "UNREACHABLE" in out, "a problem agent is still spoken for"
    assert _snapshot_state(state) == before


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
              stashes="0", dirty="0", shallow="false", mergebase=HEAD_A,
              branch="main", originhead=None, behindorigin=None, aheadorigin=None):
    block = (f"HEAD={head}\nUPSTREAM={upstream}\nBRANCH={branch}\nSHALLOW={shallow}\n"
             f"BEHIND={behind}\nAHEAD={ahead}\nMERGEBASE={mergebase}\n"
             f"STASHES={stashes}\nDIRTY={dirty}")
    if originhead is not None:
        block += f"\nORIGINHEAD={originhead}"
    if behindorigin is not None:
        block += f"\nBEHINDORIGIN={behindorigin}"
    if aheadorigin is not None:
        block += f"\nAHEADORIGIN={aheadorigin}"
    return block


def test_untracked_branch_measured_against_origin_main():
    """The SENTINEL box runs a deliberate `local-patches` branch with no
    upstream. That is a real state to report, not an UNKNOWN to give up on."""
    verdict, reasons = classify(rec(git=git_block(
        upstream="", mergebase="", branch="local-patches",
        originhead=HEAD_B, behindorigin="151", aheadorigin="8")))
    assert verdict == UNTRACKED
    assert "local-patches" in reasons[0] and "no upstream" in reasons[0]
    assert any("8 local commit(s) not in origin/main" in r for r in reasons)
    assert any("151 commit(s) behind origin/main" in r for r in reasons)


def test_falsify_untracked_without_fallback_is_unknown():
    """If origin/main is unreadable too, there is nothing to measure against."""
    verdict, reasons = classify(rec(git=git_block(upstream="", mergebase="",
                                                  branch="local-patches")))
    assert verdict == UNKNOWN
    assert "no origin/main fallback" in reasons[0]


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
