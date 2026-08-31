#!/usr/bin/env python3
"""Report update-drift across a Hermes agent fleet. READ-ONLY.

    python3 fleet_report.py                 # human summary, all enabled agents
    python3 fleet_report.py --json          # machine-readable
    python3 fleet_report.py --quiet         # print ONLY changes and problems
    python3 fleet_report.py --workers 1     # serialise (rate-limit friendly)

Mutates nothing on any agent: it runs `--version`, `update --check`, and
read-only git plumbing. Installing updates is a separate, supervised operation.

Every behaviour below was forced by an observed failure on a real estate, not
by anticipation:

* **The CLI must be named by the roster, never discovered.** Globbing for
  `hermes*.exe` finds the ACP protocol binary, which *accepts* `update --check`
  and prints NOTHING. Six machines parsed as "nothing to update" and would have
  reported green indefinitely.
* **`update --check` emits prose, not JSON**, and at least four different
  shapes (available / rate-limited / not-a-repo / empty). Anything unrecognised
  is UNKNOWN, never CURRENT.
* **The version string does not track commits.** Machines sitting 1 and 140
  commits behind both report the same version. Identity is the HEAD hash.
* **`rev-list --count` lies on a shallow clone.** Two machines at a byte-identical
  HEAD, with a byte-identical origin/main, reported 140 and 1 commits behind: the
  second was a depth-1 clone with no merge-base. A shallow repository cannot
  answer "how far behind am I", and its apparent "ahead" commits are an artifact
  of the missing history, not local work.
* **Fetching from every agent at once gets the fleet rate-limited.** A single
  parallel sweep of seven machines drew HTTP 429 on three of them, so the
  default is deliberately slow, and 429 is its own verdict.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Remote payloads. Shipped base64/-EncodedCommand so no quoting survives three
# layers of shell, and so a long command line is never truncated by cmd.exe.
# --------------------------------------------------------------------------

PS_PROBE = r"""$ErrorActionPreference='SilentlyContinue'
$ProgressPreference='SilentlyContinue'
$cli = '@@CLI@@'
if (-not (Test-Path $cli)) { $cli = (Get-Command hermes -ErrorAction SilentlyContinue).Source }
Write-Output '===CLI==='; Write-Output $cli
Write-Output '===VER==='; if ($cli) { & $cli --version 2>&1 | Select-Object -First 1 }
Write-Output '===CHECK==='; if ($cli) { & $cli update --check 2>&1 | Select-Object -First 10 }
Write-Output '===GIT==='
# The roster stores repo paths in cmd.exe form (%LOCALAPPDATA%), which
# PowerShell does not expand -- it would test a literal path that never exists
# and report every agent as BLOCKED. Expand to the PowerShell equivalent here.
$repo = '@@REPO@@'
$repo = [System.Environment]::ExpandEnvironmentVariables($repo)
if (Test-Path $repo) {
  Push-Location $repo
  Write-Output ('HEAD='       + (git rev-parse HEAD 2>&1))
  Write-Output ('UPSTREAM='   + (git rev-parse '@{u}' 2>&1))
  Write-Output ('BRANCH='     + (git rev-parse --abbrev-ref HEAD 2>&1))
  Write-Output ('SHALLOW='    + (git rev-parse --is-shallow-repository 2>&1))
  Write-Output ('BEHIND='     + (git rev-list --count 'HEAD..@{u}' 2>&1))
  Write-Output ('AHEAD='      + (git rev-list --count '@{u}..HEAD' 2>&1))
  Write-Output ('MERGEBASE='  + (git merge-base HEAD '@{u}' 2>&1))
  Write-Output ('STASHES='    + ((git stash list 2>&1 | Measure-Object -Line).Lines))
  Write-Output ('DIRTY='      + ((git status --porcelain 2>&1 | Measure-Object -Line).Lines))
  Pop-Location
} else { Write-Output 'REPO_MISSING=1' }
Write-Output '===DESKTOP==='
Get-Process | Where-Object { $_.ProcessName -like '*ermes*' } |
  Select-Object -ExpandProperty Path -Unique
Write-Output '===END==='"""

SH_PROBE = r"""cli='@@CLI@@'
[ -x "$cli" ] || cli=$(command -v hermes)
echo '===CLI==='; echo "$cli"
echo '===VER==='; [ -n "$cli" ] && "$cli" --version 2>&1 | head -1
echo '===CHECK==='; [ -n "$cli" ] && "$cli" update --check 2>&1 | head -10
echo '===GIT==='
repo='@@REPO@@'
if [ -d "$repo" ]; then
  cd "$repo"
  echo "HEAD=$(git rev-parse HEAD 2>&1)"
  echo "UPSTREAM=$(git rev-parse '@{u}' 2>&1)"
  echo "BRANCH=$(git rev-parse --abbrev-ref HEAD 2>&1)"
  echo "SHALLOW=$(git rev-parse --is-shallow-repository 2>&1)"
  echo "BEHIND=$(git rev-list --count 'HEAD..@{u}' 2>&1)"
  echo "AHEAD=$(git rev-list --count '@{u}..HEAD' 2>&1)"
  echo "MERGEBASE=$(git merge-base HEAD '@{u}' 2>&1)"
  echo "STASHES=$(git stash list 2>/dev/null | wc -l | tr -d ' ')"
  echo "DIRTY=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
else
  echo 'REPO_MISSING=1'
fi
echo '===DESKTOP==='
ps -eo comm= 2>/dev/null | grep -i 'Hermes.app' | sort -u | head -5
echo '===END==='"""

SECTIONS = ("CLI", "VER", "CHECK", "GIT", "DESKTOP")

# --------------------------------------------------------------------------
# Verdicts. Ordered worst-first: a machine gets the first one that applies.
# --------------------------------------------------------------------------

UNREACHABLE = "UNREACHABLE"   # ssh/timeout — say which and why
UNKNOWN     = "UNKNOWN"       # probe ran, output not understood. NEVER silent.
BLOCKED     = "BLOCKED"       # rate-limited / not-a-repo / no CLI
SHALLOW     = "SHALLOW"       # clone cannot answer "how far behind"
DIVERGED    = "DIVERGED"      # real local commits ahead of upstream
BEHIND      = "BEHIND"
CURRENT     = "CURRENT"

PROBLEM_VERDICTS = {UNREACHABLE, UNKNOWN, BLOCKED, SHALLOW, DIVERGED}

RX_BEHIND_PROSE = re.compile(r"(\d+)\s+commits?\s+behind", re.I)
RX_RATELIMIT    = re.compile(r"rate.?limit|HTTP 429", re.I)
RX_NOTAREPO     = re.compile(r"not a git repository", re.I)
RX_UPTODATE     = re.compile(r"up[- ]to[- ]date|already.*latest|no update", re.I)
RX_CARRIED      = re.compile(r"\+(\d+)\s+carried\s+commits?", re.I)


def parse_sections(raw: str) -> dict:
    out, cur = {}, None
    for line in raw.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("===") and s.endswith("===") and len(s) > 6:
            cur = s.strip("=")
            if cur in SECTIONS:
                out[cur] = []
            continue
        if cur in out and s:
            out[cur].append(s)
    return {k: "\n".join(v) for k, v in out.items()}


def parse_kv(block: str) -> dict:
    return dict(
        kv.split("=", 1) for kv in (block or "").splitlines() if "=" in kv
    )


def _int(value: str | None):
    return int(value) if value and value.strip().isdigit() else None


def classify(rec: dict) -> tuple[str, list[str]]:
    """Return (verdict, reasons). Anything not understood is UNKNOWN, not CURRENT."""
    if rec.get("error"):
        err = rec["error"]
        # A remote parse/syntax error is OUR bug, not an unreachable machine.
        # Distinguishing them matters: one is fixed in this file, the other on
        # the estate, and a raw CLIXML dump in the notes column hides both.
        if any(tok in err for tok in ("ParentContainsErrorRecord", "TerminatorExpected",
                                      "<Objs", "ParserError", "CLIXML")):
            return UNKNOWN, ["probe payload was rejected by the remote shell "
                             "(malformed command, not an unreachable agent)"]
        return UNREACHABLE, [err.splitlines()[0][:160]]

    git = parse_kv(rec.get("GIT", ""))
    check = rec.get("CHECK") or ""
    reasons: list[str] = []

    if not rec.get("CLI"):
        return BLOCKED, ["no CLI found on the agent"]
    if git.get("REPO_MISSING"):
        return BLOCKED, ["install repo not found at the rostered path"]
    if RX_NOTAREPO.search(check):
        reasons.append("CLI reports 'not a git repository' — it is addressing a "
                       "different directory than the rostered repo")
        return BLOCKED, reasons
    if RX_RATELIMIT.search(check):
        return BLOCKED, ["rate limited (HTTP 429) — drift unknown this cycle"]

    head, upstream = git.get("HEAD", "").strip(), git.get("UPSTREAM", "").strip()
    behind, ahead = _int(git.get("BEHIND")), _int(git.get("AHEAD"))
    stashes, dirty = _int(git.get("STASHES")), _int(git.get("DIRTY"))

    # No usable git facts at all: silence is not evidence of health. This must
    # be tested BEFORE the shallow heuristic, because "no MERGEBASE because the
    # probe returned nothing" and "no MERGEBASE because history is truncated"
    # look identical, and only the second one is SHALLOW.
    if not head or len(head) < 40:
        return UNKNOWN, ["could not read a full HEAD hash"]

    # A shallow clone's counts are arithmetic on a truncated graph.
    #
    # Only `is-shallow-repository` is positive evidence. An empty merge-base was
    # tried as a second signal and had to be removed: it fires on a machine with
    # 26,480 commits of history whose upstream ref merely failed to resolve
    # during the probe, labelling a healthy repo SHALLOW. A guard that cries
    # wolf on a correct machine teaches the reader to skip the warnings that
    # matter, so an unresolvable upstream is now its own UNKNOWN below.
    if git.get("SHALLOW", "").strip().lower() == "true":
        reasons.append("shallow clone: behind/ahead counts are not meaningful "
                       "and any apparent local commits are an artifact")
        prose = RX_BEHIND_PROSE.search(check)
        if prose:
            reasons.append(f"CLI reports {prose.group(1)} behind (advisory)")
        return SHALLOW, reasons

    if not upstream or len(upstream) < 40:
        return UNKNOWN, ["could not resolve the upstream ref (@{u}) — drift "
                         "cannot be measured this cycle"] + reasons

    if behind is None or ahead is None:
        return UNKNOWN, ["git did not return usable ahead/behind counts"]

    if stashes:
        reasons.append(f"{stashes} stash(es)")
    if dirty:
        reasons.append(f"{dirty} dirty file(s)")
    if rec.get("DESKTOP"):
        reasons.append("Desktop app running from the install tree")

    if ahead > 0:
        reasons.insert(0, f"{ahead} local commit(s) not upstream")
        return DIVERGED, reasons
    if behind > 0:
        reasons.insert(0, f"{behind} commit(s) behind")
        return BEHIND, reasons
    if head == upstream:
        return CURRENT, reasons

    # Understood the numbers, but they do not add up. Do not guess.
    return UNKNOWN, reasons + ["HEAD does not match upstream despite ahead=0, behind=0"]


def probe(agent: dict, timeout: int) -> dict:
    name = agent.get("name", "?")
    rec: dict = {"name": name, "platform": agent.get("platform")}
    cli = agent.get("cli", "")
    repo = agent.get("repo") or (
        "$HOME/.hermes/hermes-agent" if agent.get("platform") == "macos"
        else "%LOCALAPPDATA%\\hermes\\hermes-agent")
    try:
        if agent.get("host") == "local":
            body = (SH_PROBE.replace("@@CLI@@", cli)
                            .replace("@@REPO@@", os.path.expandvars(repo)))
            cmd = ["bash", "-lc", body]
        else:
            body = PS_PROBE.replace("@@CLI@@", cli).replace("@@REPO@@", repo)
            enc = base64.b64encode(body.encode("utf-16-le")).decode()
            cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
                   agent["host"], f"powershell -NoProfile -EncodedCommand {enc}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rec.update(parse_sections(proc.stdout))
        if not proc.stdout.strip():
            rec["error"] = (proc.stderr or "no output from agent").strip()[-200:]
    except subprocess.TimeoutExpired:
        rec["error"] = f"timed out after {timeout}s"
    except Exception as exc:                                       # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"

    verdict, reasons = classify(rec)
    git = parse_kv(rec.get("GIT", ""))
    return {
        "name": name,
        "verdict": verdict,
        "reasons": reasons,
        "head": (git.get("HEAD") or "")[:12],
        "upstream": (git.get("UPSTREAM") or "")[:12],
        "behind": _int(git.get("BEHIND")),
        "ahead": _int(git.get("AHEAD")),
        "stashes": _int(git.get("STASHES")),
        "dirty": _int(git.get("DIRTY")),
        "shallow": git.get("SHALLOW", "").strip().lower() == "true",
        "desktop_running": bool(rec.get("DESKTOP")),
        "version": (rec.get("VER") or "").strip(),
        "carried_claim": (m.group(1) if (m := RX_CARRIED.search(rec.get("VER") or "")) else None),
        "check_raw": (rec.get("CHECK") or "").strip(),
        "error": rec.get("error"),
    }


def load_roster(path: str | None) -> dict:
    candidates = [path] if path else [
        os.environ.get("FLEET_ROSTER"),
        "roster/agents.json",
        "agents.json",
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return json.loads(Path(cand).read_text(encoding="utf-8"))
    sys.exit("fleet_report: no roster found (set FLEET_ROSTER or pass --roster)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Report update-drift across the fleet (read-only).")
    ap.add_argument("--roster")
    ap.add_argument("--state", default="state", help="directory for heartbeat + last-verdict store")
    ap.add_argument("--workers", type=int, default=2,
                    help="parallel agents; keep low, a wide sweep draws HTTP 429 (default 2)")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="print only changes and problems; silence means all-clear")
    args = ap.parse_args()

    roster = load_roster(args.roster)
    agents = [a for a in roster.get("agents", []) if a.get("enabled")]
    if not agents:
        print("fleet_report: roster has no enabled agents", file=sys.stderr)
        return 1

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = []
        for i, agent in enumerate(agents):
            if i and args.workers <= 2:
                time.sleep(random.uniform(1.5, 4.0))   # jitter: do not stampede the remote
            futures.append(pool.submit(probe, agent, args.timeout))
        for fut in futures:
            results.append(fut.result())

    state_dir = Path(args.state)
    state_dir.mkdir(parents=True, exist_ok=True)
    prev_path = state_dir / "fleet-verdicts.json"
    previous = {}
    if prev_path.is_file():
        try:
            previous = {r["name"]: r for r in json.loads(prev_path.read_text())}
        except (ValueError, KeyError):
            previous = {}

    changed, problems = [], []
    for r in results:
        was = previous.get(r["name"], {}).get("verdict")
        r["previous_verdict"] = was
        r["changed"] = was is not None and was != r["verdict"]
        if r["changed"]:
            changed.append(r)
        if r["verdict"] in PROBLEM_VERDICTS:
            problems.append(r)

    heartbeat = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agents_total": len(results),
        "verdicts": {r["name"]: r["verdict"] for r in results},
        "heads": {r["name"]: r["head"] for r in results},
        "problems": [r["name"] for r in problems],
        "changed": [r["name"] for r in changed],
    }
    (state_dir / "fleet-heartbeat.json").write_text(json.dumps(heartbeat, indent=2))
    prev_path.write_text(json.dumps(results, indent=2))

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    if args.quiet:
        speak = changed + [r for r in problems if not r["changed"]]
        if not speak:
            return 0                      # all-clear is silent, by contract
        print("FLEET DRIFT REPORT — attention required\n")
        for r in speak:
            arrow = f"  (was {r['previous_verdict']})" if r["changed"] else ""
            print(f"  {r['name']:18} {r['verdict']}{arrow}")
            for reason in r["reasons"]:
                print(f"      - {reason}")
        return 0

    width = max(len(r["name"]) for r in results)
    print(f"{'agent'.ljust(width)}  {'verdict':11} {'head':13} {'behind':>6} {'ahead':>5} "
          f"{'stash':>5} {'dirty':>5}  notes")
    print("-" * (width + 78))
    for r in sorted(results, key=lambda x: (x["verdict"] not in PROBLEM_VERDICTS, x["name"])):
        print(f"{r['name'].ljust(width)}  {r['verdict']:11} {r['head'] or '-':13} "
              f"{str(r['behind']) if r['behind'] is not None else '-':>6} "
              f"{str(r['ahead']) if r['ahead'] is not None else '-':>5} "
              f"{str(r['stashes']) if r['stashes'] is not None else '-':>5} "
              f"{str(r['dirty']) if r['dirty'] is not None else '-':>5}  "
              f"{r['reasons'][0] if r['reasons'] else ''}")
        for reason in r["reasons"][1:]:
            print(f"{' ' * (width + 44)}{reason}")
    print(f"\n{len(problems)} of {len(results)} agents need attention. "
          f"heartbeat -> {state_dir / 'fleet-heartbeat.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
