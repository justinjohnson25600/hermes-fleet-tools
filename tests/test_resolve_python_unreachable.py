#!/usr/bin/env python3
"""An unreachable box is reported as unreachable, not as a box with no Python.

2026-10: a box that had dropped off the tailnet was reported as "no Python 3.8+ found (pin, Hermes
venvs, py launcher, PATH)". ssh exits 255 when it cannot connect or authenticate, so stdout was
empty and the interpreter probe read that as "no interpreter". The operator was sent to repair a
Python install that was fine, and the real fault (the box was offline) went unreported.

    python3 -m pytest tests/test_resolve_python_unreachable.py -q
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fleettools.deploy as d  # noqa: E402


def _fake_run(rc: int, stdout: str = "", stderr: str = ""):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)
    return run


def test_ssh_connect_failure_is_reported_as_unreachable(monkeypatch):
    err = "ssh: connect to host 192.0.2.10 port 22: Operation timed out\r\n"
    monkeypatch.setattr(d.subprocess, "run", _fake_run(255, stderr=err))
    py, note = d.resolve_python("user@192.0.2.10", None)
    assert py is None
    assert note.startswith("unreachable over SSH"), note
    assert "Operation timed out" in note
    assert "no Python" not in note


def test_ssh_failure_with_a_pin_still_does_not_blame_python(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _fake_run(255, stderr="Permission denied (publickey).\n"))
    py, note = d.resolve_python("user@192.0.2.10", r"C:\py\python.exe")
    assert py is None
    assert note.startswith("unreachable over SSH"), note
    assert "Permission denied" in note


def test_reachable_box_without_python_still_says_so(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _fake_run(0, stdout="no interpreter\r\n"))
    py, note = d.resolve_python("user@192.0.2.10", None)
    assert py is None
    assert note == "no Python 3.8+ found (pin, Hermes venvs, py launcher, PATH)"


def test_found_interpreter_is_returned(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", _fake_run(0, stdout="PY=C:\\venv\\Scripts\\python.exe\r\n"))
    py, note = d.resolve_python("user@192.0.2.10", None)
    assert py == "C:\\venv\\Scripts\\python.exe"
    assert note == ""
