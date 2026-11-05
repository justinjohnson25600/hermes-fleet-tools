"""Remote macOS/Linux agents: skills-only opt-in and the POSIX payload transport.

A Mac joined the fleet (2026-11-05) while every remote path assumed Windows (PowerShell interpreter
probe, %TEMP% staging). These tests drive the real transport through a stand-in `ssh` that runs the
remote command locally, so the payload, stdin hand-off and exit-code handling are the real ones."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleettools.deploy as d  # noqa: E402
import fleettools.soul_directive as sd  # noqa: E402

posix_only = pytest.mark.skipif(os.name == "nt", reason="stand-in ssh is a POSIX shell script")


def _fake_ssh(tmp_path: Path, monkeypatch, body: str) -> None:
    """Put an `ssh` on PATH whose behaviour is `body` (a sh snippet; "$@" is the ssh argv)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))


# Drop ssh's options and the host, then run the remote command line locally (stdin passes through).
RUN_REMOTE = 'while [ "$1" = "-o" ]; do shift 2; done; shift; exec sh -c "$*"'


def test_skills_only_agent_is_a_deploy_target_and_plain_disabled_is_not():
    agents = [
        {"name": "on", "enabled": True},
        {"name": "skills", "enabled": False, "skills_enabled": True},
        {"name": "off", "enabled": False, "blocked_reason": "offline"},
    ]
    assert [a["name"] for a in d.deploy_targets(agents)] == ["on", "skills"]


def test_only_remote_macos_and_linux_take_the_posix_path():
    assert d.is_posix_remote({"host": "u@h", "platform": "macos"})
    assert d.is_posix_remote({"host": "u@h", "platform": "linux"})
    assert not d.is_posix_remote({"host": "local", "platform": "macos"})
    assert not d.is_posix_remote({"host": "u@h", "platform": "windows"})


@posix_only
def test_posix_payload_runs_from_stdin_and_returns_its_stdout(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, RUN_REMOTE)
    out, err = d.run_posix_payload("user@192.0.2.10", sys.executable, "import json; print(json.dumps({'ok': 1}))", 60)
    assert err is None and json.loads(out) == {"ok": 1}


@posix_only
def test_ssh_failure_is_unreachable_not_a_payload_fault(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, 'echo "ssh: connect to host 192.0.2.10 port 22: Connection refused" >&2; exit 255')
    out, err = d.run_posix_payload("user@192.0.2.10", "python3", "print(1)", 60)
    assert out == "" and err.startswith("unreachable over SSH") and "Connection refused" in err


@posix_only
def test_missing_interpreter_is_reported_with_its_stderr(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, RUN_REMOTE)
    out, err = d.run_posix_payload("user@192.0.2.10", "/nonexistent/python3", "print(1)", 60)
    assert out == "" and "/nonexistent/python3" in err and "no output" in err


@posix_only
def test_macos_remote_never_takes_the_windows_probe(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, RUN_REMOTE)

    def windows_probe(*a, **k):
        raise AssertionError("a macOS box was sent the PowerShell interpreter probe")

    monkeypatch.setattr(d, "resolve_python", windows_probe)
    monkeypatch.setattr(d, "render_remote", lambda *a, **k: "import json; print(json.dumps({'skills': {}}))")
    name, rep = d.deliver({"name": "studio", "host": "user@192.0.2.10", "platform": "macos",
                           "python": sys.executable}, ["x"], dry=True)
    assert name == "studio" and rep == {"skills": {}}


@posix_only
def test_soul_directive_lands_on_a_remote_mac_through_the_posix_path(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, RUN_REMOTE)
    home = tmp_path / "studio-home"
    skill = home / "skills" / "autonomous-ai-agents" / sd.SKILL
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    (home / "SOUL.md").write_text("Studio persona\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    agent = {"name": "studio", "host": "user@192.0.2.10", "platform": "macos", "python": sys.executable,
             "enabled": False, "skills_enabled": True}
    name, rep = sd.run_on(agent, apply=True, skill=sd.SKILL)
    assert name == "studio" and [p["status"] for p in rep["profiles"]] == ["added"]
    text = (home / "SOUL.md").read_text(encoding="utf-8")
    assert text.startswith("Studio persona") and f"load the `{sd.SKILL}` skill" in text
