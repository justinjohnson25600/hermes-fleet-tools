#!/usr/bin/env python3
"""Every PowerShell payload this repo sends to Windows fits on cmd.exe's command line.

Windows OpenSSH runs a remote command through cmd.exe, which refuses a line over 8191 characters
("The command line is too long."). -EncodedCommand base64-encodes UTF-16, so each payload
character costs ~2.7 on the wire. On 2026-10-18 three comment blocks added inside PS_PROBE took
its line to 8,546 characters and the nightly report read every Windows node as UNREACHABLE.

Each test named `test_falsify_*` breaks the thing the guard protects and asserts it goes red.

    python3 -m pytest tests/test_windows_command_line.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools import deploy                       # noqa: E402
from fleettools import fleet_report as fr           # noqa: E402

# Windows MAX_PATH (260) bounds every path the roster can carry, so a payload that fits with each
# substitution at that length fits every node.
MAX_PATH = 260


def _max_path(leaf: str) -> str:
    head = "%LOCALAPPDATA%\\hermes\\"
    return head + "d" * (MAX_PATH - len(head) - len(leaf) - 1) + "\\" + leaf


LONG_CLI = _max_path("hermes.exe")
LONG_REPO = _max_path("hermes-agent")
LONG_PIN = _max_path("python.exe")

PADDING = "Write-Output 'padding padding padding'\n" * 200


def test_status_probe_fits_cmd_exe_and_round_trips():
    body = fr.PS_PROBE.replace("@@CLI@@", LONG_CLI).replace("@@REPO@@", LONG_REPO)
    line = fr.powershell_command(body)
    assert len(line) <= fr.CMD_EXE_MAX_COMMAND
    assert base64.b64decode(line.rsplit(" ", 1)[1]).decode("utf-16-le") == body


def test_deploy_interpreter_probe_fits_cmd_exe():
    script = deploy._RESOLVE_PY % {"pin": LONG_PIN}
    assert len(fr.powershell_command(script, "-NoProfile -NonInteractive")) <= fr.CMD_EXE_MAX_COMMAND


def test_falsify_oversize_payload_is_refused():
    with pytest.raises(ValueError, match=str(fr.CMD_EXE_MAX_COMMAND)):
        fr.powershell_command(PADDING)


def test_falsify_oversize_probe_names_its_cause_and_never_calls_ssh(monkeypatch):
    sent = []
    monkeypatch.setattr(fr, "PS_PROBE", PADDING)
    monkeypatch.setattr(fr.subprocess, "run", lambda *a, **k: sent.append(a))
    rec = fr.probe({"name": "win", "platform": "windows", "host": "user@win"}, timeout=5)
    assert sent == []
    assert "cmd.exe accepts" in (rec["error"] or "")
