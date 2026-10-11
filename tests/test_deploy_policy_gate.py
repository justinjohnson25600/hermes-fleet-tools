"""Integration: deploy.py --check-policy consumes the Stage B compiler.

The gate is the existing-consumer wiring required of this stage: the roster's
`identity` field (stable enrolled identity, supplied as data) joins each agent
to a client policy placement. A missing identity is a refusal with that
reason — hostname, COMPUTERNAME and BIOS serial are never substituted, because
imaged boxes share all three. The gate contacts nothing and writes nothing.

    python3 -m pytest tests/test_deploy_policy_gate.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from policy_fixtures import client_body, write_policy  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def run_deploy(*args):
    return subprocess.run(
        [sys.executable, "fleettools/deploy.py", *args],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))


def agent(name, ident=None, role="worker", host_num=5, enabled=True):
    a = {"name": name, "host": f"USER@10.0.0.{host_num}",
         "platform": "windows", "enabled": enabled, "role": role}
    if ident is not None:
        a["identity"] = ident
    return a


def build(tmp_path, agents, clients, common=None):
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps({"agents": agents}), encoding="utf-8")
    policy = write_policy(
        tmp_path / "policy",
        common=common if common is not None else
        {"capabilities": {"skill_deploy": None}},
        clients=clients)
    return roster, policy


def snapshot_dirs(*roots):
    return sorted(
        (str(p.relative_to(root)), p.read_bytes())
        for root in roots for p in root.rglob("*") if p.is_file()
        for root in [root])


def gate(tmp_path, agents, clients, common=None):
    roster, policy = build(tmp_path, agents, clients, common)
    before = snapshot_dirs(tmp_path / "policy", roster)
    r = run_deploy("--check-policy", "--roster", str(roster),
                   "--policy-dir", str(policy))
    after = snapshot_dirs(tmp_path / "policy", roster)
    assert before == after, "the policy gate must not write anything"
    return r


# ---------------------------------------------------------------------------

def test_gate_allows_rostered_identities_end_to_end(tmp_path):
    r = gate(tmp_path,
             [agent("agent-a", ident="enr-host-1", host_num=5),
              agent("agent-b", ident="enr-host-2", host_num=6)],
             {"client-a": client_body(host="enr-host-1"),
              "client-b": client_body(host="enr-host-2")})
    assert r.returncode == 0, r.stderr
    assert "ALLOW agent-a" in r.stdout and "ALLOW agent-b" in r.stdout
    assert "2 allowed, 0 refused" in r.stdout


def test_gate_refuses_an_agent_with_no_enrolled_identity(tmp_path):
    r = gate(tmp_path, [agent("agent-a", ident=None)],
             {"client-a": client_body(host="enr-host-1")})
    assert r.returncode == 1
    assert "DENY  agent-a" in r.stdout
    assert "no enrolled identity" in r.stdout
    # No silent fallback: the agent's hostname-ish name is never used as identity.
    assert "identity 'agent-a'" not in r.stdout


def test_gate_refuses_an_identity_with_no_client_placement(tmp_path):
    r = gate(tmp_path, [agent("agent-a", ident="enr-host-9")],
             {"client-a": client_body(host="enr-host-1")})
    assert r.returncode == 1
    assert "enr-host-9" in r.stdout and "no client policy placement" in r.stdout


def test_gate_refuses_a_role_mismatch(tmp_path):
    r = gate(tmp_path, [agent("agent-a", ident="enr-host-1", role="worker")],
             {"client-a": client_body(host="enr-host-1", role="writer")})
    assert r.returncode == 1
    assert "role" in r.stdout


def test_gate_refuses_when_capability_is_not_granted(tmp_path):
    r = gate(tmp_path, [agent("agent-a", ident="enr-host-1")],
             {"client-a": client_body(host="enr-host-1")},
             common={})
    assert r.returncode == 1
    assert "skill_deploy" in r.stdout and "not granted" in r.stdout


def test_gate_refuses_when_capability_is_revoked(tmp_path):
    r = gate(tmp_path, [agent("agent-a", ident="enr-host-1")],
             {"client-a": client_body(host="enr-host-1",
                                      caps={"skill_deploy": False})})
    assert r.returncode == 1
    assert "not granted" in r.stdout


def test_gate_refuses_a_duplicate_identity_claim(tmp_path):
    r = gate(tmp_path,
             [agent("agent-a", ident="enr-host-1", host_num=5),
              agent("agent-a-again", ident="enr-host-1", host_num=6)],
             {"client-a": client_body(host="enr-host-1")})
    assert r.returncode == 1
    # Whole-class fail-closed: the FIRST claimant is refused too — it is not
    # an independent check either, and refusing only the second would let
    # the first coast on a result that no longer stands alone.
    assert "ALLOW" not in r.stdout
    assert "already claimed" not in r.stdout
    assert "DENY  agent-a" in r.stdout
    assert "DENY  agent-a-again" in r.stdout
    assert "enr-host-1" in r.stdout and "claimed by" in r.stdout
    assert "2 allowed, 0 refused" not in r.stdout


def test_gate_surfaces_compile_errors_and_refuses_everyone(tmp_path):
    roster, policy = build(
        tmp_path, [agent("agent-a", ident="enr-host-1")],
        {"client-a": {"role": "worker"}})   # no placement: broken
    r = run_deploy("--check-policy", "--roster", str(roster),
                   "--policy-dir", str(policy))
    assert r.returncode == 1
    assert "COMPILATION FAILED" in r.stderr
    assert "placement" in r.stderr
    assert "ALLOW" not in r.stdout


def test_gate_requires_a_policy_dir():
    r = run_deploy("--check-policy")
    assert r.returncode == 2
    assert "--policy-dir" in (r.stderr + r.stdout)


def test_gate_ignores_disabled_agents(tmp_path):
    r = gate(tmp_path,
             [agent("agent-a", ident="enr-host-1"),
              agent("agent-off", ident=None, enabled=False, host_num=9)],
             {"client-a": client_body(host="enr-host-1")})
    assert r.returncode == 0, r.stderr
    assert "agent-off" not in r.stdout
