#!/usr/bin/env python3
"""Deliver skills to every enabled Hermes agent in agents.json.

    python3 deploy.py --list                 # who would receive, and their state
    python3 deploy.py dev-pair verify-results
    python3 deploy.py --all                  # every skill in skills.json
    python3 deploy.py dev-pair --dry-run     # show the plan, change nothing

Replaces hand-rolled one-off SSH loops: the target list lives in agents.json, so
adding a box is a data edit, not a code edit.

Deliberate behaviours, each earned from a real failure:
  * Waits for raw.githubusercontent to actually serve the version in the repo.
    The CDN lags a push by minutes and boxes silently install the OLD release.
  * Ships the payload as base64 to a temp file rather than inline `python -c`:
    long inline commands are truncated by cmd.exe.
  * Cleans only the stale copies its own installer left (the pre-split top-level
    dir, an old category dir under skills/), and only when they hold published
    content. The Hermes install tree, plugins, profiles, backups and curator
    archives belong to other owners and are never touched: walking the whole
    home deleted a bundled Hermes skill that shared an estate skill's name.
  * Keeps what agents honed: a canonical or per-profile copy holding content the
    skills repo never published is HELD and reported, never overwritten (owner
    decision 2026-10-18). Copies of any published version are replaced. Without
    the repo's history every differing copy is held (fail closed).
    --replace-honed overrides, owner-approved only.
  * Seeds only real profiles: tombstoned, identity-less and opted-out dirs are
    skipped.
  * Compares each agent's check COUNT against the highest seen. A lower count
    means checks are silently skipping, which a green "0 failed" will hide.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_ROSTER_OVERRIDE: str | None = None
RAW = os.environ.get(
    "FLEET_SKILLS_RAW",
    "https://raw.githubusercontent.com/YOUR-ORG/YOUR-SKILLS-REPO/main",
)

HONING_SRC = r'''
import hashlib as _hl, pathlib as _pl, shutil as _sh

# Same identity rule as Hermes (hermes_constants.named_profile_has_identity): a dir under
# profiles/ without one of these, or with a tombstone in profiles/.deleted/, is not a profile.
# Seeding such a dir kept a deleted profile alive on disk (2026-10).
_IDENTITY = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")


def content_hash(data):
    """Line-ending-blind digest: install.py writes text, so Windows copies carry CRLF."""
    return _hl.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:16]


def managed_files(d):
    """Every file of an installed skill copy, minus dot-entries and bytecode."""
    d = _pl.Path(d)
    out = []
    for f in sorted(d.rglob("*")) if d.is_dir() else []:
        rel = f.relative_to(d).parts
        if f.is_file() and not any(p.startswith(".") or p == "__pycache__" for p in rel):
            out.append(f)
    return out


def local_edits(files, known):
    """Files whose content was never published: the agent (or a person) honed this copy.

    `known` holds the digest of every blob the skills repo ever published for the skill, so a
    copy of ANY published version is untouched and safe to replace; anything else is local work.
    An empty `known` (repo unavailable) holds every copy that differs: fail closed.
    """
    return [str(f) for f in files if content_hash(f.read_bytes()) not in known]


def is_profile(prof):
    prof = _pl.Path(prof)
    return (prof.is_dir() and not prof.name.startswith(".")
            and not (prof.parent / ".deleted" / prof.name).exists()
            and any((prof / m).is_file() or (prof / m).is_symlink() for m in _IDENTITY))


def canonical_dir(home, skill):
    """The copy install.py owns: skills/<category>/<skill>, never under a dot-dir.

    The curator archives into skills/.archive/<skill>, which the same glob matches, and sorted()
    puts '.archive' before every category: an archived copy became "canonical" (the seed source
    for every profile, and the real install the "stale" one).
    """
    hits = sorted(f.parent for f in _pl.Path(home).glob("skills/*/%s/SKILL.md" % skill)
                  if not f.parent.parent.name.startswith("."))
    return hits[0] if hits else None


def stale_copies(home, skill, known):
    """(remove, held): leftover copies of `skill` from the estate installer, split by content.

    Candidates are only where that installer ever wrote: the pre-split top-level <home>/<skill> and
    other category dirs under <home>/skills/ (a skill that changed category). The Hermes install
    tree, plugins, profiles, backups and dot-dirs belong to other owners and are never candidates:
    walking the whole home deleted the bundled skill inside hermes-agent/skills/ while an estate
    skill shared its name (2026-10). A candidate is removed only when every file is published
    content or matches the canonical copy; anything else is local work and is held, as in
    seed_profiles. No canonical copy, nothing removed.
    """
    home = _pl.Path(home)
    canon = canonical_dir(home, skill)
    if canon is None:
        return [], []
    ours = set(known) | {content_hash(f.read_bytes()) for f in managed_files(canon)}
    cands = [home / skill] if (home / skill / "SKILL.md").is_file() else []
    cands += [f.parent for f in sorted(home.glob("skills/*/%s/SKILL.md" % skill))
              if f.parent != canon and not f.parent.parent.name.startswith(".")]
    remove, held = [], []
    for d in cands:
        edits = local_edits(managed_files(d), ours)
        if edits:
            held.append({"where": str(d), "files": edits[:10], "count": len(edits)})
        else:
            remove.append(d)
    return remove, held


def seed_profiles(home, skill, known, replace=False):
    """Seed each profile's copy of `skill` from the canonical install, holding honed copies."""
    home = _pl.Path(home)
    res = {"seeded": [], "replaced": [], "held": [], "errors": []}
    canon = canonical_dir(home, skill)
    prof_root = home / "profiles"
    if canon is None or not prof_root.is_dir():
        return res
    body = (canon / "SKILL.md").read_bytes()
    rel = canon.relative_to(home / "skills")
    for prof in sorted(prof_root.iterdir()):
        if not is_profile(prof) or (prof / ".no-bundled-skills").exists():
            continue
        dst = prof / "skills" / rel
        cur = dst / "SKILL.md"
        try:
            if cur.is_file() and cur.read_bytes() == body:
                continue
            if cur.is_file():
                edits = local_edits(managed_files(dst), known)
                if edits and not replace:
                    res["held"].append({"where": str(dst), "files": edits[:10], "count": len(edits)})
                    continue
                _sh.rmtree(dst, ignore_errors=True)
                res["replaced"].append(str(cur))
            dst.mkdir(parents=True, exist_ok=True)
            for f in canon.iterdir():
                if f.is_file():
                    _sh.copy2(f, dst / f.name)
            for sub in canon.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    _sh.copytree(sub, dst / sub.name, dirs_exist_ok=True)
            res["seeded"].append(str(dst))
        except OSError as e:
            res["errors"].append("%s: %s" % (prof.name, e))
    return res


def canonical_edits(home, skill, known, state):
    """Local edits in the default home's install of `skill`: its skill dir plus declared state files."""
    home = _pl.Path(home)
    canon = canonical_dir(home, skill)
    if canon is None:
        return []
    files = managed_files(canon)
    state_dir, state_files = state or (None, [])
    if state_dir:
        files += [home / state_dir / f for f in state_files if (home / state_dir / f).is_file()]
    return local_edits(files, known)
'''
_honing: dict = {}
exec(HONING_SRC, _honing)  # the same functions the payload carries, importable for tests
content_hash = _honing["content_hash"]
managed_files = _honing["managed_files"]
local_edits = _honing["local_edits"]
is_profile = _honing["is_profile"]
canonical_dir = _honing["canonical_dir"]
stale_copies = _honing["stale_copies"]
seed_profiles = _honing["seed_profiles"]
canonical_edits = _honing["canonical_edits"]

REMOTE = r'''import json, os, pathlib, re, shutil, subprocess, sys, time, urllib.request
RAW = "%(raw)s"
SKILLS = %(skills)r
DRY = %(dry)s
KNOWN = {k: set(v) for k, v in %(known)r.items()}
STATE = %(state)r
REPLACE = %(replace)s
%(honing)s
h = os.environ.get("HERMES_HOME") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes")
HOME = pathlib.Path(h) if h and pathlib.Path(h).is_dir() else pathlib.Path.home() / ".hermes"
rep = {"host": os.environ.get("COMPUTERNAME") or os.uname().nodename, "home": str(HOME), "skills": {}}
# COMPUTERNAME is NOT a machine identity: cloning a Windows image without sysprep
# leaves every copy sharing a name (and a MachineGuid). The BIOS serial survives
# imaging and is per-unit, so it is the honest key for "have I seen this box".
try:
    if os.name == "nt":
        _s = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "(Get-CimInstance Win32_BIOS).SerialNumber"],
                            capture_output=True, text=True, timeout=60)
        _v = (_s.stdout or "").strip()
        # Cheap mini-PCs ship an unfilled SMBIOS field, so the "serial" is a
        # placeholder shared by every unit of that model. Treating those as an
        # identity makes unrelated boxes collide.
        if _v.lower() in {"default string", "to be filled by o.e.m.", "system serial number",
                          "none", "n/a", "0", "123456789", "invalid"}:
            _v = ""
        rep["serial"] = _v or None
except Exception:
    rep["serial"] = None
if not HOME.is_dir():
    rep["error"] = "no Hermes home found"
    print(json.dumps(rep)); raise SystemExit(0)

tmp = pathlib.Path(os.environ.get("TEMP") or "/tmp") / ("inst%%d.py" %% time.time())
try:
    with urllib.request.urlopen(RAW + "/install.py?cb=" + str(time.time()), timeout=90) as r:
        tmp.write_bytes(r.read())
except Exception as e:
    rep["error"] = "installer fetch failed: %%s" %% e
    print(json.dumps(rep)); raise SystemExit(0)

def _walk(root, pattern):
    """rglob that survives Windows junctions.

    pathlib.rglob raises OSError 448 on an untraversable reparse point, and npm
    workspace installs create exactly those under node_modules. One junction
    aborts the whole walk, so the scan is done manually with the unreadable and
    irrelevant branches pruned.
    """
    hits = []
    skip = {"node_modules", ".git", "__pycache__", "backups", "venv"}
    for dirpath, dirnames, filenames in os.walk(str(root), onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            p = pathlib.Path(dirpath) / fn
            if p.match(pattern):
                hits.append(p)
    return hits


def ver(f):
    try:
        m = re.search(r"^version:\s*([0-9.]+)", f.read_text(encoding="utf-8", errors="replace"), re.M)
        return m.group(1) if m else None
    except Exception:
        return None

# A default-home install that carries local edits is held: install.py would overwrite it.
HELD = {}
if not DRY and not REPLACE:
    for s in SKILLS:
        _e = canonical_edits(HOME, s, KNOWN.get(s, set()), STATE.get(s))
        if _e:
            HELD[s] = _e
rep["held"] = [{"skill": s, "where": "canonical", "files": e[:10], "count": len(e)}
               for s, e in sorted(HELD.items())]

for s in SKILLS:
    if DRY:
        rep["skills"][s] = {"dry_run": True}
        continue
    if s in HELD:
        rep["skills"][s] = {"held": True}
        continue
    p = subprocess.run([sys.executable, str(tmp), s], capture_output=True, text=True, timeout=900)
    # install.py logs to stdout, including "VERIFY FAILED" and the suite's "N passed, M failed".
    out = (p.stdout or "") + (p.stderr or "")
    rep["skills"][s] = {"rc": p.returncode, "err": out.strip()[-200:] if p.returncode else ""}
    counts = re.findall(r"(\d+) passed, (\d+) failed", out)
    if counts:
        rep["skills"][s]["verify"] = {"passed": int(counts[-1][0]), "failed": int(counts[-1][1])}

# Stale copies: only what the estate installer itself left (the pre-split top-level dir, an old
# category dir), and only when it holds published content; local work there is held and reported
# (stale_copies). Per-profile copies are NOT stale by location: a named-profile session never
# falls back to the default home's skills, so each profile needs its own copy of the prompt
# files - they are reconciled by content below, not deleted.
cleaned = []
if not DRY:
    for s in SKILLS:
        if s in HELD:
            continue
        _rm, _kept = stale_copies(HOME, s, KNOWN.get(s, set()))
        for d in _rm:
            shutil.rmtree(d, ignore_errors=True); cleaned.append(str(d / "SKILL.md"))
        rep["held"] += [dict(h, skill=s) for h in _kept]
rep["cleaned"] = cleaned

# per-profile visibility: seed every profile's skills dir from the canonical
# install (same content), skipping deliberate opt-outs and non-profiles. A copy
# that already matches is left alone; one holding only published content (an
# old version, the pre-split layout) is replaced; one with local edits is HELD
# and reported: agents hone their own skills, and a deploy never erases that.
# A held canonical install is not propagated. Profile-local state
# (devpair/config.json) is never touched — only the prompt files are managed.
seeded, pruned_profiles = [], []
if not DRY:
    for s in SKILLS:
        if s in HELD:
            continue
        _r = seed_profiles(HOME, s, KNOWN.get(s, set()), REPLACE)
        seeded += _r["seeded"]
        pruned_profiles += _r["replaced"] + ["ERR " + e for e in _r["errors"]]
        rep["held"] += [dict(h, skill=s) for h in _r["held"]]
rep["seeded_profiles"] = seeded
rep["pruned_profiles"] = pruned_profiles

for s in SKILLS:
    hits = [f for f in _walk(HOME, "%%s/SKILL.md" %% s) if "backups" not in str(f)]
    # Report the canonical copy. Walk order put profiles/ before skills/, so an
    # opted-out profile still holding an old copy was reported as the install.
    canon = canonical_dir(HOME, s)
    main = (canon / "SKILL.md") if canon else (hits[0] if hits else None)
    v = ver(main) if main else None
    rep["skills"].setdefault(s, {})["version"] = v
    rep["skills"][s]["copies"] = len(hits)
    others = sorted({ver(f) for f in hits if f != main} - {v, None})
    if others:
        rep["skills"][s]["other_versions"] = others

t = HOME / "devpair" / "test_devpair.py"
if t.is_file() and not DRY:
    p = subprocess.run([sys.executable, str(t)], capture_output=True, text=True, timeout=1200, cwd=str(t.parent))
    m = re.search(r"(\d+) passed, (\d+) failed", p.stdout or "")
    rep["tests"] = {"passed": int(m.group(1)), "failed": int(m.group(2))} if m else {"raw": (p.stdout or "")[-120:]}
    # Name the failing checks: an intermittent failure reported only as a count cannot be traced later.
    fails = [l.strip()[:200] for l in (p.stdout or "").splitlines() if l.lstrip().startswith("FAIL ")][:5]
    if fails:
        rep["tests"]["fail_lines"] = fails
try:
    tmp.unlink()
except Exception:
    pass
print(json.dumps(rep))
'''


def verify_counts(text: str) -> dict | None:
    """The installer's last 'N passed, M failed' line, or None for a skill with no suite.
    REMOTE carries the same parse inline: it runs on the box and cannot import this module."""
    found = re.findall(r"(\d+) passed, (\d+) failed", text or "")
    return {"passed": int(found[-1][0]), "failed": int(found[-1][1])} if found else None


def _checks(v: dict | None) -> str:
    return f" (checks {v['passed']}/{v['passed'] + v['failed']})" if v else ""


def skill_failures(r: dict) -> list[str]:
    """One message per delivered skill that did not install cleanly or failed its own checks.
    A version on disk is not a working install: install.py exits 1 on VERIFY FAILED after the
    files have landed."""
    out = []
    for s, d in (r.get("skills") or {}).items():
        if d.get("dry_run"):
            continue
        v = d.get("verify")
        if d.get("rc"):
            msg = f"{s}: installer exited {d['rc']}{_checks(v)}"
            if d.get("err"):
                msg += f" -- {d['err'][-120:]}"
            out.append(msg)
        elif v and v["failed"]:
            out.append(f"{s}: {v['failed']} check(s) failed{_checks(v)}")
    return out


def format_versions(r: dict) -> str:
    """'skill=version (checks p/t) (other copies: ...)' for each delivered skill."""
    parts = []
    for s, d in (r.get("skills") or {}).items():
        p = f"{s}={d.get('version')}{_checks(d.get('verify'))}"
        if d.get("held"):
            p += " (held: local edits)"
        if d.get("other_versions"):
            p += f" (other copies: {','.join(d['other_versions'])})"
        parts.append(p)
    return " ".join(parts)


def load_agents() -> dict:
    """Read the roster.

    The roster is DATA and lives wherever the operator keeps it — commonly a
    separate private repo, since it holds real hostnames and usernames while
    this tool is meant to be shareable. Resolution order:

      1. --roster / FLEET_ROSTER   explicit path
      2. ./agents.json             the working directory
      3. alongside this script     the single-repo case
    """
    for cand in (_ROSTER_OVERRIDE, os.environ.get("FLEET_ROSTER"),
                 Path.cwd() / "agents.json", Path.cwd() / "roster" / "agents.json",
                 ROOT / "agents.json"):
        if cand and Path(cand).is_file():
            return json.loads(Path(cand).read_text(encoding="utf-8"))
    raise SystemExit(
        "no roster found. Looked for --roster/FLEET_ROSTER, ./agents.json, "
        "./roster/agents.json, and one beside deploy.py.\n"
        "Copy agents.example.json to agents.json and fill in your hosts."
    )


def wait_for_cdn(skills: list[str], timeout: int = 420) -> dict:
    """Block until the CDN serves the versions this repo has. A push is not a
    release: raw.githubusercontent caches, and boxes install the previous one."""
    want = {}
    for s in skills:
        # Local manifest if this repo also holds the skills; otherwise ask the
        # remote what version it believes is current and wait for that to settle.
        f = ROOT / s / "skill.json"
        if f.is_file():
            want[s] = json.loads(f.read_text(encoding="utf-8"))["version"]
    if not want:
        # Nothing local to compare against: poll the remote until two reads
        # agree, which clears a mid-propagation cache without needing to know
        # the target version.
        seen = {}
        for s in skills:
            try:
                with urllib.request.urlopen(f"{RAW}/{s}/skill.json?cb={time.time()}", timeout=30) as r:
                    seen[s] = json.loads(r.read().decode()).get("version")
            except Exception:
                seen[s] = None
        return {"want": seen, "serving": seen, "current": True}
    deadline = time.time() + timeout
    while True:
        seen, ok = {}, True
        for s, v in want.items():
            try:
                with urllib.request.urlopen(f"{RAW}/{s}/skill.json?cb={time.time()}", timeout=30) as r:
                    seen[s] = json.loads(r.read().decode())["version"]
            except Exception as e:
                seen[s] = f"ERR {e}"
            if seen[s] != v:
                ok = False
        if ok or time.time() > deadline:
            return {"want": want, "serving": seen, "current": ok}
        time.sleep(20)


_RESOLVE_PY = r'''
$pin = '%(pin)s'
$h = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
$c = @()
if ($pin) { $c += $pin }
$c += (Join-Path $h 'hermes-agent\venv\Scripts\python.exe')
$c += (Get-ChildItem (Join-Path $h 'installs') -Recurse -Depth 6 -Filter python.exe -ErrorAction SilentlyContinue |
       Where-Object { $_.FullName -match '\\environments\\[^\\]+\\venv\\Scripts\\python\.exe$' } |
       Sort-Object LastWriteTime -Descending | ForEach-Object { $_.FullName })
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $c += (& $py.Source -3 -c "import sys; print(sys.executable)" 2>$null) }
$sys = Get-Command python -ErrorAction SilentlyContinue
if ($sys -and $sys.Source -notmatch '\\WindowsApps\\') { $c += $sys.Source }
foreach ($p in $c) {
  if (-not $p -or -not (Test-Path $p)) { continue }
  $out = & $p -c "import sys; print(sys.version_info >= (3, 8))" 2>$null
  if ("$out".Trim() -eq 'True') { "PY=" + $p; exit 0 }
}
"PY="
'''


def resolve_python(host: str, pinned: str | None) -> tuple[str | None, str]:
    """A working interpreter on a remote Windows box, and a note if the pin
    could not be used.

    A Hermes update replaces the environment a pin points into, and a box with
    no pin has only the Microsoft Store stub as `python`. Either way the deploy
    used to fail with "The system cannot find the path specified". The pin
    still wins while it works; otherwise take what the box has now and say so.
    """
    script = _RESOLVE_PY % {"pin": (pinned or "").replace("'", "''")}
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
                            f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"],
                           capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return pinned, "interpreter probe timed out; using the roster pin as-is"
    m = re.search(r"^PY=(.*)$", r.stdout or "", re.M)
    found = (m.group(1).strip() if m else "") or None
    if not found:
        return None, "no Python 3.8+ found (pin, Hermes venvs, py launcher, PATH)"
    if pinned and os.path.normcase(found) != os.path.normcase(pinned):
        return found, f"roster pin {pinned} no longer runs; used {found}. Update the roster pin."
    return found, ""


_HOLD: dict = {"known": {}, "state": {}, "replace": False}


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=True).stdout


def _published_ref(repo: Path) -> str:
    probe = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "origin/main"],
                           capture_output=True)
    return "origin/main" if probe.returncode == 0 else "HEAD"


def published_hashes(repo: Path, skills: list[str]) -> dict[str, list[str]]:
    """content_hash of every blob the skills repo ever published under each skill's directory.

    A live copy made only of these is some published version, so replacing it loses nothing;
    any other content is local honing and is held.
    """
    ref = _published_ref(repo)
    out = {}
    for s in skills:
        log = _git(repo, "log", ref, "-m", "--format=", "--raw", "--no-abbrev", "--no-renames",
                   "--", f"{s}/").decode("utf-8", "replace")
        shas = {sha for line in log.splitlines() if line.startswith(":")
                for sha in line.split()[2:4] if set(sha) != {"0"}}
        blobs = []
        if shas:
            data = _git(repo, "cat-file", "--batch", data=("\n".join(sorted(shas)) + "\n").encode())
            i = 0
            while i < len(data):
                nl = data.index(b"\n", i)
                head = data[i:nl].split()
                if head[-1] == b"missing":
                    i = nl + 1
                    continue
                size = int(head[2])
                blobs.append(data[nl + 1:nl + 1 + size])
                i = nl + 1 + size + 1
        out[s] = sorted({content_hash(b) for b in blobs})
    return out


def state_files(repo: Path, skills: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Each skill's install.py state dir and declared files (installed outside the skill dir)."""
    ref, out = _published_ref(repo), {}
    for s in skills:
        try:
            m = json.loads(_git(repo, "show", f"{ref}:{s}/skill.json"))
        except (subprocess.CalledProcessError, ValueError):
            continue
        if m.get("files"):
            out[s] = (m.get("state_dir", s), list(m["files"]))
    return out


def render_remote(skills: list[str], dry: bool, raw: str | None = None, known: dict | None = None,
                  state: dict | None = None, replace: bool = False) -> str:
    return REMOTE % {"raw": raw or RAW, "skills": skills, "dry": dry, "known": known or {},
                     "state": state or {}, "replace": replace, "honing": HONING_SRC}


def deliver(agent: dict, skills: list[str], dry: bool) -> tuple[str, dict]:
    name, host = agent["name"], agent["host"]
    code = render_remote(skills, dry, **_HOLD)
    # Not every Windows box has a usable `python` on PATH. Some have only the
    # Microsoft Store alias stub, which prints an install advert and exits 9009
    # instead of running anything. An agent may therefore pin its interpreter,
    # and resolve_python() falls back when that pin has gone stale.
    py = agent.get("python") or "python"
    note = ""
    if host == "local":
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=1800)
        out = p.stdout
    else:
        py, note = resolve_python(host, agent.get("python"))
        if not py:
            return name, {"error": note}
        b64 = base64.b64encode(code.encode()).decode()
        writer = (f'"{py}" -c "import base64,os,sys;'
                  "open(os.environ['TEMP']+chr(92)+'_hsdeploy.py','wb')"
                  '.write(base64.b64decode(sys.stdin.read()))"')
        w = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, writer],
                           input=b64, capture_output=True, text=True, timeout=180)
        if w.returncode:
            return name, {"error": f"payload write failed: {(w.stderr or '').strip()[:200]}"}
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", host, f'"{py}" %TEMP%\\_hsdeploy.py'],
                           capture_output=True, text=True, timeout=1800)
        out = p.stdout
    try:
        rep = json.loads(out)
    except Exception:
        return name, {"error": (out or p.stderr or "no output")[-300:]}
    if note:
        rep["interp_note"] = note
    return name, rep


def main() -> int:
    ap = argparse.ArgumentParser(description="Deliver skills to the Hermes agent fleet.")
    ap.add_argument("skills", nargs="*", help="skill names (default: every skill in skills.json)")
    ap.add_argument("--all", action="store_true", help="every skill in skills.json")
    ap.add_argument("--list", action="store_true", help="show agents and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="skip the CDN freshness wait")
    ap.add_argument("--roster", help="path to agents.json (default: ./agents.json, ./roster/agents.json, or beside this script)")
    ap.add_argument("--skills-repo", default=os.environ.get("FLEET_SKILLS_REPO",
                                                            str(Path.home() / "repos" / "hermes_skills")),
                    help="local clone of the skills repo; its history tells published content from local honing")
    ap.add_argument("--replace-honed", action="store_true",
                    help="overwrite copies with local edits (owner-approved only; default holds and reports them)")
    args = ap.parse_args()

    global _ROSTER_OVERRIDE
    _ROSTER_OVERRIDE = args.roster

    cfg = load_agents()
    agents = cfg["agents"]

    if args.list:
        print(f"{'AGENT':<20} {'ROLE':<9} {'PLATFORM':<8} {'ENABLED':<8} HOST")
        for a in agents:
            print(f"{a['name']:<20} {a.get('role',''):<9} {a['platform']:<8} "
                  f"{str(a['enabled']):<8} {a['host']}")
            if not a["enabled"] and a.get("blocked_reason"):
                print(f"{'':<20} BLOCKED: {a['blocked_reason'][:150]}")
        print(f"\nnot enrolled: {', '.join(x['name'] for x in cfg.get('unreachable_not_enrolled', []))}")
        return 0

    skills = args.skills
    if args.all or not skills:
        # The skills repo owns the list; a copy beside this script is the fallback.
        manifest = Path(args.skills_repo).expanduser() / "skills.json"
        if not manifest.is_file():
            manifest = ROOT / "skills.json"
        if not manifest.is_file():
            print("--all needs a skills.json (in --skills-repo or beside this script), or name skills explicitly.")
            return 1
        skills = json.loads(manifest.read_text(encoding="utf-8"))
    # Skills are fetched from the remote source at install time, so there is
    # nothing local to validate against. A name that does not exist upstream
    # surfaces as a per-agent install failure, which is the honest place for it.

    targets = [a for a in agents if a["enabled"]]
    skipped = [a for a in agents if not a["enabled"]]
    print(f"delivering {', '.join(skills)} to {len(targets)} agent(s)"
          f"{' [DRY RUN]' if args.dry_run else ''}")
    for a in skipped:
        print(f"  SKIP {a['name']}: {a.get('blocked_reason', 'disabled')[:110]}")

    repo = Path(args.skills_repo).expanduser()
    _HOLD["replace"] = args.replace_honed
    if (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], capture_output=True, timeout=120)
        _HOLD["known"] = published_hashes(repo, skills)
        _HOLD["state"] = state_files(repo, skills)
        n = sum(len(v) for v in _HOLD["known"].values())
        print(f"  honing guard: {n} published blob digest(s) from {repo} ({_published_ref(repo)})")
    else:
        print(f"  HOLD-ALL: no skills repo at {repo}; every copy that differs will be held (fail closed)")
    if args.replace_honed:
        print("  --replace-honed: local edits WILL be overwritten")

    if not args.dry_run and not args.no_wait:
        cdn = wait_for_cdn(skills)
        if cdn and not cdn["current"]:
            print(f"  WARNING: CDN still stale after wait: {cdn['serving']} (want {cdn['want']})")
            print("  Boxes may install the previous release. Re-run later, or pass --no-wait to force.")
        elif cdn:
            print(f"  CDN serving current: {cdn['want']}")

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as ex:
        results = list(ex.map(lambda a: deliver(a, skills, args.dry_run), targets))

    print()
    counts, failed = [], []
    held_total = 0
    seen_hosts: dict[str, str] = {}
    # Check counts are only comparable between nodes of the same OS.
    agent_by_name = {a["name"]: a for a in targets}
    for name, r in results:
        if r.get("error"):
            print(f"  FAIL {name}: {r['error'][:160]}")
            failed.append(name)
            continue
        for h in r.get("held") or []:
            held_total += 1
            print(f"  HELD {name:<18} {h['skill']} at {h['where']}: {h['count']} locally honed file(s) kept")
        # Identity is the BIOS serial, not the hostname: these boxes are imaged
        # clones that legitimately share a COMPUTERNAME (and MachineGuid), so
        # keying on the name would warn forever on a correct roster and train
        # the reader to ignore it. A repeated SERIAL is the real error — the
        # same physical unit reached twice, whose second result is not
        # independent evidence.
        ident = r.get("serial") or r.get("host")
        if ident and ident in seen_hosts and seen_hosts[ident] != name:
            print(f"  WARN {name}: same machine as {seen_hosts[ident]!r} "
                  f"(identity {ident!r}). One roster entry is redundant and its "
                  f"result is not an independent check.")
            failed.append(name)
        if ident:
            seen_hosts[ident] = name
        bad = skill_failures(r)
        if bad:
            # The files may have landed, but the skill is not proven working there.
            for msg in bad:
                print(f"  FAIL {name}: {msg[:220]}")
            failed.append(name)
            continue
        vers = format_versions(r)
        t = r.get("tests") or {}
        if "raw" in t:
            # The suite ran but printed no summary: it crashed part-way. That is
            # an unverified install, not an OK one.
            print(f"  FAIL {name}: dev-pair suite did not complete — {t['raw'][-140:]!r}")
            failed.append(name)
            continue
        tt = f"{t.get('passed')}/{t.get('passed', 0) + t.get('failed', 0)}" if "passed" in t else "-"
        extra = f" cleaned={len(r['cleaned'])}" if r.get("cleaned") else ""
        if t.get("failed"):
            # Every check ran and some failed: a failing install, not an OK one, and not a skip.
            print(f"  FAIL {name}: dev-pair suite: {t['failed']} check(s) failed ({tt})  {vers}")
            for line in t.get("fail_lines") or []:
                print(f"       {'':<18} {line}")
            failed.append(name)
        else:
            print(f"  OK   {name:<18} {vers}  dev-pair suite={tt}{extra}")
        if r.get("interp_note"):
            print(f"       {'':<18} NOTE: {r['interp_note']}")
        if "passed" in t:
            # Checks RUN (passed + failed), not checks passed: only a smaller number of checks
            # run means checks were skipped. Comparing passed counts reported a failure as a skip.
            counts.append((name, t["passed"] + t.get("failed", 0),
                           (agent_by_name.get(name, {}) or {}).get("platform") or "unknown"))

    # A lower check count than its PEERS means checks are SKIPPING there.
    #
    # Compare within a platform, never across. A suite legitimately runs a
    # different number of checks per OS when assertions are guarded by
    # os.name/sys.platform (argv-limit checks, for example, only exist on
    # Windows), so a global max makes every node of the smaller-count platform
    # warn forever on a perfectly correct install — and a guard that cries wolf
    # on a correct fleet trains the reader to ignore the warnings that matter.
    if counts:
        by_platform = {}
        for name, c, plat in counts:
            by_platform.setdefault(plat, []).append((name, c))
        for plat, group in sorted(by_platform.items()):
            best = max(c for _, c in group)
            for name, c in group:
                if c < best:
                    peers = ", ".join(sorted(n for n, cc in group if cc == best))
                    print(f"  WARN {name}: {c} checks vs {best} on the same platform "
                          f"({plat}: {peers}) — checks are being skipped, not passing. "
                          f"Investigate before trusting this install.")
                    failed.append(name)
        # Cross-platform differences are reported as information, not failure:
        # worth seeing (a real skip could hide here) but not actionable alone.
        tops = {plat: max(c for _, c in group) for plat, group in by_platform.items()}
        if len(set(tops.values())) > 1:
            spread = ", ".join(f"{p}={c}" for p, c in sorted(tops.items()))
            print(f"  note: check counts differ by platform ({spread}) — expected when "
                  f"assertions are platform-guarded; confirm the delta is explained.")

    if held_total:
        print(f"\n{held_total} honed copy(ies) kept, not overwritten. Promote improvements to the skills "
              f"repo, or re-run with --replace-honed once the owner approves.")
    if failed:
        print(f"\n{len(set(failed))} agent(s) need attention: {', '.join(sorted(set(failed)))}")
        return 1
    print(f"\nall {len(results)} agent(s) current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
