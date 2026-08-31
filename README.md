# hermes-fleet-tools

Generic tooling for maintaining a fleet of [Hermes](https://github.com/NousResearch/hermes) agents across many machines. No orchestrator, no shared state, no agent-to-agent traffic: one control box pushes to a list of hosts over SSH and reads back what actually happened.

Extracted from a working seven-machine estate. This repo is deliberately free of estate-specific data so it can be shared; a pre-commit hook enforces that.

## Why this exists

Ad-hoc SSH loops do not survive contact with a real fleet. The failures that motivated each piece of this tooling were all silent:

- A skill installed fine on every box and **17 of its tests were quietly skipped** on Windows, because a path was hardcoded POSIX. The suite still printed `0 failed`.
- A CDN served a **stale release for minutes** after a push, so machines installed the previous version and reported success.
- Two machines shared a `COMPUTERNAME` because they were **cloned from one image**, so anything keying on hostname treated them as the same box.
- Per-profile directories accumulated **stale duplicate copies** of a skill, and the agent loaded one of those instead of the canonical one.

Every one of those reported green. The tooling here exists to make them report red.

## What is in the box

| File | Purpose |
|---|---|
| `fleettools/deploy.py` | Deliver skills to every enabled agent in parallel; verify versions and test counts |
| `fleettools/agents.example.json` | Roster format — copy to `agents.json` and fill in |
| `check_no_estate_data.py` | Pre-commit guard: refuses to publish usernames, IPs, serials, machine names |

## Quick start

```bash
cp fleettools/agents.example.json agents.json
$EDITOR agents.json                      # fill in real hosts
export FLEET_SKILLS_RAW=https://raw.githubusercontent.com/YOUR-ORG/YOUR-SKILLS-REPO/main

python3 fleettools/deploy.py --list      # who would receive, and what blocks the rest
python3 fleettools/deploy.py --dry-run   # plan only, changes nothing
python3 fleettools/deploy.py my-skill    # deliver
```

## The roster

Adding a machine is a **data edit**, not a code change:

```json
{
  "name": "worker-03",
  "host": "USER@10.0.0.3",
  "platform": "windows",
  "hermes_home": "%LOCALAPPDATA%\\hermes",
  "enabled": true,
  "role": "worker",
  "python": "PATH-TO-PYTHON"
}
```

| Field | Meaning |
|---|---|
| `host` | `user@host` for SSH, or `local` for the control box itself |
| `hermes_home` | Windows installs use `%LOCALAPPDATA%\hermes`, not `~/.hermes` |
| `python` | Optional. Pin an interpreter when the box has no usable `python` on `PATH` |
| `enabled` | `false` keeps an unreachable machine **visible** rather than forgotten |
| `blocked_reason` | Required when disabled. Printed by `--list`, so the blocker stays in view |

That last pair matters more than it looks. A machine you cannot reach is not the same as a machine that does not exist, and deleting it from the roster is how it gets silently dropped from your fleet.

## What `deploy.py` refuses to let you believe

**A push is not a release.** `raw.githubusercontent.com` serves a cached copy for minutes afterwards. `deploy.py` polls until the CDN serves the version your repo has, so a deploy cannot race the cache and install the previous one.

**`0 failed` is not a passing suite.** Every agent's *check count* is compared against the highest seen. A machine reporting fewer checks is **skipping** them, which no pass/fail line reveals. A lower count fails the run.

**Hostname is not identity.** Imaged Windows boxes share a `COMPUTERNAME` and a `MachineGuid`. Identity is keyed on BIOS serial, with unfilled SMBIOS placeholders (`Default string`, `To be filled by O.E.M.`) normalised away so unrelated cheap hardware does not collide.

**The canonical copy is not the only copy.** Stale duplicates under per-profile directories are removed on every run.

## Windows portability traps

Each of these cost a release. All were invisible on macOS:

- **`pathlib.rglob` raises `WinError 448`** on untraversable reparse points, and npm workspace installs create junctions under `node_modules`. One junction aborts the entire walk. Use `os.walk` with those branches pruned.
- **Emptying `PATH` does not hide a binary.** `CreateProcess` searches the launching executable's own directory first. A test that hides a CLI this way will silently exercise the wrong code path.
- **A bare command name never consults `PATHEXT`.** `CreateProcess` only appends `.exe`, so a `.cmd`/`.bat` shim on `PATH` is invisible. Use `shutil.which`.
- **`cmd.exe` truncates long command lines.** Ship payloads to a temp file and execute that.
- **`write_text()` without `encoding=`** uses the locale codepage (`cp1252`), so any non-ASCII character in generated Python produces a file the interpreter refuses to parse.
- **Some boxes have no real Python.** `python.exe` resolves to a Microsoft Store alias stub that prints an advert and exits 9009. Pin the interpreter per agent.

## Publishing safely

```bash
python3 check_no_estate_data.py .
```

Matches *shapes* — tailnet and LAN addresses, `DESKTOP-XXXXXXX` names, HP/Dell serial formats, `user@ip` pairs, machine GUIDs, real home paths — rather than a blocklist of known strings, because a blocklist only catches identifiers someone remembered to add. Install as a hook:

```bash
ln -sf ../../check_no_estate_data.py .git/hooks/pre-commit
```

## Requirements

Python 3.9+, an SSH client, and key-based access to each machine. No agent-side install: the payload is shipped per run.

### SSH diagnosis

`Permission denied` covers two completely different faults. Run `ssh -v` and look for `Server accepts key`:

- **Line present** — the key is authorised and the **username** is wrong.
- **Line absent for every username** — the **key** was never installed.

Two different fixes, indistinguishable from the error message alone.

## Licence

MIT
