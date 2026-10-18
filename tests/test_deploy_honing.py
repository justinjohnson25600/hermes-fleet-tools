#!/usr/bin/env python3
"""Agents hone their own skills; a fleet deploy keeps that work and reports it.

Owner decision 2026-10-18: every agent may locally hone its skills. Before this, deploy.py
replaced any profile copy whose SKILL.md differed from the canonical install, and install.py
rewrote the canonical copy, so a `skill_manage` patch an agent made in its own profile was erased
by the next deploy. Now a copy made only of content the skills repo once published is replaced
(an old version), and a copy holding anything else is HELD and reported.

    python3 -m pytest tests/test_deploy_honing.py -q
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.deploy import (  # noqa: E402
    canonical_edits, content_hash, local_edits, managed_files, published_hashes, render_remote,
    seed_profiles,
)

V1 = b"---\nname: s\nversion: 0.0.1\n---\nDo the thing.\n"
V2 = b"---\nname: s\nversion: 0.0.2\n---\nDo the thing well.\n"
REF = b"# reference\n"
KNOWN = {content_hash(V1), content_hash(V2), content_hash(REF)}


def put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def home_with_canon(tmp_path: Path, body: bytes = V2) -> Path:
    home = tmp_path / "home"
    put(home / "skills" / "dev" / "s" / "SKILL.md", body)
    put(home / "skills" / "dev" / "s" / "references" / "r.md", REF)
    return home


def profile(home: Path, name: str, skill_body: bytes | None, *, identity: bool = True) -> Path:
    prof = home / "profiles" / name
    prof.mkdir(parents=True)
    if identity:
        put(prof / "config.yaml", b"model: {}\n")
    if skill_body is not None:
        put(prof / "skills" / "dev" / "s" / "SKILL.md", skill_body)
    return prof


def test_a_published_version_is_untouched_even_with_windows_line_endings(tmp_path):
    f = put(tmp_path / "SKILL.md", V1.replace(b"\n", b"\r\n"))
    assert local_edits([f], KNOWN) == []


def test_an_edited_or_added_file_is_local_honing(tmp_path):
    d = tmp_path / "copy"
    put(d / "SKILL.md", V2 + b"\nPitfall learned on this box.\n")
    put(d / "references" / "mine.md", b"agent notes\n")
    put(d / ".hub" / "meta.json", b"{}")                     # dot-entries are not skill content
    put(d / "__pycache__" / "x.pyc", b"\x00")
    assert sorted(Path(p).name for p in local_edits(managed_files(d), KNOWN)) == ["SKILL.md", "mine.md"]


def test_without_published_history_every_differing_copy_is_held(tmp_path):
    f = put(tmp_path / "SKILL.md", V1)
    assert local_edits([f], set()) == [str(f)]


def test_seeding_keeps_a_honed_copy_and_replaces_an_old_published_one(tmp_path):
    home = home_with_canon(tmp_path)
    honed = V1 + b"\nOur client wants British spelling.\n"
    profile(home, "honed", honed)
    profile(home, "stale", V1)
    profile(home, "fresh", None)
    r = seed_profiles(home, "s", KNOWN)
    assert (home / "profiles/honed/skills/dev/s/SKILL.md").read_bytes() == honed
    assert [h["where"] for h in r["held"]] == [str(home / "profiles/honed/skills/dev/s")]
    for name in ("stale", "fresh"):
        assert (home / f"profiles/{name}/skills/dev/s/SKILL.md").read_bytes() == V2
        assert (home / f"profiles/{name}/skills/dev/s/references/r.md").read_bytes() == REF


def test_replace_honed_overwrites_only_when_asked(tmp_path):
    home = home_with_canon(tmp_path)
    profile(home, "honed", V1 + b"\nlocal\n")
    r = seed_profiles(home, "s", KNOWN, replace=True)
    assert r["held"] == []
    assert (home / "profiles/honed/skills/dev/s/SKILL.md").read_bytes() == V2


def test_tombstoned_identityless_and_opted_out_dirs_are_never_seeded(tmp_path):
    home = home_with_canon(tmp_path)
    profile(home, "deleted", None)
    put(home / "profiles" / ".deleted" / "deleted", b"deleted\n")
    profile(home, "ghost", None, identity=False)
    put(profile(home, "client", None) / ".no-bundled-skills", b"")
    seed_profiles(home, "s", KNOWN)
    for name in ("deleted", "ghost", "client"):
        assert not (home / f"profiles/{name}/skills").exists(), name


def test_canonical_edits_cover_declared_state_files(tmp_path):
    home = home_with_canon(tmp_path)
    put(home / "devpair" / "devpair.py", b"print('patched on this box')\n")
    put(home / "devpair" / "config.json", b"{}")                 # machine-local, not declared
    edits = canonical_edits(home, "s", KNOWN, ("devpair", ["devpair.py", "test_devpair.py"]))
    assert [Path(e).name for e in edits] == ["devpair.py"]


GIT = shutil.which("git")


@pytest.mark.skipif(GIT is None, reason="git not installed")
def test_published_hashes_cover_every_version_in_history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    g = functools.partial(subprocess.run, check=True, capture_output=True, cwd=repo)
    g([GIT, "init", "-q"])
    g([GIT, "config", "user.email", "t@example.invalid"])
    g([GIT, "config", "user.name", "t"])
    for body in (V1, V2):
        put(repo / "s" / "SKILL.md", body)
        g([GIT, "add", "-A"])
        g([GIT, "commit", "-q", "-m", "v"])
    known = set(published_hashes(repo, ["s"])["s"])
    assert {content_hash(V1), content_hash(V2)} <= known
    assert content_hash(V2 + b"local") not in known


def _serve(directory: Path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_end_to_end_a_honed_canonical_install_is_not_reinstalled_or_propagated(tmp_path):
    """The real payload against a fake installer that would overwrite the canonical copy."""
    srv = tmp_path / "srv"
    put(srv / "install.py",
        b"import os, pathlib, sys\n"
        b"h = pathlib.Path(os.environ['HERMES_HOME'])\n"
        b"(h / 'skills/dev/s/SKILL.md').write_bytes(" + repr(V2).encode() + b")\n"
        b"(h / 'installer-ran').write_text('yes')\n")
    home = home_with_canon(tmp_path, body=V1 + b"\nhoned on this box\n")
    profile(home, "p", V1)
    httpd = _serve(srv)
    try:
        code = render_remote(["s"], False, raw=f"http://127.0.0.1:{httpd.server_address[1]}",
                             known={"s": sorted(KNOWN)})
        env = dict(os.environ, HERMES_HOME=str(home), TEMP=str(tmp_path))
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                           timeout=120)
    finally:
        httpd.shutdown()
    rep = json.loads(p.stdout.strip().splitlines()[-1])
    assert not (home / "installer-ran").exists()
    assert (home / "skills/dev/s/SKILL.md").read_bytes().endswith(b"honed on this box\n")
    assert (home / "profiles/p/skills/dev/s/SKILL.md").read_bytes() == V1   # not overwritten by honed canon
    assert rep["skills"]["s"]["held"] is True
    assert rep["held"][0]["where"] == "canonical"


def test_all_reads_the_skill_list_from_the_skills_repo(monkeypatch, capsys, tmp_path):
    import fleettools.deploy as d
    repo = tmp_path / "skills-repo"
    put(repo / "skills.json", b'["alpha", "beta"]')
    seen = []
    monkeypatch.setattr(d, "load_agents", lambda: {"agents": [
        {"name": "a", "platform": "windows", "enabled": True, "host": "u@a", "role": "worker"}]})
    monkeypatch.setattr(d, "deliver", lambda a, skills, dry: (seen.append(list(skills)), (a["name"], {"skills": {}}))[1])
    monkeypatch.setattr(sys, "argv", ["deploy.py", "--all", "--no-wait", "--skills-repo", str(repo)])
    assert d.main() == 0
    assert seen == [["alpha", "beta"]]
