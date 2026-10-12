#!/usr/bin/env python3
"""The estate-data guard must fail closed.

A guard that reports "clean" after scanning nothing is worse than no guard: `--staged` (a flag the
script never had) was read as a directory name, the directory did not exist, rglob found no files,
and the script printed "clean". These contracts pin the fix: unknown flags and missing paths are
usage errors (exit 2), a real finding still exits 1, and a clean tree still exits 0.

    python3 -m pytest tests/test_check_no_estate_data.py -q
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "check_no_estate_data.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(GUARD), *args], capture_output=True, text=True, timeout=60)


def test_unknown_flag_is_a_usage_error(tmp_path):
    r = run("--staged")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "clean" not in r.stdout


def test_missing_path_is_a_usage_error(tmp_path):
    r = run(str(tmp_path / "does-not-exist"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "clean" not in r.stdout


def test_finding_exits_1(tmp_path):
    ip = ".".join(["100", "101", "7", "9"])  # built at run time so this file stays clean
    (tmp_path / "notes.md").write_text(f"ssh to {ip}\n", encoding="utf-8")
    r = run(str(tmp_path))
    assert r.returncode == 1, r.stdout + r.stderr


def test_clean_tree_exits_0_and_counts_files(tmp_path):
    (tmp_path / "notes.md").write_text("nothing private here\n", encoding="utf-8")
    r = run(str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 file" in r.stdout, r.stdout


if __name__ == "__main__":
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                try:
                    fn(Path(d))
                    print(f"PASS {name}")
                except AssertionError as e:
                    failures += 1
                    print(f"FAIL {name}: {e}")
    print(f"{failures} failed")
    sys.exit(1 if failures else 0)
