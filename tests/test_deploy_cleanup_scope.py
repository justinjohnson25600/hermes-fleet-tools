#!/usr/bin/env python3
"""A fleet deploy removes only the stale copies its own installer left, never another owner's.

2026-10: the stale-copy cleanup walked the whole Hermes home for `<skill>/SKILL.md` and deleted
every match outside profiles/ and backups/. The Hermes install tree lives inside the home, so when
an estate skill shared a name with a skill Hermes bundles, the bundled copy was deleted from the
install's git tree and the fleet updater then refused those nodes as "unreviewed local changes".
Other owners inside the home: the Hermes install (hermes-agent/), plugins (plugins/*/skills/), the
skill curator (skills/.archive/), profiles and backups.

    python3 -m pytest tests/test_deploy_cleanup_scope.py -q
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

from fleettools.deploy import content_hash, render_remote  # noqa: E402

PUBLISHED = b"---\nname: s\nversion: 0.0.2\n---\nDo the thing well.\n"
OLD = b"---\nname: s\nversion: 0.0.1\n---\nDo the thing.\n"
BUNDLED = b"---\nname: s\nversion: 2.5.1\n---\nThe Hermes bundled skill of the same name.\n"
KNOWN = {"s": sorted({content_hash(PUBLISHED), content_hash(OLD)})}
CANON = Path("skills/dev/s")
STALE = Path("skills/old-category/s")       # sorts after CANON: the canonical choice is unambiguous


def put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _serve(directory: Path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def run_payload(tmp_path: Path, home: Path) -> dict:
    """The real payload a node runs, with an installer that changes nothing."""
    srv = tmp_path / "srv"
    put(srv / "install.py", b'RAW_BASE = "https://raw.example/main"\nimport sys\nsys.exit(0)\n')
    httpd = _serve(srv)
    try:
        code = render_remote(["s"], False, raw=f"http://127.0.0.1:{httpd.server_address[1]}", known=KNOWN)
        env = dict(os.environ, HERMES_HOME=str(home), TEMP=str(tmp_path))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    finally:
        httpd.shutdown()
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_deploy_never_touches_trees_other_owners_manage(tmp_path):
    home = tmp_path / "home"
    put(home / CANON / "SKILL.md", PUBLISHED)
    others = [
        home / "hermes-agent/skills/creative/s/SKILL.md",                 # Hermes install tree
        home / "hermes-agent/optional-skills/creative/s/SKILL.md",
        home / "plugins/some-plugin/skills/s/SKILL.md",                   # plugin-provided skill
    ]
    for f in others:
        put(f, BUNDLED)
    put(home / STALE / "SKILL.md", OLD)                                   # our own leftover category copy
    rep = run_payload(tmp_path, home)
    for f in others:
        assert f.is_file() and f.read_bytes() == BUNDLED, f"deploy deleted another owner's copy: {f}"
    assert not (home / STALE).exists()
    assert (home / CANON / "SKILL.md").read_bytes() == PUBLISHED
    assert rep["cleaned"] == [str(home / STALE / "SKILL.md")]


def test_a_stale_copy_holding_local_work_is_held_and_reported_not_deleted(tmp_path):
    home = tmp_path / "home"
    put(home / CANON / "SKILL.md", PUBLISHED)
    put(home / STALE / "SKILL.md", OLD + b"\nhoned here, never published\n")
    put(home / "s/SKILL.md", OLD)                                         # pre-split top-level leftover
    rep = run_payload(tmp_path, home)
    assert (home / STALE / "SKILL.md").read_bytes().endswith(b"never published\n")
    assert [h["where"] for h in rep["held"] if h["skill"] == "s"] == [str(home / STALE)]
    assert not (home / "s").exists()


def test_an_archived_copy_is_never_the_canonical_install_or_the_seed_source(tmp_path):
    home = tmp_path / "home"
    put(home / "skills/.archive/s/SKILL.md", OLD)                         # curator archive (flat layout)
    put(home / CANON / "SKILL.md", PUBLISHED)
    prof = home / "profiles/p"
    put(prof / "config.yaml", b"model: {}\n")
    run_payload(tmp_path, home)
    assert (home / "skills/.archive/s/SKILL.md").read_bytes() == OLD
    assert (home / CANON / "SKILL.md").read_bytes() == PUBLISHED
    assert (prof / CANON / "SKILL.md").read_bytes() == PUBLISHED
    assert not (prof / "skills/.archive").exists()
    from fleettools.deploy import canonical_dir
    assert canonical_dir(home, "s") == home / CANON
