"""Tests for the Stage B effective business-client policy compiler.

The compiler merges common -> role -> client policy layers into one canonical
effective policy per client, and it is fail-closed: a file it cannot fully
understand is a compile error naming the file and field, never a silent
default. Every fixture here is synthetic placeholder data (enr-host-N,
approver-a, /var/telemetry) so the suite stays publishable.

    python3 -m pytest tests/test_policy_compiler.py -q
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.policy import (           # noqa: E402
    PolicyCompileError,
    PolicySourceError,
    canonical_json,
    compile_effective,
    load_sources,
    policy_hash,
)
from policy_fixtures import client_body, write_policy  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def exc(eid="exc-1", capability="backup", path="/var/telemetry",
        approved_by="approver-a", approved_at="2026-10-01T00:00:00+00:00",
        expires_at="2026-12-01T00:00:00+00:00"):
    body = {"id": eid, "capability": capability, "approved_by": approved_by,
            "approved_at": approved_at, "expires_at": expires_at}
    if path is not None:
        body["path"] = path
    return body


def compile_dir(root, now=NOW):
    return compile_effective(load_sources(root), now=now)


# ---------------------------------------------------------------------------
# Layer merge: common -> role -> client, deterministic.
# ---------------------------------------------------------------------------

def test_layers_merge_common_role_client_in_order(tmp_path):
    write_policy(tmp_path,
                 common={"capabilities": {"backup": None, "telemetry": None},
                         "protected_paths": ["/var/secrets"]},
                 roles={"worker": {"capabilities":
                                   {"backup": {"paths": ["/var/backups"]}}}},
                 clients={"client-a": client_body(caps={"telemetry": False})})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.capabilities["backup"] == {
        "granted": True, "paths": ["/var/backups"], "note": None}
    assert eff.capabilities["telemetry"] == {
        "granted": False, "paths": None, "note": None}
    assert eff.protected_paths == ["/var/secrets"]
    assert eff.placement == {"host_identity": "enr-host-1"}


def test_result_is_deterministic_across_recompiles(tmp_path):
    write_policy(tmp_path,
                 common={"capabilities": {"backup": {"paths": ["/var/backups"]}}},
                 roles={"worker": {"protected_paths": ["/var/secrets"]}},
                 clients={"client-a": client_body()})
    first = compile_dir(tmp_path)["client-a"].as_dict()
    second = compile_dir(tmp_path)["client-a"].as_dict()
    assert first == second
    assert policy_hash(first) == policy_hash(second)


def test_hash_is_deterministic_regardless_of_file_key_order(tmp_path):
    write_policy(tmp_path / "a",
                 common={"capabilities": {"x": None}, "protected_paths": ["/p"]},
                 clients={"c": client_body()})
    write_policy(tmp_path / "b",
                 common={"protected_paths": ["/p"], "capabilities": {"x": None}},
                 clients={"c": client_body()})
    h1 = policy_hash(compile_dir(tmp_path / "a")["c"].as_dict())
    h2 = policy_hash(compile_dir(tmp_path / "b")["c"].as_dict())
    assert h1 == h2


def test_canonical_json_sorts_keys():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


# ---------------------------------------------------------------------------
# Placement: identity is supplied data, never guessed.
# ---------------------------------------------------------------------------

def test_missing_client_placement_is_an_error_not_a_guess(tmp_path):
    write_policy(tmp_path, clients={"client-a": {"role": "worker"}})
    with pytest.raises(PolicySourceError) as e:
        compile_dir(tmp_path)
    assert "placement" in str(e.value)
    assert "client-a" in str(e.value)


def test_placement_in_a_non_client_layer_is_an_error(tmp_path):
    write_policy(tmp_path,
                 roles={"worker": {"placement": {"host_identity": "enr-host-1"}}},
                 clients={"client-a": client_body()})
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "placement" in str(e.value)


def test_placement_passes_vm_identity_through(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body(placement={
        "host_identity": "enr-host-1", "vm_identity": "vm-a"})})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.placement == {"host_identity": "enr-host-1", "vm_identity": "vm-a"}


def test_client_role_missing_is_an_error(tmp_path):
    write_policy(tmp_path, clients={"client-a": {"placement":
                                                 {"host_identity": "enr-host-1"}}})
    with pytest.raises(PolicySourceError) as e:
        compile_dir(tmp_path)
    assert "role" in str(e.value)


def test_missing_role_layer_is_an_error(tmp_path):
    # The role file is genuinely ABSENT (auto_role=False, no fake create-
    # then-delete dance), and the compile refuses the source set: a role a
    # client names must have a layer file, even an empty one.
    write_policy(tmp_path, clients={"client-a": client_body(role="writer")},
                 auto_role=False)
    with pytest.raises(PolicySourceError) as e:
        compile_dir(tmp_path)
    assert "writer" in str(e.value)
    assert "roles/writer.json" in str(e.value)


def test_missing_roles_dir_is_an_error(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body(role="writer")},
                 auto_role=False)
    (tmp_path / "roles").rmdir()
    with pytest.raises(PolicySourceError) as e:
        compile_dir(tmp_path)
    assert "roles/" in str(e.value)


# ---------------------------------------------------------------------------
# One physical host, many clients: only explicit isolated VM identities.
# ---------------------------------------------------------------------------

def test_two_clients_on_one_host_without_vm_identities_is_rejected(tmp_path):
    write_policy(tmp_path, clients={
        "client-a": client_body(host="enr-host-1"),
        "client-b": client_body(host="enr-host-1")})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    msg = str(e.value)
    assert "enr-host-1" in msg and "client-a" in msg and "client-b" in msg
    assert "vm_identity" in msg


def test_two_clients_on_one_host_with_distinct_vm_identities_is_accepted(tmp_path):
    write_policy(tmp_path, clients={
        "client-a": client_body(placement={"host_identity": "enr-host-1",
                                           "vm_identity": "vm-a"}),
        "client-b": client_body(placement={"host_identity": "enr-host-1",
                                           "vm_identity": "vm-b"})})
    eff = compile_dir(tmp_path)
    assert set(eff) == {"client-a", "client-b"}


def test_same_vm_identity_on_a_shared_host_is_rejected(tmp_path):
    write_policy(tmp_path, clients={
        "client-a": client_body(placement={"host_identity": "enr-host-1",
                                           "vm_identity": "vm-a"}),
        "client-b": client_body(placement={"host_identity": "enr-host-1",
                                           "vm_identity": "vm-a"})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "vm-a" in str(e.value)


def test_distinct_hosts_need_no_vm_identities(tmp_path):
    write_policy(tmp_path, clients={
        "client-a": client_body(host="enr-host-1"),
        "client-b": client_body(host="enr-host-2")})
    assert set(compile_dir(tmp_path)) == {"client-a", "client-b"}


# ---------------------------------------------------------------------------
# Protected paths and capability constraints.
# ---------------------------------------------------------------------------

def test_unprotected_grant_needs_no_exception(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body(
        caps={"backup": {"paths": ["/var/backups"]}})})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.capabilities["backup"]["granted"] is True


def test_protected_path_grant_requires_active_exception(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/telemetry"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/telemetry"]}})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "/var/telemetry" in str(e.value)


def test_active_exception_makes_protected_grant_legal(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/telemetry"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/telemetry"]}},
                     exceptions=[exc()])})
    eff = compile_dir(tmp_path)["client-a"]
    assert len(eff.exceptions_active) == 1
    assert eff.exceptions_active[0]["id"] == "exc-1"
    assert eff.exceptions_inactive == []


def test_unconstrained_grant_does_not_implicitly_include_protected_paths(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/secrets"],
                                   "capabilities": {"telemetry": None}},
                 clients={"client-a": client_body()})
    eff = compile_dir(tmp_path)["client-a"]
    # The grant compiles, but the protected path is not inside it: consumers
    # treat an unconstrained grant as everything EXCEPT protected paths.
    assert eff.capabilities["telemetry"]["paths"] is None
    assert eff.protected_paths == ["/var/secrets"]


def test_client_cannot_widen_role_capability_paths(tmp_path):
    write_policy(tmp_path,
                 roles={"worker": {"capabilities":
                                   {"backup": {"paths": ["/var/backups"]}}}},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/backups", "/var/secrets"]}})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "widen" in str(e.value)


def test_client_cannot_unconstrain_a_constrained_capability(tmp_path):
    write_policy(tmp_path,
                 roles={"worker": {"capabilities":
                                   {"backup": {"paths": ["/var/backups"]}}}},
                 clients={"client-a": client_body(caps={"backup": None})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "widen" in str(e.value)


def test_revoked_capability_cannot_be_revived_by_a_lower_layer(tmp_path):
    write_policy(tmp_path,
                 common={"capabilities": {"telemetry": None}},
                 roles={"worker": {"capabilities": {"telemetry": False}}},
                 clients={"client-a": client_body(caps={"telemetry": None})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "revive" in str(e.value).lower()


def test_protected_paths_only_ever_grow(tmp_path):
    write_policy(tmp_path,
                 common={"protected_paths": ["/var/secrets"]},
                 roles={"worker": {"protected_paths": ["/var/telemetry"]}},
                 clients={"client-a": client_body()})
    eff = compile_dir(tmp_path)["client-a"]
    # Order is layer order (common first), membership is a set.
    assert eff.protected_paths == ["/var/secrets", "/var/telemetry"]


# ---------------------------------------------------------------------------
# Exceptions: explicit, approved, expiring — fail-closed and reported.
# ---------------------------------------------------------------------------

def test_expired_exception_fails_closed_and_is_reported(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/telemetry"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/telemetry"]}},
                     exceptions=[exc(approved_at="2026-08-01T00:00:00+00:00",
                                     expires_at="2026-09-30T00:00:00+00:00")])})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    msg = str(e.value)
    assert "expired" in msg and "/var/telemetry" in msg and "exc-1" in msg


def test_expired_non_load_bearing_exception_is_inactive_and_reported(tmp_path):
    write_policy(tmp_path,
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/backups"]}},
                     exceptions=[exc(approved_at="2026-08-01T00:00:00+00:00",
                                     expires_at="2026-09-30T00:00:00+00:00")])})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.exceptions_active == []
    assert eff.exceptions_inactive[0]["status"] == "expired"
    assert eff.capabilities["backup"]["granted"] is True


def test_future_dated_exception_is_not_yet_in_effect(tmp_path):
    write_policy(tmp_path,
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/backups"]}},
                     exceptions=[exc(approved_at="2026-11-01T00:00:00+00:00",
                                     expires_at="2026-12-01T00:00:00+00:00")])})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.exceptions_inactive[0]["status"] == "not_yet_in_effect"


@pytest.mark.parametrize("mutate,fragment", [
    ({"approved_by": ""}, "approved_by"),
    ({"approved_by": None}, "approved_by"),
    ({"expires_at": "not-a-date"}, "expires_at"),
    ({"approved_at": "not-a-date"}, "approved_at"),
    ({"approved_at": "2026-12-01T00:00:00+00:00",
      "expires_at": "2026-11-01T00:00:00+00:00"}, "window"),
])
def test_malformed_exception_is_a_compile_error(tmp_path, mutate, fragment):
    write_policy(tmp_path,
                 clients={"client-a": client_body(exceptions=[exc(**mutate)])})
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert fragment in str(e.value)
    assert "exc-1" in str(e.value)


def test_exceptions_are_only_valid_in_the_client_layer(tmp_path):
    write_policy(tmp_path,
                 roles={"worker": {"exceptions": [exc()]}},
                 clients={"client-a": client_body()})
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "exceptions" in str(e.value)


# ---------------------------------------------------------------------------
# Fail-closed loading: unknown keys, missing layers, every error reported.
# ---------------------------------------------------------------------------

def test_unknown_top_level_key_is_rejected(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body(exceptions=[],
                                                            ) | {"exeptions": []}})
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "exeptions" in str(e.value)


def test_missing_common_layer_is_an_error(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body()}, with_common=False)
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "common" in str(e.value)


def test_compile_without_any_client_is_an_error(tmp_path):
    write_policy(tmp_path)
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "client" in str(e.value)


def test_compile_reports_every_broken_client_not_just_the_first(tmp_path):
    # A missing placement is a LOAD error here (the fixture auto-creates the
    # empty role file, so layer resolution succeeds and both clients are
    # reported together in one pass).
    write_policy(tmp_path, clients={
        "client-a": {"role": "worker"},
        "client-b": {"role": "worker"}})
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    msg = str(e.value)
    assert "client-a" in msg and "client-b" in msg


def test_unparseable_layer_json_names_the_file(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body()})
    (tmp_path / "clients" / "client-a.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "client-a.json" in str(e.value)


def test_bad_client_filename_is_rejected(tmp_path):
    write_policy(tmp_path, clients={"client-a": client_body()})
    (tmp_path / "clients" / "Bad Name!.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    assert "Bad Name!" in str(e.value)
