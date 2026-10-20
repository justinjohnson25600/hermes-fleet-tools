#!/usr/bin/env python3
"""A candidate ref installs THAT ref's files, and a box is OK only at the version the CDN serves.

install.py downloads every skill file from its own RAW_BASE constant (the repo's main), whatever
ref it was itself fetched from. So FLEET_SKILLS_RAW=<candidate sha> ran the candidate's installer,
which installed main's files and printed OK at the old version (2026-10-20, a 1.2.8 canary that
stayed on 1.2.6). The payload now pins the installer's RAW_BASE to RAW, and the verdict fails any
delivered skill whose installed version differs from the version served at RAW.

    python3 -m pytest tests/test_deploy_pins_installer.py -q
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.deploy import PIN_SRC, render_remote, version_failures  # noqa: E402

_pin: dict = {}
exec(PIN_SRC, _pin)
pin_installer = _pin["pin_installer"]

INSTALLER = '#!/usr/bin/env python3\nimport os\nRAW_BASE = "https://raw.example/org/skills/main"\n\ndef fetch(p):\n    return RAW_BASE + "/" + p\n'
CAND = "https://raw.example/org/skills/0123456789abcdef0123456789abcdef01234567"


def test_installer_is_pinned_to_the_ref_it_was_fetched_from():
    out = pin_installer(INSTALLER, CAND)
    assert f'RAW_BASE = "{CAND}"' in out
    assert "/main" not in out
    ns: dict = {}
    exec(out, ns)
    assert ns["fetch"]("dev-pair/SKILL.md") == CAND + "/dev-pair/SKILL.md"


def test_no_constant_refuses_rather_than_installing_main():
    with pytest.raises(ValueError, match="RAW_BASE"):
        pin_installer("print('an installer with no base constant')\n", CAND)


def test_two_constants_refuse():
    with pytest.raises(ValueError, match="RAW_BASE"):
        pin_installer(INSTALLER + 'RAW_BASE = "https://elsewhere"\n', CAND)


def test_the_payload_pins_before_it_runs_the_installer():
    code = render_remote(["s"], False, raw=CAND)
    compile(code, "<remote>", "exec")
    assert "def pin_installer" in code
    fetch, pin, run = code.index("/install.py?cb="), code.index("pin_installer(_inst, RAW)"), code.index("str(tmp), s]")
    assert fetch < pin < run


def report(**skills) -> dict:
    return {"skills": skills, "tests": {"passed": 605, "failed": 0}}


def test_box_left_on_the_old_version_fails():
    r = report(**{"dev-pair": {"rc": 0, "err": "", "version": "1.2.6"}})
    msgs = version_failures(r, {"dev-pair": "1.2.8"})
    assert len(msgs) == 1 and "1.2.6" in msgs[0] and "1.2.8" in msgs[0]


def test_box_at_the_served_version_passes():
    r = report(**{"dev-pair": {"rc": 0, "err": "", "version": "1.2.8"}})
    assert version_failures(r, {"dev-pair": "1.2.8"}) == []


def test_held_and_dry_run_skills_are_not_version_failures():
    r = report(**{"a": {"held": True, "version": "1.0.0"}, "b": {"dry_run": True}})
    assert version_failures(r, {"a": "2.0.0", "b": "2.0.0"}) == []


def test_unknown_expectation_is_not_a_failure():
    r = report(**{"a": {"rc": 0, "err": "", "version": "1.0.0"}})
    assert version_failures(r, {}) == []
    assert version_failures(r, None) == []


def test_end_to_end_the_box_installs_from_the_served_ref(tmp_path):
    # The real payload, a real HTTP fetch, and an installer that records where it would download from.
    srv = tmp_path / "srv"
    srv.mkdir()
    (srv / "install.py").write_text(
        'RAW_BASE = "https://raw.example/org/skills/main"\n'
        "import os, pathlib\n"
        "pathlib.Path(os.environ['TEMP'], 'base.txt').write_text(RAW_BASE)\n"
        "print('[install] verify passed.')\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    raw = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code = render_remote(["s"], False, raw=raw)
        env = dict(os.environ, HERMES_HOME=str(home), TEMP=str(tmp_path))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    finally:
        httpd.shutdown()
    rep = json.loads(p.stdout.strip().splitlines()[-1])
    assert not rep.get("error"), rep
    assert (tmp_path / "base.txt").read_text() == raw


def test_end_to_end_an_unpinnable_installer_is_refused_not_run(tmp_path):
    srv = tmp_path / "srv"
    srv.mkdir()
    (srv / "install.py").write_text(
        "import os, pathlib\n"
        "pathlib.Path(os.environ['TEMP'], 'ran.txt').write_text('yes')\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code = render_remote(["s"], False, raw=f"http://127.0.0.1:{httpd.server_address[1]}")
        env = dict(os.environ, HERMES_HOME=str(home), TEMP=str(tmp_path))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    finally:
        httpd.shutdown()
    rep = json.loads(p.stdout.strip().splitlines()[-1])
    assert "RAW_BASE" in rep.get("error", "")
    assert not (tmp_path / "ran.txt").exists()
