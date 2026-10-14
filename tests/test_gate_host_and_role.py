"""Ambiguous host placement and roster-role admission in the policy gate.

A roster agent's enrolled identity names a HOST. When several clients are
legitimately placed on one host (each with its own vm_identity), no single
client's policy may answer for the agent: the gate must refuse the claim as
ambiguous instead of silently picking one. And the roster role is checked
BEFORE any capability: a missing, blank, or non-string role is a refusal,
never a pass.

    python3 -m pytest tests/test_gate_host_and_role.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_deploy_policy_gate import agent, gate  # noqa: E402


def vm_client(host, vm, caps=None):
    from policy_fixtures import client_body
    return client_body(
        placement={"host_identity": host, "vm_identity": vm}, caps=caps)


# ---------------------------------------------------------------------------
# Ambiguous host placement: never silently choose a client.
# ---------------------------------------------------------------------------

def test_shared_host_refuses_the_agent_even_when_one_client_grants(tmp_path):
    # client-a revokes skill_deploy, client-b grants it. The old map kept the
    # LAST compiled client (client-b), so the gate ALLOWED on the granting
    # policy even though the roster never said which client this agent is.
    r = gate(tmp_path,
             [agent("agent-a", ident="enr-host-1")],
             {"client-a": vm_client("enr-host-1", "vm-a",
                                    caps={"skill_deploy": False}),
              "client-b": vm_client("enr-host-1", "vm-b")})
    assert r.returncode == 1, r.stdout
    assert "DENY  agent-a" in r.stdout
    assert "ALLOW" not in r.stdout
    assert "ambiguous" in r.stdout
    assert "client-a" in r.stdout and "client-b" in r.stdout


def test_an_ambiguous_host_leaves_unique_hosts_usable(tmp_path):
    r = gate(tmp_path,
             [agent("agent-a", ident="enr-host-1"),    # shared host
              agent("agent-b", ident="enr-host-2")],   # unique host
             {"client-a1": vm_client("enr-host-1", "vm-a"),
              "client-a2": vm_client("enr-host-1", "vm-b"),
              "client-b": client_body_unique()})
    assert r.returncode == 1, r.stdout
    assert "DENY  agent-a" in r.stdout and "ambiguous" in r.stdout
    assert "ALLOW agent-b" in r.stdout
    assert "1 allowed" in r.stdout


def client_body_unique():
    from policy_fixtures import client_body
    return client_body(host="enr-host-2")


# ---------------------------------------------------------------------------
# Roster role: refused before any capability is considered.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_role", [None, "", "   "])
def test_missing_or_blank_roster_role_is_refused(tmp_path, bad_role):
    a = agent("agent-a", ident="enr-host-1")
    if bad_role is None:
        del a["role"]
    else:
        a["role"] = bad_role
    r = gate(tmp_path, [a],
             {"client-a": client_body_unique_for(tmp_path, "enr-host-1")})
    assert r.returncode == 1, r.stdout
    assert "DENY  agent-a" in r.stdout
    assert "role" in r.stdout
    assert "ALLOW" not in r.stdout


def test_non_string_roster_role_is_refused(tmp_path):
    a = agent("agent-a", ident="enr-host-1")
    a["role"] = 7
    r = gate(tmp_path, [a],
             {"client-a": client_body_unique_for(tmp_path, "enr-host-1")})
    assert r.returncode == 1, r.stdout
    assert "DENY  agent-a" in r.stdout
    assert "ALLOW" not in r.stdout


def client_body_unique_for(tmp_path, host):
    from policy_fixtures import client_body
    return client_body(host=host)
