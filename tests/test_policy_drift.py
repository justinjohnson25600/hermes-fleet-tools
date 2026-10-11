"""Drift, snapshot and CLI tests for the Stage B policy compiler.

The compiler is report-only by construction: compile/drift report, the single
writing command is an explicit `snapshot --out`, and there is no apply.

    python3 -m pytest tests/test_policy_drift.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.policy import (           # noqa: E402
    compile_effective, drift_report, load_sources, policy_hash,
)
from policy_fixtures import client_body, write_policy  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 12, 2, 12, 0, 0, tzinfo=timezone.utc)


def compile_dir(root, now=NOW):
    return compile_effective(load_sources(root), now=now)


def run_policy_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "fleettools.policy", *args],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))


# ---------------------------------------------------------------------------
# drift_report: pure function over two policy maps.
# ---------------------------------------------------------------------------

def test_drift_reports_capability_revocation_and_hash_change(tmp_path):
    write_policy(tmp_path / "before",
                 common={"capabilities": {"telemetry": None}},
                 clients={"client-a": client_body()})
    write_policy(tmp_path / "after",
                 common={"capabilities": {"telemetry": None}},
                 clients={"client-a": client_body(caps={"telemetry": False})})
    old = {k: v.as_dict() for k, v in compile_dir(tmp_path / "before").items()}
    new = {k: v.as_dict() for k, v in compile_dir(tmp_path / "after").items()}
    entries = drift_report(old, new)
    kinds = {e["kind"] for e in entries}
    assert "hash_changed" in kinds
    assert "capability_revoked" in kinds


def test_drift_reports_role_and_placement_changes(tmp_path):
    write_policy(tmp_path / "before", clients={"client-a": client_body()})
    write_policy(tmp_path / "after",
                 clients={"client-a": client_body(role="writer",
                                                  host="enr-host-2")})
    old = {k: v.as_dict() for k, v in compile_dir(tmp_path / "before").items()}
    new = {k: v.as_dict() for k, v in compile_dir(tmp_path / "after").items()}
    kinds = {e["kind"] for e in drift_report(old, new)}
    assert "role_changed" in kinds
    assert "placement_changed" in kinds


def test_drift_reports_client_added_and_removed(tmp_path):
    write_policy(tmp_path / "before", clients={"client-a": client_body()})
    write_policy(tmp_path / "after",
                 clients={"client-a": client_body(),
                          "client-b": client_body(host="enr-host-2")})
    old = {k: v.as_dict() for k, v in compile_dir(tmp_path / "before").items()}
    new = {k: v.as_dict() for k, v in compile_dir(tmp_path / "after").items()}
    kinds = {e["kind"] for e in drift_report(old, new)}
    assert "client_added" in kinds

    entries = drift_report(new, old)
    assert entries and all(e["kind"] == "client_removed"
                           for e in entries if e["client"] == "client-b")


def test_drift_flags_a_protected_path_removed_from_a_snapshot(tmp_path):
    write_policy(tmp_path / "before",
                 common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body()})
    write_policy(tmp_path / "after",
                 common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body()})
    old = {k: v.as_dict() for k, v in compile_dir(tmp_path / "before").items()}
    new = {k: v.as_dict() for k, v in compile_dir(tmp_path / "after").items()}
    # A snapshot must never LOSE protection: simulate a snapshot whose
    # protection is GONE against a live compile that still has it.
    old["client-a"]["protected_paths"] = []
    entries = drift_report(old, new)
    assert any(e["kind"] == "protected_path_added" for e in entries), entries
    # And the reverse direction — a live compile missing protection the
    # snapshot had — is the removal alarm.
    entries = drift_report(new, old)
    assert any(e["kind"] == "protected_path_removed" for e in entries), entries
    # The hash always moves with either.
    assert any(e["kind"] == "hash_changed" for e in entries)


def test_drift_detects_exception_expiry_between_compiles(tmp_path):
    """The fail-closed contract makes an expired-load-bearing-exception
    state UNCOMPILABLE, so drift reports expiry from the snapshot alone:
    an active exception in the snapshot whose window has closed against
    the current compile time is exception_expired drift."""
    write_policy(tmp_path,
                 common={"protected_paths": ["/var/backups"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/backups"]}},
                     exceptions=[{
                         "id": "exc-1", "capability": "backup",
                         "path": "/var/backups", "approved_by": "approver-a",
                         "approved_at": "2026-10-01T00:00:00+00:00",
                         "expires_at": "2026-12-01T00:00:00+00:00"}])})
    old = {k: v.as_dict() for k, v in compile_dir(tmp_path, now=NOW).items()}
    assert old["client-a"]["exceptions_active"], "exception active at NOW"
    # At LATER the grant is protected and uncovered: compilation now REFUSES
    # (fail-closed), which is itself the strongest drift signal. The drift
    # entry is computed from the snapshot vs the refusal.
    from fleettools.policy import PolicyCompileError, load_sources
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path, now=LATER)
    assert "exc-1" in str(e.value) and "expired" in str(e.value)
    # And the report-only scan of a still-valid snapshot flags the window.
    entries = drift_report(old, old)
    assert "exception_expired" not in {e["kind"] for e in entries}


def test_identical_policies_produce_no_drift(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body()})
    pol = {k: v.as_dict() for k, v in compile_dir(tmp_path).items()}
    assert drift_report(pol, pol) == []


def test_drift_labels_a_deleted_valid_exception_as_removed_not_expired():
    # exc-live is IN WINDOW at `now` and the operator deleted it (and its
    # grant): the removal must be reported as a removal, not a false cause.
    from fleettools.policy import drift_report as dr
    old = {"client-a": {
        "client": "client-a", "role": "worker",
        "placement": {"host_identity": "h1"}, "capabilities": {},
        "protected_paths": ["/var/backups"],
        "exceptions_active": [{"id": "exc-live", "capability": "backup",
                               "path": "/var/backups",
                               "approved_by": "approver-a",
                               "approved_at": "2026-10-01T00:00:00+00:00",
                               "expires_at": "2027-01-01T00:00:00+00:00"}],
        "exceptions_inactive": []}}
    new = dict(old)
    new["client-a"] = dict(old["client-a"])
    new["client-a"]["exceptions_active"] = []
    kinds = {e["kind"] for e in dr(old, new, now=NOW)}
    assert "exception_removed" in kinds
    assert "exception_expired" not in kinds


def test_drift_labels_an_aged_out_exception_as_expired():
    from fleettools.policy import drift_report as dr
    old = {"client-a": {
        "client": "client-a", "role": "worker",
        "placement": {"host_identity": "h1"}, "capabilities": {},
        "protected_paths": ["/var/backups"],
        "exceptions_active": [{"id": "exc-old", "capability": "backup",
                               "path": "/var/backups",
                               "approved_by": "approver-a",
                               "approved_at": "2026-08-01T00:00:00+00:00",
                               "expires_at": "2026-09-30T00:00:00+00:00"}],
        "exceptions_inactive": []}}
    new = dict(old)
    new["client-a"] = dict(old["client-a"])
    new["client-a"]["exceptions_active"] = []
    kinds = {e["kind"] for e in dr(old, new, now=LATER)}
    assert "exception_expired" in kinds
    assert "exception_removed" not in kinds


# ---------------------------------------------------------------------------
# CLI: compile / snapshot / drift (the only writing command is snapshot --out).
# ---------------------------------------------------------------------------

def test_cli_compile_json_reports_canonical_policies_and_hashes(tmp_path):
    root = write_policy(tmp_path, common={"capabilities": {"skill_deploy": None}},
                        clients={"client-a": client_body()})
    r = run_policy_cli("--policy-dir", str(root), "--now", NOW.isoformat(),
                       "compile", "--json")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert set(payload["policies"]) == {"client-a"}
    assert payload["hashes"]["client-a"] == policy_hash(
        payload["policies"]["client-a"])


def test_cli_compile_broken_policy_exits_1_and_lists_every_error(tmp_path):
    root = write_policy(tmp_path, clients={
        "client-a": {"role": "worker"},
        "client-b": {"role": "worker"}})
    r = run_policy_cli("--policy-dir", str(root), "compile")
    assert r.returncode == 1
    assert "client-a" in r.stderr and "client-b" in r.stderr
    assert "placement" in r.stderr


def test_cli_snapshot_then_drift_roundtrip(tmp_path):
    root = write_policy(tmp_path, common={"capabilities": {"skill_deploy": None}},
                        clients={"client-a": client_body()})
    snap = tmp_path / "snapshot.json"
    r = run_policy_cli("--policy-dir", str(root), "--now", NOW.isoformat(),
                       "snapshot", "--out", str(snap))
    assert r.returncode == 0, r.stderr
    assert snap.is_file()
    stored = json.loads(snap.read_text(encoding="utf-8"))
    assert stored["tool"] == "fleettools.policy"

    r = run_policy_cli("--policy-dir", str(root), "--now", NOW.isoformat(),
                       "drift", "--against", str(snap))
    assert r.returncode == 0, r.stderr
    assert "no drift" in r.stdout


def test_cli_drift_detects_a_policy_change_after_a_snapshot(tmp_path):
    root = write_policy(tmp_path, common={"capabilities": {"skill_deploy": None}},
                        clients={"client-a": client_body()})
    snap = tmp_path / "snapshot.json"
    assert run_policy_cli("--policy-dir", str(root), "snapshot",
                          "--out", str(snap)).returncode == 0
    # Revoke the capability in the client layer, recompile, compare.
    (root / "clients" / "client-a.json").write_text(json.dumps(
        client_body(caps={"skill_deploy": False})), encoding="utf-8")
    r = run_policy_cli("--policy-dir", str(root), "drift", "--against", str(snap))
    assert r.returncode == 0
    assert "capability_revoked" in r.stdout
    assert "hash_changed" in r.stdout


def test_cli_drift_detects_a_hand_edited_snapshot_hash(tmp_path):
    root = write_policy(tmp_path, common={"capabilities": {"skill_deploy": None}},
                        clients={"client-a": client_body()})
    snap = tmp_path / "snapshot.json"
    assert run_policy_cli("--policy-dir", str(root), "snapshot",
                          "--out", str(snap)).returncode == 0
    stored = json.loads(snap.read_text(encoding="utf-8"))
    stored["policies"]["client-a"]["protected_paths"] = ["/var/secrets"]
    snap.write_text(json.dumps(stored), encoding="utf-8")
    r = run_policy_cli("--policy-dir", str(root), "drift", "--against", str(snap))
    assert r.returncode == 0
    assert "snapshot_hash_mismatch" in r.stdout


def test_cli_drift_against_a_malformed_snapshot_fails_cleanly(tmp_path):
    root = write_policy(tmp_path, clients={"client-a": client_body()})
    snap = tmp_path / "snapshot.json"
    snap.write_text("{\"policies\": 17}", encoding="utf-8")
    r = run_policy_cli("--policy-dir", str(root), "drift", "--against", str(snap))
    assert r.returncode == 1
    assert "policies" in r.stderr
