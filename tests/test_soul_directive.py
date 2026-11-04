"""The SOUL.md directive payload, run for real against a temporary Hermes home.

Contracts: user text outside the managed block is never altered; the block appears exactly once;
re-running is a no-op; only homes that have the skill get it; a missing SOUL.md is never created;
CRLF/BOM survive; malformed markers leave the file untouched; dry runs write nothing."""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fleettools.soul_directive as sd

SKILL = sd.SKILL
BEGIN = f"<!-- fleet-directive:{SKILL} begin"


def _skill(home: Path) -> None:
    d = home / "skills" / "autonomous-ai-agents" / SKILL
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: %s\nversion: 0.2.0\n---\n" % SKILL, encoding="utf-8")


def _profile(home: Path, name: str, soul: bytes | None, skill: bool = True) -> Path:
    p = home / "profiles" / name
    p.mkdir(parents=True)
    (p / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    if soul is not None:
        (p / "SOUL.md").write_bytes(soul)
    if skill:
        _skill(p)
    return p


def _run(home: Path, apply: bool) -> dict:
    env = dict(os.environ, HERMES_HOME=str(home))
    out = subprocess.run([sys.executable, "-c", sd.render(apply)], env=env, capture_output=True,
                         text=True, timeout=120, check=True).stdout
    rep = json.loads(out)
    return {p["profile"]: p for p in rep["profiles"]}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "SOUL.md").write_text("You are the default agent.\n", encoding="utf-8")
    _skill(home)
    return home


def test_directive_added_once_and_user_text_preserved(tmp_path):
    home = _home(tmp_path)
    prof = _profile(home, "client", b"# Persona\n\nBe kind.")
    ino = os.stat(prof / "SOUL.md").st_ino
    first = _run(home, apply=True)
    assert first["default"]["status"] == first["client"]["status"] == "added"
    # Rewritten in place: the same file object keeps its owner and ACL (Windows elevated-SSH trap).
    assert os.stat(prof / "SOUL.md").st_ino == ino
    for soul, original in ((home / "SOUL.md", "You are the default agent.\n"), (prof / "SOUL.md", "# Persona\n\nBe kind.")):
        text = soul.read_text(encoding="utf-8")
        assert text.startswith(original.rstrip("\n"))
        assert text.count(BEGIN) == 1 and f"load the `{SKILL}` skill" in text
    snapshot = (prof / "SOUL.md").read_bytes()
    again = _run(home, apply=True)
    assert again["client"]["status"] == "current" and (prof / "SOUL.md").read_bytes() == snapshot
    backups = list((home / "backups" / "soul-directive" / "client").iterdir())
    assert [b.read_bytes() for b in backups] == [b"# Persona\n\nBe kind."]


def test_only_homes_with_the_skill_and_a_soul_are_touched(tmp_path):
    home = _home(tmp_path)
    no_skill = _profile(home, "noskill", b"keep me\n", skill=False)
    opted = _profile(home, "optout", b"keep me too\n")
    (opted / ".no-bundled-skills").write_text("", encoding="utf-8")
    no_soul = _profile(home, "nosoul", None)
    rep = _run(home, apply=True)
    assert rep["noskill"]["status"] == "skip" and (no_skill / "SOUL.md").read_bytes() == b"keep me\n"
    assert rep["optout"]["status"] == "skip" and (opted / "SOUL.md").read_bytes() == b"keep me too\n"
    assert rep["nosoul"]["status"] == "warn" and not (no_soul / "SOUL.md").exists()


def test_flat_hub_copy_counts_and_archived_copy_does_not(tmp_path):
    home = _home(tmp_path)
    flat = _profile(home, "flat", b"Flat persona\n", skill=False)
    (flat / "skills" / SKILL).mkdir(parents=True)
    (flat / "skills" / SKILL / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    archived = _profile(home, "archived", b"Archived persona\n", skill=False)
    (archived / "skills" / ".archive" / SKILL).mkdir(parents=True)
    (archived / "skills" / ".archive" / SKILL / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    rep = _run(home, apply=True)
    assert rep["flat"]["status"] == "added" and BEGIN in (flat / "SOUL.md").read_text(encoding="utf-8")
    assert rep["archived"]["status"] == "skip" and (archived / "SOUL.md").read_bytes() == b"Archived persona\n"


def test_crlf_and_bom_survive_and_changed_block_is_replaced_in_place(tmp_path):
    home = _home(tmp_path)
    old = (f"\ufeffHead\r\n\r\n{BEGIN} (managed by FleetOps; this block is rewritten) -->\r\nold text\r\n"
           f"<!-- fleet-directive:{SKILL} end -->\r\nTail\r\n").encode("utf-8")
    prof = _profile(home, "win", old)
    assert _run(home, apply=True)["win"]["status"] == "updated"
    raw = (prof / "SOUL.md").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfHead\r\n") and raw.endswith(b"Tail\r\n")
    assert b"old text" not in raw and b"\n" not in raw.replace(b"\r\n", b"")
    assert raw.decode("utf-8").count(BEGIN) == 1


def test_malformed_markers_leave_file_untouched(tmp_path):
    home = _home(tmp_path)
    cases = {
        "begin-only": f"Persona\n{BEGIN} -->\nno end marker\n",
        "end-only": f"Persona\n<!-- fleet-directive:{SKILL} end -->\n",
        "repeated": f"{BEGIN} -->\na\n<!-- fleet-directive:{SKILL} end -->\n{BEGIN} -->\nb\n<!-- fleet-directive:{SKILL} end -->\n",
    }
    profs = {name: _profile(home, name, text.encode("utf-8")) for name, text in cases.items()}
    rep = _run(home, apply=True)
    for name, text in cases.items():
        assert rep[name]["status"] == "error", name
        assert (profs[name] / "SOUL.md").read_bytes() == text.encode("utf-8"), name


def test_dry_run_writes_nothing(tmp_path):
    home = _home(tmp_path)
    prof = _profile(home, "client", b"Persona\n")
    rep = _run(home, apply=False)
    assert rep["client"]["status"] == rep["default"]["status"] == "would-add"
    assert (prof / "SOUL.md").read_bytes() == b"Persona\n" and not (home / "backups").exists()


def test_directive_block_passes_the_hermes_context_scanner_patterns():
    """The managed block must not trip Hermes' SOUL.md injection scanner (html_comment_injection et al.)."""
    import re
    block = sd.BLOCK.format(skill=SKILL)
    hermes = Path(os.environ.get("HERMES_SOURCE_ROOT", Path.home() / ".hermes" / "hermes-agent"))
    patterns = hermes / "tools" / "threat_patterns.py"
    if not patterns.is_file():
        import pytest
        pytest.skip("no Hermes source to read the scanner patterns from")
    sys.path.insert(0, str(hermes))
    from tools.threat_patterns import scan_for_threats  # noqa: E402
    assert scan_for_threats(block, scope="context") == []
    assert re.search(r"(?i)ignore|override|hidden|secret", block) is None
