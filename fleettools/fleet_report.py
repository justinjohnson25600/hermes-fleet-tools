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
  # A branch with no upstream cannot answer @{u}. Fall back to the default
  # remote branch so a deliberately-patched checkout still reports real drift
  # instead of collapsing to UNKNOWN.
  Write-Output ('ORIGINHEAD=' + (git rev-parse origin/main 2>&1))
  Write-Output ('BEHINDORIGIN=' + (git rev-list --count 'HEAD..origin/main' 2>&1))
  Write-Output ('AHEADORIGIN='  + (git rev-list --count 'origin/main..HEAD' 2>&1))
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
  echo "ORIGINHEAD=$(git rev-parse origin/main 2>&1)"
  echo "BEHINDORIGIN=$(git rev-list --count HEAD..origin/main 2>&1)"
  echo "AHEADORIGIN=$(git rev-list --count origin/main..HEAD 2>&1)"
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
UNTRACKED   = "UNTRACKED"     # branch has no upstream ref; measured vs origin/main
BEHIND      = "BEHIND"
CURRENT     = "CURRENT"

PROBLEM_VERDICTS = {UNREACHABLE, UNKNOWN, BLOCKED, SHALLOW, DIVERGED, UNTRACKED}

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
        # No upstream ref. On this estate that means a deliberately-patched
        # branch (e.g. `local-patches`), not a fault -- so measure against the
        # default remote branch rather than reporting UNKNOWN and giving up.
        origin_head = git.get("ORIGINHEAD", "").strip()
        b_org, a_org = _int(git.get("BEHINDORIGIN")), _int(git.get("AHEADORIGIN"))
        branch = git.get("BRANCH", "").strip() or "?"
        if len(origin_head) >= 40 and b_org is not None and a_org is not None:
            detail = [f"on branch '{branch}' with no upstream tracking ref"]
            if a_org:
                detail.append(f"{a_org} local commit(s) not in origin/main")
            if b_org:
                detail.append(f"{b_org} commit(s) behind origin/main")
            return UNTRACKED, detail + reasons
        return UNKNOWN, ["could not resolve the upstream ref (@{u}) and no "
                         "origin/main fallback was readable"] + reasons

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


VERDICTS_FILENAME = "fleet-verdicts.json"
HEARTBEAT_FILENAME = "fleet-heartbeat.json"


# The full per-record key set render_report() reads unconditionally. ONE
# tuple feeds BOTH the store validator (load_stored_verdicts) and the
# renderer, so the two can never drift apart again: round 2 found the
# validator checking only name/verdict while render indexed head/behind/
# ahead/stashes/dirty, so a minimal record {"name": ..., "verdict": ...}
# passed validation and died with KeyError 'head' AFTER the banner.
RENDER_REQUIRED_KEYS = ("name", "verdict", "head", "behind", "ahead",
                        "stashes", "dirty")

# Text-table columns in print order: (key, width, label, right-align). The
# renderer builds its header and rows FROM this spec, and the assert below
# welds it to RENDER_REQUIRED_KEYS: add a column the table renders without
# adding its key to the validated set (or vice versa) and import fails —
# the drift that caused the round-2 KeyError becomes impossible.
_RENDER_COLUMNS = (
    ("head",    13, "head",   False),
    ("behind",   6, "behind", True),
    ("ahead",    5, "ahead",  True),
    ("stashes",  5, "stash",  True),
    ("dirty",    5, "dirty",  True),
)
assert {c[0] for c in _RENDER_COLUMNS} | {"name", "verdict"} == set(
    RENDER_REQUIRED_KEYS), "render columns and validated keys drifted apart"


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON so a PROCESS CRASH never leaves a torn or stale-but-paired file.

    A plain write_text truncates the file first: a crash mid-write leaves a
    zero-byte store that looks exactly like 'no verdicts'. tempfile + rename
    is atomic against process crashes on POSIX and Windows: readers see the
    old file or the new one, never a half-written one.

    Durability boundary (review round 2, deliberate): the file is fsynced but
    the parent directory is NOT, so a full OS power loss may lose the rename
    on some filesystems. Dir-fsync is not portable to the Windows control
    boxes this tool runs from; process-crash atomicity is the guarantee on
    sale, and the docstring now says so instead of implying more.
    """
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp creates the temp file 0600; this store has always shipped
        # 0644 and secondary readers (other accounts, archive tooling) depend
        # on that — an atomic rewrite must not silently tighten permissions.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_stored_verdicts(path: Path) -> tuple[list[dict], str | None]:
    """Read a fleet-verdicts.json in EITHER the current or the legacy shape.

    Current (written since the ts-inside-the-store fix): ``{"ts": ...,
    "results": [...]}`` — the store carries its own sweep timestamp. Legacy
    (a bare list) has no embedded ts; the caller falls back to file mtime.

    Raises ValueError with a one-line, human-readable message on any shape
    problem. Records are validated against RENDER_REQUIRED_KEYS — the same
    tuple render_report() consumes — so anything that would die as a KeyError
    traceback after the banner dies here as a clean error line instead.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        results, ts = raw.get("results"), raw.get("ts")
    else:
        results, ts = raw, None
    if not isinstance(results, list) or not results:
        raise ValueError("not a non-empty list of results")
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            raise ValueError(f"results[{i}] must be an object, not {type(r).__name__}")
        if not isinstance(r.get("name"), str) or not r["name"]:
            raise ValueError(f"results[{i}].name must be a non-empty string")
        missing = [k for k in RENDER_REQUIRED_KEYS if k not in r]
        if missing:
            raise ValueError(
                f"results[{i}] is missing field(s) the report renders: "
                + ", ".join(missing))
    return results, ts if isinstance(ts, str) else None


def _age_h_from_capture(ts: str | None, mtime: float | None) -> float | None:
    """Age in hours from an ALREADY-CAPTURED (ts, mtime) pair; None if unknowable.

    Shared by verdicts_age_h (which captures for itself) and
    serve_stored_state (which must age the exact bytes it serves — see the
    TOCTOU note there). A corrupt embedded ts falls through to mtime: the
    store proved readable, only the stamp is garbage, so the age is not
    unknowable.
    """
    if ts is not None:
        try:
            stamped = datetime.fromisoformat(ts)
        except ValueError:
            stamped = None
        if stamped is not None:
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - stamped).total_seconds() / 3600.0)
    if mtime is None:
        return None
    return max(0.0, (datetime.now(timezone.utc).timestamp() - mtime) / 3600.0)


def verdicts_age_h(state_dir: Path) -> float | None:
    """Age of the SERVED verdicts, in hours; None when unknowable.

    The sweep stamps its timestamp INSIDE fleet-verdicts.json, so the file
    a consumer serves labels itself — a state dir whose heartbeat was lost
    or never written can no longer pass as fresh (the 2026-09-03 incident
    class: the banner used to age the heartbeat while serving the verdicts).
    Embedded ts is authoritative; file mtime is the fallback for legacy
    stores. Unknowable age returns None — unknown is not false.
    """
    vpath = Path(state_dir) / VERDICTS_FILENAME
    try:
        ts = None
        try:
            _, ts = load_stored_verdicts(vpath)
        except (OSError, ValueError):
            pass
        return _age_h_from_capture(ts, vpath.stat().st_mtime)
    except (OSError, ValueError):
        return None


def emit_staleness_banner(state_dir, max_age_mins=180, stream=None,
                          age_h: float | None = None) -> bool:
    """Print a STALE DATA banner when the served verdicts are older than max_age_mins.

    The 2026-09-03 incident: a 5.6h-old sweep was served as a current status
    because nothing on the report's face carried its age. The banner is
    printed to STDOUT (the quiet-watchdog contract: stdout IS the human
    channel; this is warranted signal, not noise) alongside the normal table
    — stale data must be labelled, never suppressed.

    Returns True iff the banner fired, so callers can turn staleness into an
    exit code (JSON consumers cannot see stderr). Fail-open by design on
    unknowable age (no file, unreadable store): an unknown age is not a
    false one. That condition has a different owner (the deadman cron) and
    must not cry wolf here. A readable store with a corrupt embedded ts is
    NOT unknowable — age falls back to file mtime (round 2: it used to fail
    open, letting a 24h-old corrupt store serve as rc=0 fresh).
    """
    out = stream if stream is not None else sys.stdout
    if age_h is None:
        age_h = verdicts_age_h(state_dir)
    if age_h is None or age_h * 60 <= max_age_mins:
        return False
    print(
        "STALE DATA: sweep is %.1f h old — re-run before trusting these numbers" % age_h,
        file=out,
    )
    return True


def render_report(results: list[dict], state_dir: Path, quiet: bool = False,
                  served_ts: str | None = None) -> int:
    """Print the human report for `results` — live from a sweep, or stored."""
    problems = [r for r in results if r.get("verdict") in PROBLEM_VERDICTS]
    if quiet:
        speak = ([r for r in results if r.get("changed")]
                 + [r for r in problems if not r.get("changed")])
        if not speak:
            return 0                      # all-clear is silent, by contract
        print("FLEET DRIFT REPORT — attention required\n")
        for r in speak:
            arrow = f"  (was {r.get('previous_verdict')})" if r.get("changed") else ""
            print(f"  {r['name']:18} {r['verdict']}{arrow}")
            for reason in r.get("reasons") or []:
                print(f"      - {reason}")
        return 0

    width = max(len(r["name"]) for r in results)

    def cell(r, key, w, right):
        v = r[key]
        if right:                       # numeric columns: only None is '-'
            return (str(v) if v is not None else "-").rjust(w)
        return (str(v) if v else "-").ljust(w)   # head: '' renders as '-'

    header = f"{'agent'.ljust(width)}  {'verdict':11} " + " ".join(
        label.rjust(w) if right else label.ljust(w)
        for _k, w, label, right in _RENDER_COLUMNS) + "  notes"
    print(header)
    print("-" * (width + 78))
    for r in sorted(results, key=lambda x: (x["verdict"] not in PROBLEM_VERDICTS, x["name"])):
        cells = " ".join(
            cell(r, key, w, right) for key, w, _label, right in _RENDER_COLUMNS)
        print(f"{r['name'].ljust(width)}  {r['verdict']:11} {cells}  "
              f"{(r.get('reasons') or [''])[0]}")
        for reason in (r.get("reasons") or [])[1:]:
            print(f"{' ' * (width + 44)}{reason}")
    if served_ts is None:
        # Producer (sweep) mode: this run wrote the state the next consumer
        # will serve, so point at the store it just refreshed.
        print(f"\n{len(problems)} of {len(results)} agents need attention. "
              f"store -> {state_dir / VERDICTS_FILENAME}")
    else:
        # Consumer (--no-sweep) mode: nothing was written; report exactly
        # what is being served, with the sweep timestamp it was stamped with.
        print(f"\n{len(problems)} of {len(results)} agents need attention. "
              f"served from {state_dir / VERDICTS_FILENAME} (sweep ts {served_ts})")
    return 0


def serve_stored_state(args) -> int:
    """--no-sweep: read-only interactive view of the state dir.

    Serves the LAST sweep's stored verdicts and labels them with the STALE
    DATA banner when the state dir's heartbeat is old. This is the banner's
    only home. It cannot live in the sweep path:

    * BEFORE the sweep (where it sat since b08a4f8) it reads the PREVIOUS
      run's heartbeat — ~24h old in the nightly cron — so the banner fired
      on fresh data every single night (production output 09-05, 09-06).
      A guard that cries wolf nightly is worse than none.
    * AFTER the sweep it would read the heartbeat the same run just wrote,
      which is fresh by construction (probe() never raises, so the write is
      unconditional) — an unreachable guard is guard deletion.

    Age comes from the ts the sweep stamps INSIDE fleet-verdicts.json
    (embedded ts first, file mtime fallback for legacy stores) — never from
    the heartbeat, which is a liveness side-channel and can be missing while
    the verdicts it vouches for are a day old (2026-09-03 incident class).
    """
    state_dir = Path(args.state)
    prev_path = state_dir / VERDICTS_FILENAME
    if not prev_path.is_file():
        # Nothing to serve is a visible failure, never silence: a missing
        # report must not look like an all-clear fleet.
        print(f"fleet_report: no stored verdicts in {state_dir} — run a sweep first",
              file=sys.stderr)
        return 1
    try:
        # Read the store ONCE and capture its mtime alongside: everything
        # below (banner, table, JSON) is derived from this single snapshot.
        # Re-reading inside emit_staleness_banner was a TOCTOU: a sweep
        # landing between the two reads replaced the store with a fresh one,
        # and the banner labelled the OLD results we still serve with the NEW
        # timestamp (review round 2). Ageing the captured bytes closes it.
        results, served_ts = load_stored_verdicts(prev_path)
        served_mtime = prev_path.stat().st_mtime
    except (OSError, ValueError) as exc:
        print(f"fleet_report: stored verdicts unreadable ({exc})", file=sys.stderr)
        return 1

    # A stored report is a replay, not a live observation: 'changed'/'previous_verdict'
    # were computed against the state of the world one sweep ago and must not be
    # re-announced as if they just happened. Reset in place (not popped) so the
    # --json schema keeps its keys with inert values instead of silently
    # dropping fields between a sweep and a replay.
    for r in results:
        r["changed"] = False
        r["previous_verdict"] = None

    # Staleness first, before any table, so a reader can never mistake an
    # old sweep for a current one (2026-09-03). Unconditional w.r.t. --quiet:
    # stale data is signal. In --json mode stdout must stay valid JSON, so
    # the banner goes to stderr there — and staleness also becomes rc=2,
    # because rc is the only channel a JSON consumer reliably reads. rc=2
    # applies to BOTH modes: the README exit-code table is mode-unqualified
    # and text mode must honour it too (round 2: only --json returned 2).
    bannered = emit_staleness_banner(
        state_dir, max_age_mins=args.max_age_mins,
        stream=sys.stderr if args.json else sys.stdout,
        age_h=_age_h_from_capture(served_ts, served_mtime))

    if args.json:
        print(json.dumps(results, indent=2))
        return 2 if bannered else 0
    rc = render_report(results, state_dir, quiet=args.quiet, served_ts=served_ts)
    return 2 if bannered else rc


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
    ap.add_argument("--no-sweep", action="store_true",
                    help="read-only: serve the last sweep's stored report from --state "
                         "and label it with the STALE DATA banner if old; no probing, "
                         "no state writes")
    ap.add_argument("--max-age-mins", type=int, default=180,
                    help="STALE DATA threshold on the stored verdicts' age (default 180)")
    args = ap.parse_args()

    if args.no_sweep:
        return serve_stored_state(args)

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
    prev_path = state_dir / VERDICTS_FILENAME
    previous = {}
    if prev_path.is_file():
        try:
            # Both shapes: current {"ts":..., "results":[...]} and the legacy
            # bare list written before the ts-inside-the-store fix.
            prev_results, _ = load_stored_verdicts(prev_path)
            previous = {r["name"]: r for r in prev_results}
        except (OSError, ValueError, KeyError):
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

    sweep_ts = datetime.now(timezone.utc).isoformat()
    # Verdicts FIRST, heartbeat second: the heartbeat is the liveness beacon
    # for a deadman switch, so it must only proclaim life after the store it
    # vouches for is durable on disk. A crash between the two writes then
    # leaves an ageing heartbeat over stale verdicts — visible — instead of
    # a fresh heartbeat over stale verdicts, which is a lie. Both writes are
    # temp-file + os.replace, so a crash mid-write can never tear a file.
    _atomic_write_json(prev_path, {"ts": sweep_ts, "results": results})

    heartbeat = {
        "ts": sweep_ts,
        "agents_total": len(results),
        "verdicts": {r["name"]: r["verdict"] for r in results},
        "heads": {r["name"]: r["head"] for r in results},
        "problems": [r["name"] for r in problems],
        "changed": [r["name"] for r in changed],
    }
    _atomic_write_json(state_dir / HEARTBEAT_FILENAME, heartbeat)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    return render_report(results, state_dir, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
