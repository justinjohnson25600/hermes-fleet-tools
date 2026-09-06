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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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


# --------------------------------------------------------------------------
# The typed record every consumer of a stored store renders from.
#
# Round 3 closed the class, not another instance: rounds 1 and 2 kept a
# key-PRESENCE validator next to a renderer that indexes those keys, and the
# third drift (a stored record with verdict:null passing presence checks and
# dying in render with a TypeError AFTER the banner) proved presence and type
# are two different contracts that must live in ONE place. They do now: a
# record read by load_stored_verdicts becomes a StoredVerdict whose fields
# ARE the render schema, typed, checked once at construction. render_report
# indexes attributes; a wrong type or a missing field raises here, at load,
# naming the field — before any banner is printed, so the caller's
# except-clause turns it into the one-line unreadable-store message.
#
# The record deliberately also carries the soft render inputs (reasons,
# changed, previous_verdict): constructing it from ANY stored dict that
# renders cleanly means construction cannot introduce a new after-the-banner
# crash class, which a name-only record would.
# --------------------------------------------------------------------------
@dataclass
class StoredVerdict:
    """One stored agent record, validated once, rendered everywhere."""

    name: str
    verdict: str
    head: str
    behind: Optional[int]
    ahead: Optional[int]
    stashes: Optional[int]
    dirty: Optional[int]
    reasons: list = field(default_factory=list)
    changed: bool = False
    previous_verdict: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def __init__(self, index, raw):
        """Validate raw (a decoded JSON record) into typed fields.

        Raises ValueError with a one-line message naming the field and the
        problem, exactly like the previous key-presence validator did — so
        callers' except ValueError handling is unchanged.
        """
        where = f"results[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(
                f"{where} must be an object, not {type(raw).__name__}")
        self.extra = {k: v for k, v in raw.items()}

        def take(field_name: str, want, allow_missing: bool = False) -> Any:
            if field_name not in raw:
                if allow_missing:
                    return None
                raise ValueError(f"{where} is missing field '{field_name}' "
                                 f"the report renders")
            v = raw[field_name]
            # The numeric fields are int|None by contract: the producer emits
            # null counts whenever git cannot answer (UNREACHABLE, UNKNOWN),
            # so None is ACCEPTED there and rendered as '-'. A bool is refused
            # even though it subclasses int — dirty:true is a type error, not
            # a count — and a numeric string is accepted from a hand-edited
            # store. String fields must be real strings.
            if want is int:
                if v is None:
                    return None
                if isinstance(v, bool):
                    ok = False
                elif isinstance(v, int):
                    ok = True
                elif isinstance(v, str) and v.strip().isdigit():
                    v = int(v)
                    ok = True
                else:
                    ok = False
            else:
                ok = isinstance(v, want) and not isinstance(v, bool)
            if not ok:
                raise ValueError(
                    f"{where}.{field_name} must be "
                    f"{'an integer or null' if want is int else _typename(want)}, "
                    f"not {json.dumps(v)[:60]} ({type(v).__name__})")
            return v

        self.name = take("name", str)
        if not self.name:
            raise ValueError(f"{where}.name must be a non-empty string")
        self.verdict = take("verdict", str)
        self.head = take("head", str)
        self.behind = take("behind", int)
        self.ahead = take("ahead", int)
        self.stashes = take("stashes", int)
        self.dirty = take("dirty", int)
        reasons = raw.get("reasons") or []
        if not isinstance(reasons, list) or any(
                not isinstance(x, str) for x in reasons):
            raise ValueError(f"{where}.reasons must be a list of strings")
        self.reasons = list(reasons)
        self.changed = bool(raw.get("changed", False))
        previous = raw.get("previous_verdict")
        if previous is not None and not isinstance(previous, str):
            raise ValueError(f"{where}.previous_verdict must be a string or null")
        self.previous_verdict = previous

    def as_dict(self):
        """The full record for --json output: validated fields first, then
        any extra keys the producer wrote, preserving them verbatim."""
        d = {k: v for k, v in self.extra.items()}
        d.update({
            "name": self.name, "verdict": self.verdict, "head": self.head,
            "behind": self.behind, "ahead": self.ahead,
            "stashes": self.stashes, "dirty": self.dirty,
            "reasons": self.reasons, "changed": self.changed,
            "previous_verdict": self.previous_verdict,
        })
        return d


def _typename(want) -> str:
    return {str: "a string", int: "an integer", list: "a list"}.get(
        want, getattr(want, "__name__", str(want)))


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


def _read_store_fd(path: Path):
    """Open the store once and return (decoded JSON, mtime from the SAME fd).

    Round 3 TOCTOU: load_stored_verdicts used to read the bytes with
    read_text() and then call path.stat() — two separate opens. On a legacy
    bare-list store (the only shape with no embedded ts, so the only shape
    whose age comes from mtime) a sweep could land between the two: the reader
    holds the OLD bytes but stats the NEW file, pairs old data with a fresh
    mtime, computes age 0, and serves a day-old report with no banner.
    os.open + os.fstat + read on one descriptor makes the pairing airtight:
    the fstat sees whatever inode the open() resolved to, and reading from
    that same fd reads exactly that inode — bytes and mtime provably describe
    the same file, whatever lands on the path meanwhile.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        st = os.fstat(fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return json.loads(b"".join(chunks).decode("utf-8")), st.st_mtime


def load_stored_verdicts(path: Path) -> tuple[list, str | None, float]:
    """Read a fleet-verdicts.json ONCE; return (records, ts, mtime).

    Works with EITHER the current or the legacy store shape. Current
    (written since the ts-inside-the-store fix): ``{"ts": ...,
    "results": [...]}`` — the store carries its own sweep timestamp. Legacy
    (a bare list) has no embedded ts; the caller falls back to file mtime.

    The bytes AND the mtime come from a single open file description
    (see _read_store_fd) — the mtime provably describes the exact bytes
    returned, whatever lands on the path during the call. Round 3: this
    used to be read_text() followed by path.stat(), so a sweep landing
    between them served old bytes paired with a fresh mtime — age 0, no
    banner — and the legacy bare-list shape (mtime is its ONLY age source)
    was exactly the shape that race bit.

    Every record is validated ONCE into a StoredVerdict (typed fields for
    everything the report renders). Wrong shape, missing keys, or wrong types
    (a null verdict, a numeric head) raise ValueError here with a one-line
    message naming the field — BEFORE any banner or table — instead of dying
    as a KeyError/TypeError traceback after it.
    """
    raw, mtime = _read_store_fd(Path(path))
    results, ts = _parse_store(raw)
    return results, ts, mtime


def _load_previous_verdicts_lenient(path: Path) -> dict[str, str]:
    """name -> previous verdict, for the producer's changed-arrows.

    Round 3: the strict typed loader now backs the consumer, and wiring the
    producer's diff to it would mean ONE malformed record in the previous
    store silently kills every changed-arrow for the night (the strict load
    raises, the caller catches, previous = {}). This loader asks only the two
    questions the diff needs — is there a name, is there a verdict string —
    and keeps every record that answers both, so a single bad record costs
    only its own arrow, never the night's.
    """
    try:
        raw, _ = _read_store_fd(Path(path))
    except (OSError, ValueError):
        return {}
    results = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(results, list):
        return {}
    previous: dict[str, str] = {}
    for r in results:
        if (isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"]
                and isinstance(r.get("verdict"), str)):
            previous[r["name"]] = r["verdict"]
    return previous


def _age_h_from_capture(ts: str | None, mtime: float | None) -> float | None:
    """Age in hours from an ALREADY-CAPTURED (ts, mtime) pair; None if unknowable.

    Shared by verdicts_age_h (which captures for itself) and
    serve_stored_state (which must age the exact bytes it serves — see the
    TOCTOU note there). A corrupt embedded ts falls through to mtime: the
    store proved readable, only the stamp is garbage, so the age is not
    unknowable.

    A future ts (clock skew, or a store written by a fast clock) computes a
    NEGATIVE age; clamping it to 0.0 in both branches made a corrupt-future
    store read fresh forever. A negative age is evidence the stamp is not
    trustworthy, so it falls through to mtime instead (round 3).
    """
    if ts is not None:
        try:
            stamped = datetime.fromisoformat(ts)
        except ValueError:
            stamped = None
        if stamped is not None:
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - stamped).total_seconds() / 3600.0
            if age >= 0.0:
                return age
            # future stamp: not merely fresh — untrustworthy. Try mtime.
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

    The ts and the mtime are captured from the SAME file descriptor as the
    bytes (round 3: this used to load the store and then stat the path —
    two opens, so a sweep landing between them paired old data with a fresh
    mtime on exactly the legacy shape whose only age source is mtime).
    Record validity is deliberately NOT required here: the age of a store
    does not depend on whether its records render.
    """
    vpath = Path(state_dir) / VERDICTS_FILENAME
    try:
        raw, mtime = _read_store_fd(vpath)
    except (OSError, ValueError):
        return None
    ts = raw.get("ts") if isinstance(raw, dict) else None
    return _age_h_from_capture(ts if isinstance(ts, str) else None, mtime)


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


# Text-table columns in print order: (attribute, width, label, right-align).
# Every attribute here is a typed field on StoredVerdict — the render schema
# lives in ONE place (the dataclass), so there is no second key set to drift
# out of sync and nothing left to weld together with an import-time assert
# (round 3 removed RENDER_REQUIRED_KEYS and the weld; see StoredVerdict).
_RENDER_COLUMNS = (
    ("head",    13, "head",   False),
    ("behind",   6, "behind", True),
    ("ahead",    5, "ahead",  True),
    ("stashes",  5, "stash",  True),
    ("dirty",    5, "dirty",  True),
)


def render_report(results: list, state_dir: Path, quiet: bool = False,
                  served_ts: str | None = None) -> int:
    """Print the human report for `results` — live from a sweep, or stored.

    `results` items are StoredVerdict records (attributes, not dict keys):
    the producer wraps its fresh sweep dicts, the consumer loads them from
    the store — both go through the same typed record, so a live sweep and
    a replay of the same data are rendered by identical code paths.
    """
    problems = [r for r in results if r.verdict in PROBLEM_VERDICTS]
    if quiet:
        speak = ([r for r in results if r.changed]
                 + [r for r in problems if not r.changed])
        if not speak:
            return 0                      # all-clear is silent, by contract
        print("FLEET DRIFT REPORT — attention required\n")
        for r in speak:
            arrow = f"  (was {r.previous_verdict})" if r.changed else ""
            print(f"  {r.name:18} {r.verdict}{arrow}")
            for reason in r.reasons:
                print(f"      - {reason}")
        return 0

    width = max(len(r.name) for r in results)

    def cell(r, key, w, right):
        v = getattr(r, key)
        if right:                       # numeric columns: only None is '-'
            return (str(v) if v is not None else "-").rjust(w)
        return (str(v) if v else "-").ljust(w)   # head: '' renders as '-'

    header = f"{'agent'.ljust(width)}  {'verdict':11} " + " ".join(
        label.rjust(w) if right else label.ljust(w)
        for _k, w, label, right in _RENDER_COLUMNS) + "  notes"
    print(header)
    print("-" * (width + 78))
    for r in sorted(results, key=lambda x: (x.verdict not in PROBLEM_VERDICTS, x.name)):
        cells = " ".join(
            cell(r, key, w, right) for key, w, _label, right in _RENDER_COLUMNS)
        print(f"{r.name.ljust(width)}  {r.verdict:11} {cells}  "
              f"{(r.reasons or [''])[0]}")
        for reason in (r.reasons or [])[1:]:
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
        # Read the store ONCE from a single fd: bytes and mtime are captured
        # from the same open file description, and every record is validated
        # into a typed StoredVerdict BEFORE any banner or table. Two rounds
        # of read-then-re-read TOCTOU and render-crashes-after-the-banner
        # both live here, and both are closed by read-once-at-the-top:
        #   * a sweep replacing the file mid-serve can no longer pair old
        #     results with a fresh mtime (round 2 banner TOCTOU; round 3
        #     found the same race in the bytes/mtime pairing itself);
        #   * a malformed record dies inside the loader with a field name,
        #     not inside the renderer with a traceback (rounds 2 and 3).
        results, served_ts, served_mtime = load_stored_verdicts(prev_path)
    except (OSError, ValueError) as exc:
        print(f"fleet_report: stored verdicts unreadable ({exc})", file=sys.stderr)
        return 1

    # A stored report is a replay, not a live observation: 'changed'/'previous_verdict'
    # were computed against the state of the world one sweep ago and must not be
    # re-announced as if they just happened. Reset in place (not dropped) so the
    # --json schema keeps its keys with inert values instead of silently
    # dropping fields between a sweep and a replay.
    for r in results:
        r.changed = False
        r.previous_verdict = None

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
        print(json.dumps([r.as_dict() for r in results], indent=2))
        return 2 if bannered else 0
    rc = render_report(results, state_dir, quiet=args.quiet, served_ts=served_ts)
    return 2 if bannered else rc


def _parse_store(raw) -> tuple[list, str | None]:
    """Decode an already-read store payload into (StoredVerdicts, ts)."""
    if isinstance(raw, dict):
        results, ts = raw.get("results"), raw.get("ts")
    else:
        results, ts = raw, None
    if not isinstance(results, list) or not results:
        raise ValueError("not a non-empty list of results")
    return [StoredVerdict(i, r) for i, r in enumerate(results)], (
        ts if isinstance(ts, str) else None)


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
    # The producer's changed-arrows diff against the PREVIOUS store goes
    # through the LENIENT loader (name+verdict only): one malformed record
    # in yesterday's store must cost its own arrow, not the whole night's
    # (round 3 — the strict typed loader backs the consumer, where a bad
    # record means we cannot faithfully serve the report at all).
    previous = _load_previous_verdicts_lenient(prev_path) \
        if prev_path.is_file() else {}

    changed, problems = [], []
    for r in results:
        was = previous.get(r["name"])
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

    return render_report([StoredVerdict(i, r) for i, r in enumerate(results)],
                         state_dir, quiet=args.quiet)


if __name__ == "__main__":
    sys.exit(main())
