"""Stage B review regressions: gate admission, integrity, exception scoping.

Every test here encodes a review defect verified against the real CLIs
before the fix: the vacuous all-disabled all-clear, untyped roster
identities, an unparseable roster traceback, empty capability path sets
that display as granted while authorising nothing, snapshot integrity
treated as silently valid for clients that no longer compile, declared
exceptions vanishing from snapshots, and the direct-sources API refusing
raw shapes contextually instead of raising deep in the fold.

    python3 -m pytest tests/test_policy_regressions.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.policy import (                    # noqa: E402
    PolicySourceError,
    compile_effective,
    load_sources,
    policy_hash,
)
from policy_fixtures import client_body, write_policy  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def run_policy_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "fleettools.policy", *args],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))


def run_gate(tmp_path, agents, clients, common=None):
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps({"agents": agents}), encoding="utf-8")
    policy = write_policy(
        tmp_path / "policy",
        common=common if common is not None else
        {"capabilities": {"skill_deploy": None}},
        clients=clients)
    return subprocess.run(
        [sys.executable, "fleettools/deploy.py", "--check-policy",
         "--roster", str(roster), "--policy-dir", str(policy)],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))


def agent(name, ident=None, role="worker", enabled=True):
    a = {"name": name, "host": f"USER@10.0.0.{9 if not enabled else 5}",
         "platform": "windows", "enabled": enabled, "role": role}
    if ident is not None:
        a["identity"] = ident
    return a


def one_client(host="enr-host-1", **kw):
    return {"client-a": client_body(host=host, **kw)}


# ---------------------------------------------------------------------------
# Non-vacuous gate: zero executable targets is NOT an all-clear.
# ---------------------------------------------------------------------------

def test_all_disabled_roster_is_not_an_all_clear(tmp_path):
    r = run_gate(tmp_path, [agent("agent-off", ident="enr-host-1",
                                  enabled=False)],
                 one_client())
    assert r.returncode == 1, r.stdout
    assert "NO ENABLED AGENTS" in r.stdout
    assert "1 disabled/unenrolled agent(s)" in r.stdout
    assert "nothing was checked" in r.stdout


def test_empty_roster_is_not_an_all_clear(tmp_path):
    r = run_gate(tmp_path, [], one_client())
    assert r.returncode == 1
    assert "NO ENABLED AGENTS" in r.stdout


def test_zero_targets_still_reports_intended_managed_coverage(tmp_path):
    r = run_gate(tmp_path, [agent("agent-off", enabled=False)],
                 one_client())
    assert r.returncode == 1
    assert "NO ENABLED AGENTS" in r.stdout
    assert "1 disabled/unenrolled" in r.stdout
    # The intended managed coverage stays visible even with nothing checked.
    assert "managed coverage: 1 client policy/policies compiled" in r.stdout
    assert "client-a" in r.stdout


# ---------------------------------------------------------------------------
# Typed roster admission: identities are opaque, case-sensitive strings.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_ident", [["enr-host-1"], {"h": 1}, 17, True])
def test_typed_hole_identity_is_a_contextual_denial(tmp_path, bad_ident):
    r = run_gate(tmp_path, [agent("agent-a", ident=bad_ident)], one_client())
    assert r.returncode == 1, r.stdout
    assert "Traceback" not in r.stderr
    assert "DENY  agent-a" in r.stdout
    assert "identity must be a string" in r.stdout


def test_whitespace_identity_is_refused_not_stripped(tmp_path):
    r = run_gate(tmp_path, [agent("agent-a", ident="  enr-host-1  ")],
                 one_client())
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "no padding or whitespace" in r.stdout
    assert "ALLOW" not in r.stdout


def test_identity_case_must_match_enrollment_exactly(tmp_path):
    # Opaque and case-sensitive: no case-fold invents a Windows-principal
    # equivalence that the enrolment contract never granted.
    r = run_gate(tmp_path, [agent("agent-a", ident="ENR-HOST-1")],
                 one_client())
    assert r.returncode == 1
    assert "no client policy placement" in r.stdout


def test_malformed_roster_json_is_a_typed_refusal(tmp_path):
    roster = tmp_path / "agents.json"
    roster.write_text("{not json at all", encoding="utf-8")
    policy = write_policy(tmp_path / "policy", clients=one_client())
    r = subprocess.run(
        [sys.executable, "fleettools/deploy.py", "--check-policy",
         "--roster", str(roster), "--policy-dir", str(policy)],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "ROSTER REFUSED" in r.stderr
    assert "not valid JSON" in r.stderr


def test_roster_without_an_agents_list_is_refused(tmp_path):
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps({"fleet": []}), encoding="utf-8")
    policy = write_policy(tmp_path / "policy", clients=one_client())
    r = subprocess.run(
        [sys.executable, "fleettools/deploy.py", "--check-policy",
         "--roster", str(roster), "--policy-dir", str(policy)],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "'agents' must be a list" in r.stderr


def test_healthy_gate_reports_enabled_count_and_managed_coverage(tmp_path):
    r = run_gate(tmp_path, [agent("agent-a", ident="enr-host-1")],
                 one_client())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 allowed, 0 refused, 1 enabled agent(s)" in r.stdout
    assert "managed coverage: 1 client policy/policies compiled" in r.stdout
    assert "client-a" in r.stdout


# ---------------------------------------------------------------------------
# Empty capability path sets are refused, not granted.
# ---------------------------------------------------------------------------

def test_empty_path_list_in_a_layer_is_a_source_error(tmp_path):
    write_policy(tmp_path,
                 common={"capabilities": {"skill_deploy": {"paths": []}}},
                 clients=one_client())
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "empty path list" in str(e.value)


def test_empty_path_list_cannot_reach_the_gate(tmp_path):
    # The empty grant that USED to compile to granted:true + paths:[]
    # (and let the gate ALLOW) is refused at the source now.
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps(
        {"agents": [agent("agent-a", ident="enr-host-1")]}), encoding="utf-8")
    policy = write_policy(tmp_path / "policy",
                          common={"capabilities":
                                  {"skill_deploy": {"paths": []}}},
                          clients=one_client())
    r = subprocess.run(
        [sys.executable, "fleettools/deploy.py", "--check-policy",
         "--roster", str(roster), "--policy-dir", str(policy)],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))
    assert r.returncode == 1
    assert "empty path list" in (r.stdout + r.stderr)
    assert "ALLOW" not in r.stdout


def test_unconstrained_and_false_grants_keep_their_meaning(tmp_path):
    write_policy(tmp_path,
                 common={"capabilities": {"a": None, "b": False}},
                 clients=one_client())
    eff = compile_effective(load_sources(tmp_path), now=NOW)["client-a"]
    assert eff.capabilities["a"] == {"granted": True, "paths": None,
                                     "note": None}
    assert eff.capabilities["b"]["granted"] is False


# ---------------------------------------------------------------------------
# Exception scoping: coverage must contain the whole reached region.
# ---------------------------------------------------------------------------

def exc(eid="exc-1", capability="backup", path="/var/secrets",
        approved_at="2026-10-01T00:00:00+00:00",
        expires_at="2026-12-01T00:00:00+00:00"):
    return {"id": eid, "capability": capability, "path": path,
            "approved_by": "approver-a", "approved_at": approved_at,
            "expires_at": expires_at}


def test_exception_scoped_to_a_leaf_does_not_bless_a_broader_grant(tmp_path):
    # The exception covers /var/secrets/db only; the grant reaches ALL of
    # /var/secrets, so the compile fails closed.
    write_policy(tmp_path, common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/secrets"]}},
                     exceptions=[exc(path="/var/secrets/db")])})
    with pytest.raises(Exception) as e:
        compile_effective(load_sources(tmp_path), now=NOW)
    assert "no in-window exception covering it" in str(e.value)


def test_exception_covering_the_reached_region_compiles(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/secrets/db"]}},
                     exceptions=[exc(path="/var/secrets")])})
    eff = compile_effective(load_sources(tmp_path), now=NOW)["client-a"]
    assert eff.exceptions_active[0]["id"] == "exc-1"
    assert eff.exceptions_inactive == []


def test_unused_in_window_exception_is_recorded_not_vanished(tmp_path):
    write_policy(tmp_path,
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/backups"]}},
                     exceptions=[exc(path="/var/telemetry")])})
    eff = compile_effective(load_sources(tmp_path), now=NOW)["client-a"]
    assert eff.exceptions_active == []
    assert eff.exceptions_inactive[0]["status"] == "unused"
    d = eff.as_dict()
    assert d["exceptions_inactive"][0]["status"] == "unused"


# ---------------------------------------------------------------------------
# Snapshot integrity: every stored entry is checked, report-only.
# ---------------------------------------------------------------------------

def snapshot_cmd(tmp_path, mutate, clients=None):
    root = write_policy(tmp_path / "policy",
                        common={"capabilities": {"skill_deploy": None}},
                        clients=clients or {
                            "client-a": client_body(host="h1"),
                            "client-b": client_body(host="h2")})
    snap = tmp_path / "snapshot.json"
    assert run_policy_cli("--policy-dir", str(root), "snapshot", "--out",
                          str(snap)).returncode == 0
    stored = json.loads(snap.read_text(encoding="utf-8"))
    mutate(stored)
    snap.write_text(json.dumps(stored), encoding="utf-8")
    (root / "clients" / "client-b.json").unlink()
    return run_policy_cli("--policy-dir", str(root), "drift", "--against",
                          str(snap))


def test_removed_clients_tampering_is_still_reported(tmp_path):
    def mutate(stored):
        stored["policies"]["client-b"]["protected_paths"] = ["/var/tampered"]
        stored["hashes"]["client-b"] = "deadbeef"
    r = snapshot_cmd(tmp_path, mutate)
    assert r.returncode == 0
    assert "snapshot_hash_mismatch" in r.stdout
    assert "client-b" in r.stdout
    assert "client_removed" in r.stdout


def test_missing_hash_entry_is_reported_not_silently_valid(tmp_path):
    def mutate(stored):
        del stored["hashes"]["client-a"]
    r = snapshot_cmd(tmp_path, mutate)
    assert r.returncode == 0
    assert "snapshot_hash_missing" in r.stdout
    assert "no drift" not in r.stdout


def test_non_dict_snapshot_policy_is_a_typed_refusal(tmp_path):
    def mutate(stored):
        stored["policies"]["client-a"] = 17
    r = snapshot_cmd(tmp_path, mutate)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "must be an object" in r.stderr


def test_snapshot_hash_entry_without_a_policy_is_reported(tmp_path):
    def mutate(stored):
        stored["hashes"]["client-ghost"] = "0" * 64
    r = snapshot_cmd(tmp_path, mutate)
    assert r.returncode == 0
    assert "snapshot_orphan_hash" in r.stdout


def test_snapshot_shape_validation_requires_canonical_keys(tmp_path):
    def mutate(stored):
        del stored["policies"]["client-a"]["placement"]
    r = snapshot_cmd(tmp_path, mutate)
    assert r.returncode == 1
    assert "missing placement" in r.stderr


# ---------------------------------------------------------------------------
# compile_effective with DIRECT sources: same taxonomy, fail closed.
# ---------------------------------------------------------------------------

def good_sources():
    return {"common": {},
            "roles": {"worker": {}},
            "clients": {"client-a": client_body()}}


def test_direct_sources_happy_path_still_compiles():
    eff = compile_effective(good_sources(), now=NOW)
    assert set(eff) == {"client-a"}


def test_direct_sources_bad_vm_identity_is_contextual_not_a_traceback():
    sources = good_sources()
    sources["clients"]["client-a"]["placement"] = {
        "host_identity": "fixture-host", "vm_identity": 17}
    with pytest.raises(PolicySourceError) as e:
        compile_effective(sources, now=NOW)
    msg = str(e.value)
    assert "vm_identity" in msg and "clients['client-a']" in msg


def test_direct_sources_unknown_placement_key_is_contextual():
    sources = good_sources()
    sources["clients"]["client-a"]["placement"] = {
        "host_identity": "h", "rack": "r3"}
    with pytest.raises(PolicySourceError) as e:
        compile_effective(sources, now=NOW)
    assert "unknown key(s) rack" in str(e.value)


def test_direct_sources_empty_placement_is_contextual():
    sources = good_sources()
    sources["clients"]["client-a"]["placement"] = {}
    with pytest.raises(PolicySourceError) as e:
        compile_effective(sources, now=NOW)
    assert "non-empty object" in str(e.value)


def test_direct_sources_relative_path_is_refused():
    sources = good_sources()
    sources["clients"]["client-a"]["capabilities"] = {
        "backup": {"paths": ["relative/path"]}}
    with pytest.raises(PolicySourceError) as e:
        compile_effective(sources, now=NOW)
    assert "not modelable" in str(e.value)


def test_direct_sources_bad_shape_is_reported_not_raised():
    with pytest.raises(PolicySourceError) as e:
        compile_effective({"common": {}, "roles": {},
                           "clients": {"client-a": 17}}, now=NOW)
    assert "must be a dict layer" in str(e.value)


def test_direct_sources_all_problems_reported_together():
    sources = {"common": {"bogus": 1}, "roles": {"worker": {}},
               "clients": {"client-a": client_body(host="h"),
                           "client-b": client_body(host="h2")}}
    del sources["clients"]["client-b"]["placement"]
    with pytest.raises(PolicySourceError) as e:
        compile_effective(sources, now=NOW)
    msg = str(e.value)
    assert "bogus" in msg and "client-b" in msg


# ---------------------------------------------------------------------------
# CLI: contextual placement diagnostics, no tracebacks, full output.
# ---------------------------------------------------------------------------

def test_cli_vm_identity_17_names_file_and_field(tmp_path):
    root = write_policy(tmp_path, clients={"client-a": {
        "role": "worker",
        "placement": {"host_identity": "fixture-host", "vm_identity": 17}}})
    r = run_policy_cli("--policy-dir", str(root), "compile")
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "client-a.json" in r.stderr
    assert "vm_identity" in r.stderr
    assert "must be a non-empty string" in r.stderr


def test_cli_unknown_placement_key_is_reported_with_the_file(tmp_path):
    root = write_policy(tmp_path, clients={"client-a": {
        "role": "worker",
        "placement": {"host_identity": "h", "rack": "r3"}}})
    r = run_policy_cli("--policy-dir", str(root), "compile")
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "rack" in r.stderr and "client-a.json" in r.stderr


def test_cli_identity_shape_rules(tmp_path):
    # An enrolled id with padding or embedded whitespace is refused at load.
    root = write_policy(tmp_path, clients={"client-a": {
        "role": "worker", "placement": {"host_identity": "enr host 1"}}})
    r = run_policy_cli("--policy-dir", str(root), "compile")
    assert r.returncode == 1
    assert "must not contain" in r.stderr
