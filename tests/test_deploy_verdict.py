#!/usr/bin/env python3
"""A box is OK only when every delivered skill installed AND passed its own verify suite.

Before this, the per-agent verdict read only transport errors and dev-pair's suite: a skill whose
install.py exited 1 ("VERIFY FAILED - the skill is installed but not proven working") still printed
OK with its version, and the OK line never showed the delivered skill's own check count.

    python3 -m pytest tests/test_deploy_verdict.py -q
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.deploy import REMOTE, format_versions, skill_failures, verify_counts  # noqa: E402


def report(**skills) -> dict:
    return {"skills": skills, "tests": {"passed": 590, "failed": 0}}


def test_clean_install_with_passing_verify_is_ok_and_shows_the_count():
    r = report(**{"senior-dev-prompt": {"rc": 0, "err": "", "version": "0.0.1",
                                        "verify": {"passed": 186, "failed": 0}}})
    assert skill_failures(r) == []
    assert format_versions(r) == "senior-dev-prompt=0.0.1 (checks 186/186)"


def test_install_exit_1_fails_the_box_even_though_the_version_landed():
    r = report(**{"senior-dev-prompt": {"rc": 1, "err": "VERIFY FAILED", "version": "0.0.1",
                                        "verify": {"passed": 185, "failed": 1}}})
    msgs = skill_failures(r)
    assert len(msgs) == 1
    assert "senior-dev-prompt" in msgs[0] and "185/186" in msgs[0]


def test_failed_checks_fail_the_box_even_with_exit_0():
    r = report(**{"x": {"rc": 0, "err": "", "version": "1.0.0", "verify": {"passed": 9, "failed": 1}}})
    assert skill_failures(r) and "9/10" in skill_failures(r)[0]


def test_install_error_without_a_suite_still_fails_and_quotes_the_error():
    r = report(**{"y": {"rc": 1, "err": "no such skill: y", "version": None}})
    assert skill_failures(r) and "no such skill" in skill_failures(r)[0]


def test_markdown_only_skill_without_a_suite_is_ok_and_says_so():
    r = report(**{"z": {"rc": 0, "err": "", "version": "0.0.2"}})
    assert skill_failures(r) == []
    assert format_versions(r) == "z=0.0.2"


def test_dry_run_entries_are_not_failures():
    assert skill_failures(report(**{"z": {"dry_run": True}})) == []


def test_other_copies_are_still_reported():
    r = report(**{"d": {"rc": 0, "err": "", "version": "1.2.4", "other_versions": ["1.2.2"],
                        "verify": {"passed": 590, "failed": 0}}})
    assert format_versions(r) == "d=1.2.4 (checks 590/590) (other copies: 1.2.2)"


def test_verify_counts_reads_the_installer_summary_line():
    out = "[install] verifying: test_scripts.py\n[install]   186 passed, 0 failed\n[install] verify passed.\n"
    assert verify_counts(out) == {"passed": 186, "failed": 0}
    assert verify_counts("[install] markdown-only skill: nothing to verify\n") is None


def test_the_remote_script_records_the_verify_counts():
    # REMOTE runs on each box; it must carry the parser and store its result per skill.
    code = REMOTE % {"raw": "https://example.invalid", "skills": ["s"], "dry": False}
    compile(code, "<remote>", "exec")
    assert '["verify"]' in code


def test_main_fails_the_box_whose_skill_failed_verify(monkeypatch, capsys):
    # Drives the real verdict loop in main(), not just the helpers: the wiring is what broke.
    import fleettools.deploy as d
    agents = [{"name": n, "platform": "windows", "enabled": True, "host": f"u@{n}", "role": "worker"}
              for n in ("box-a", "box-b")]
    reports = {
        "box-a": {"serial": "A", "tests": {"passed": 590, "failed": 0},
                  "skills": {"s": {"rc": 1, "err": "VERIFY FAILED", "version": "0.0.1",
                                   "verify": {"passed": 185, "failed": 1}}}},
        "box-b": {"serial": "B", "tests": {"passed": 590, "failed": 0},
                  "skills": {"s": {"rc": 0, "err": "", "version": "0.0.1",
                                   "verify": {"passed": 186, "failed": 0}}}},
    }
    monkeypatch.setattr(d, "load_agents", lambda: {"agents": agents})
    monkeypatch.setattr(d, "deliver", lambda a, skills, dry: (a["name"], reports[a["name"]]))
    monkeypatch.setattr(sys, "argv", ["deploy.py", "s", "--no-wait"])
    rc = d.main()
    out = capsys.readouterr().out
    assert "FAIL box-a: s: installer exited 1 (checks 185/186)" in out
    assert "OK   box-a" not in out
    assert "OK   box-b" in out and "s=0.0.1 (checks 186/186)" in out
    assert rc != 0


def _run_main(monkeypatch, capsys, suites):
    """Drive main() over Windows boxes whose dev-pair suite results are `suites` {name: (passed, failed)}."""
    import fleettools.deploy as d
    agents = [{"name": n, "platform": "windows", "enabled": True, "host": f"u@{n}", "role": "worker"}
              for n in suites]
    reports = {n: {"serial": n.upper(), "tests": {"passed": p, "failed": f},
                   "skills": {"s": {"rc": 0, "err": "", "version": "0.0.1", "verify": {"passed": 9, "failed": 0}}}}
               for n, (p, f) in suites.items()}
    monkeypatch.setattr(d, "load_agents", lambda: {"agents": agents})
    monkeypatch.setattr(d, "deliver", lambda a, skills, dry: (a["name"], reports[a["name"]]))
    monkeypatch.setattr(sys, "argv", ["deploy.py", "s", "--no-wait"])
    rc = d.main()
    return rc, capsys.readouterr().out


def test_a_failed_devpair_check_is_a_failure_not_ok_and_not_a_skip(monkeypatch, capsys):
    # 589 passed + 1 failed = 590 run: nothing was skipped, one check failed. The old verdict printed
    # this box as OK and warned "checks are being skipped", which sent the reader the wrong way.
    rc, out = _run_main(monkeypatch, capsys, {"box-a": (589, 1), "box-b": (590, 0), "box-c": (590, 0)})
    assert "FAIL box-a: dev-pair suite: 1 check(s) failed (589/590)" in out
    assert "OK   box-a" not in out
    assert "box-a: 589 checks vs 590" not in out
    assert "OK   box-b" in out
    assert rc != 0


def test_a_real_skip_still_warns_against_same_platform_peers(monkeypatch, capsys):
    rc, out = _run_main(monkeypatch, capsys, {"box-a": (559, 0), "box-b": (590, 0)})
    assert "WARN box-a: 559 checks vs 590 on the same platform" in out
    assert "checks are being skipped" in out
    assert rc != 0


def test_a_clean_fleet_exits_zero(monkeypatch, capsys):
    rc, out = _run_main(monkeypatch, capsys, {"box-a": (590, 0), "box-b": (590, 0)})
    assert "FAIL" not in out and "WARN" not in out
    assert rc == 0


def test_remote_script_end_to_end_reads_the_installers_stdout(tmp_path):
    # install.py logs to stdout; a remote script that read only stderr would lose every count.
    # Serve a fake installer over local HTTP and run REMOTE in a sandboxed Hermes home.
    srv = tmp_path / "srv"
    srv.mkdir()
    (srv / "install.py").write_text(
        "import sys\n"
        "print('[install] verifying: test_x.py')\n"
        "print('[install]   7 passed, 1 failed')\n"
        "print('[install] VERIFY FAILED - the skill is installed but not proven working.')\n"
        "sys.exit(1)\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(srv))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code = REMOTE % {"raw": f"http://127.0.0.1:{httpd.server_address[1]}", "skills": ["s"], "dry": False}
        env = dict(os.environ, HERMES_HOME=str(home), TEMP=str(tmp_path))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    finally:
        httpd.shutdown()
    rep = json.loads(p.stdout.strip().splitlines()[-1])
    s = rep["skills"]["s"]
    assert s["rc"] == 1
    assert s["verify"] == {"passed": 7, "failed": 1}
    assert "VERIFY FAILED" in s["err"]
    assert rep["home"] == str(home)
