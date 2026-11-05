"""The CDN wait targets the skills repo's published versions, so a stale CDN cannot pass it.

2026-11-05: right after estate-experiments 0.3.2 was pushed, deploy.py had no local skill.json
(the skills live in a separate repo), fell back to "two CDN reads agree", accepted the stale
0.3.1 at once, and failed five boxes that had correctly installed 0.3.2 as 'expected 0.3.1'."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleettools.deploy as d  # noqa: E402

GIT = shutil.which("git")


def _skills_repo(tmp_path: Path, version: str) -> Path:
    repo = tmp_path / "skills-repo"
    (repo / "demo").mkdir(parents=True)
    (repo / "demo" / "skill.json").write_text(json.dumps({"name": "demo", "version": version}), encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def _cdn(serving: list[str], monkeypatch) -> list[str]:
    """A CDN that serves the given versions in turn (last one repeats); records each read."""
    reads = []

    def urlopen(url, timeout=30):
        v = serving[min(len(reads), len(serving) - 1)]
        reads.append(v)
        return io.BytesIO(json.dumps({"version": v}).encode())

    monkeypatch.setattr(d.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    return reads


@pytest.mark.skipif(GIT is None, reason="git not installed")
def test_published_versions_reads_the_repo(tmp_path):
    assert d.published_versions(_skills_repo(tmp_path, "0.3.2"), ["demo", "absent"]) == {"demo": "0.3.2"}


def test_stale_cdn_is_not_current_when_the_published_version_is_known(monkeypatch):
    _cdn(["0.3.1"], monkeypatch)
    r = d.wait_for_cdn(["demo"], timeout=0, published={"demo": "0.3.2"})
    assert r["current"] is False and r["want"] == {"demo": "0.3.2"} and r["serving"] == {"demo": "0.3.1"}


def test_wait_ends_when_the_cdn_catches_up(monkeypatch):
    reads = _cdn(["0.3.1", "0.3.1", "0.3.2"], monkeypatch)
    r = d.wait_for_cdn(["demo"], timeout=600, published={"demo": "0.3.2"})
    assert r["current"] is True and r["serving"] == {"demo": "0.3.2"} and len(reads) == 3
