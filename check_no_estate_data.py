#!/usr/bin/env python3
"""Refuse to publish estate identifiers.

The public repo exists to be shared. Its value depends on containing no
usernames, IP addresses, hardware serials, machine names, or provider IDs from
the private estate. A one-off manual check rots the first time someone copies a
file across in a hurry, so this runs as a pre-commit hook.

Patterns are shapes, not a blocklist of known-bad strings: a blocklist only
catches the identifiers someone remembered to add.

    python3 check_no_estate_data.py [path]      # exit 1 if anything matches
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# (name, pattern, why it matters)
PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("tailnet IP", re.compile(r"\b100\.(?:[6-9]\d|1[0-2]\d)\.\d{1,3}\.\d{1,3}\b"),
     "Tailscale CGNAT address identifies a real machine"),
    ("private LAN IP", re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
     "internal network layout"),
    ("public IP", re.compile(r"\b(?:80|81|82)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
     "site's public address"),
    ("windows COMPUTERNAME", re.compile(r"\bDESKTOP-[A-Z0-9]{7}\b"),
     "real machine name"),
    ("HP/Dell serial", re.compile(r"\b(?:8CG|CND|MXL|5CG)[0-9A-Z]{6,7}\b"),
     "hardware serial identifies a physical unit"),
    ("windows user path", re.compile(r"C:\\+Users\\+(?!<|USER|%)[A-Za-z][A-Za-z0-9_.-]{1,}"),
     "embeds a real account name"),
    ("unix home path", re.compile(r"/(?:Users|home)/(?!<|USER)[a-z][a-z0-9_.-]{2,}"),
     "embeds a real account name"),
    ("ssh target", re.compile(r"\b[a-z][a-z0-9_.-]{1,}@(?:\d{1,3}\.){3}\d{1,3}\b"),
     "user@host credential pair"),
    ("machine GUID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
     "MachineGuid or similar unique install ID"),
    ("private provider id", re.compile(r"\bzai-indirect\b|\blmstudio_r\b|\bkimi-coding\b"),
     "proprietary routing detail"),
]

# Placeholders the examples are supposed to contain.
ALLOW = re.compile(
    r"USER@|YOUR-ORG|10\.0\.0\.|%LOCALAPPDATA%|PATH-TO-|<[a-z-]+>|"
    r"example\.com|/Users/USER|C:\\Users\\USER|0{8}-0{4}",
    re.I,
)

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".txt", ".sh", ".toml", ".cfg", ""}


def scan(root: Path) -> list[tuple[Path, int, str, str, str]]:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(p in SKIP_DIRS for p in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.name == Path(__file__).name:
            continue  # this file necessarily contains the patterns
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            for label, rx, why in PATTERNS:
                m = rx.search(line)
                if m and not ALLOW.search(line):
                    findings.append((path.relative_to(root), n, label, m.group(0), why))
    return findings


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings = scan(root)
    if not findings:
        print(f"clean: no estate identifiers found under {root.name}/")
        return 0
    print(f"{len(findings)} estate identifier(s) would be published:\n")
    for rel, n, label, hit, why in findings:
        print(f"  {rel}:{n}")
        print(f"    {label}: {hit!r} — {why}")
    print("\nReplace with a placeholder (USER@, 10.0.0.x, %LOCALAPPDATA%, YOUR-ORG)")
    print("or move the file to the private repo.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
