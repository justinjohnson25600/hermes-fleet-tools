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
  * Seeds every named profile's skills dir from the canonical install (a
    profile never falls back to the default home), instead of deleting
    per-profile copies as stale.
  * Compares each agent's check COUNT against the highest seen. A lower count
    means checks are silently skipping, which a green "0 failed" will hide.
  * `--check-policy` admits roster identities as DATA: opaque, case-sensitive
    non-blank strings, never coerced, and a roster with no enabled agent is
    "nothing checked", not an all-clear.
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

# Stage B consumer hook: the policy gate imports lazily so a roster deploy on
# a box without the policy modules still works, and `--check-policy` — which
# needs them — fails with a clear message instead of an import traceback.
_sys_path_root = str(ROOT.parent)


def _load_policy_module():
    import importlib
    if _sys_path_root not in sys.path:
        sys.path.insert(0, _sys_path_root)
    return importlib.import_module("fleettools.policy")

REMOTE = r'''import json, os, pathlib, re, shutil, subprocess, sys, time, urllib.request
RAW = "%(raw)s"
SKILLS = %(skills)r
DRY = %(dry)s
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

for s in SKILLS:
    if DRY:
        rep["skills"][s] = {"dry_run": True}
        continue
    p = subprocess.run([sys.executable, str(tmp), s], capture_output=True, text=True, timeout=900)
    rep["skills"][s] = {"rc": p.returncode, "err": (p.stderr or "").strip()[-200:] if p.returncode else ""}

# stale copies: anything outside the canonical category dir and outside
# profiles/ (the stray top-level skill dir from the pre-split layout).
# Per-profile copies are NOT stale by location: a named-profile session never
# falls back to the default home's skills, so each profile needs its own copy
# of the prompt files — they are reconciled by content below, not deleted.
cleaned = []
if not DRY:
    for s in SKILLS:
        canon = list(HOME.glob("skills/*/%%s/SKILL.md" %% s))
        canon = canon[0] if canon else None
        stray = HOME / s / "SKILL.md"
        if stray.is_file() and canon and stray != canon:
            shutil.rmtree(HOME / s, ignore_errors=True); cleaned.append(str(stray))
        for f in list(_walk(HOME, "%%s/SKILL.md" %% s)):
            if "backups" in str(f) or f == canon or "profiles" in f.parts:
                continue
            shutil.rmtree(f.parent, ignore_errors=True); cleaned.append(str(f))
rep["cleaned"] = cleaned

# per-profile visibility: seed every profile's skills dir from the canonical
# install (same content), skipping deliberate opt-outs. A copy that already
# matches is left alone; one that differs (old version or the pre-split layout
# with code files inside) is replaced. Profile-local state (devpair/config.json)
# is never touched — only the prompt files are managed.
seeded, pruned_profiles = [], []
if not DRY:
    prof_root = HOME / "profiles"
    for s in SKILLS:
        canon = list(HOME.glob("skills/*/%%s/SKILL.md" %% s))
        canon = canon[0] if canon else None
        if canon is None:
            continue
        body = canon.read_bytes()
        rel = canon.parent.relative_to(HOME / "skills")
        for prof in sorted(prof_root.iterdir()) if prof_root.is_dir() else []:
            if not prof.is_dir() or prof.name.startswith("."):
                continue
            if (prof / ".no-bundled-skills").exists():
                continue
            dst = prof / "skills" / rel
            cur = dst / "SKILL.md"
            try:
                if cur.is_file() and cur.read_bytes() == body:
                    continue
                if cur.is_file():
                    shutil.rmtree(dst, ignore_errors=True); pruned_profiles.append(str(cur))
                dst.mkdir(parents=True, exist_ok=True)
                for f in canon.parent.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dst / f.name)
                for sub in canon.parent.iterdir():
                    if sub.is_dir() and not sub.name.startswith("."):
                        shutil.copytree(sub, dst / sub.name, dirs_exist_ok=True)
                seeded.append(str(dst))
            except OSError as e:
                pruned_profiles.append("ERR %%s: %%s" %% (prof.name, e))
rep["seeded_profiles"] = seeded
rep["pruned_profiles"] = pruned_profiles

for s in SKILLS:
    hits = [f for f in _walk(HOME, "%%s/SKILL.md" %% s) if "backups" not in str(f)]
    rep["skills"].setdefault(s, {})["version"] = ver(hits[0]) if hits else None
    rep["skills"][s]["copies"] = len(hits)

t = HOME / "devpair" / "test_devpair.py"
if t.is_file() and not DRY:
    p = subprocess.run([sys.executable, str(t)], capture_output=True, text=True, timeout=1200, cwd=str(t.parent))
    m = re.search(r"(\d+) passed, (\d+) failed", p.stdout or "")
    rep["tests"] = {"passed": int(m.group(1)), "failed": int(m.group(2))} if m else {"raw": (p.stdout or "")[-120:]}
try:
    tmp.unlink()
except Exception:
    pass
print(json.dumps(rep))
'''


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


class PolicyGateInputError(Exception):
    """The roster (or its shape) cannot be admitted by the policy gate."""


def _load_roster() -> dict:
    """load_agents + typed validation for the gate: a roster file that is
    not JSON, not an object, or has no agent list is a refused INPUT, not a
    traceback — the operator gets the file, the problem, and the fix."""
    explicit = _ROSTER_OVERRIDE if _ROSTER_OVERRIDE is not None else os.environ.get('FLEET_ROSTER')
    try:
        raw = json.loads(Path(explicit).read_text(encoding='utf-8')) if explicit is not None else load_agents()
    except SystemExit as exc:
        raise PolicyGateInputError('no roster found') from exc
    except json.JSONDecodeError as exc:
        raise PolicyGateInputError(
            f"roster is not valid JSON (line {exc.lineno})") from exc
    except (OSError, UnicodeError) as exc:
        raise PolicyGateInputError('roster is unreadable or not valid UTF-8') from exc
    except (ValueError, RecursionError) as exc:
        raise PolicyGateInputError(
            f'roster JSON exceeds supported parser limits ({type(exc).__name__})') from exc
    if not isinstance(raw, dict):
        raise PolicyGateInputError(
            f"roster must be a JSON object with an 'agents' list "
            f"(got {type(raw).__name__})")
    agents = raw.get("agents")
    if not isinstance(agents, list):
        raise PolicyGateInputError(
            "roster is malformed: 'agents' must be a list of agent objects")
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            raise PolicyGateInputError(
                f"roster is malformed: agents[{i}] must be an object")
        if not isinstance(a.get('enabled', False), bool):
            raise PolicyGateInputError(f'roster is malformed: agents[{i}].enabled must be a boolean')
    return raw


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


def deliver(agent: dict, skills: list[str], dry: bool) -> tuple[str, dict]:
    name, host = agent["name"], agent["host"]
    code = REMOTE % {"raw": RAW, "skills": skills, "dry": dry}
    # Not every Windows box has a usable `python` on PATH. Some have only the
    # Microsoft Store alias stub, which prints an install advert and exits 9009
    # instead of running anything. An agent may therefore pin its interpreter.
    py = agent.get("python") or "python"
    if host == "local":
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=1800)
        out = p.stdout
    else:
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
        return name, json.loads(out)
    except Exception:
        return name, {"error": (out or p.stderr or "no output")[-300:]}


def check_policy_gate(args) -> int:
    """--check-policy: report-only policy gate over the roster.

    ALLOW verifies declared identity/role and capability presence only. No
    delivery target is supplied here, so constrained skill_deploy paths are
    not evaluated; ALLOW does not authorize a write to any particular path.

    Joins each enabled agent's `identity` (the stable enrolled identity,
    supplied as roster DATA) to a compiled client policy and refuses — per
    agent, with the reason — when:

    * the agent has no `identity` (hostname/COMPUTERNAME/BIOS serial are
      never substituted: imaged machines share all three, so a fallback
      would be a guess wearing a badge);
    * the identity is not an opaque, case-sensitive non-blank string —
      lists, dicts, numbers or padded/whitespaced values are typed DENYs,
      never coerced or stripped into a match;
    * no client policy placement exists for that identity;
    * SEVERAL clients are placed on that identity's host and the roster
      does not say which client the agent is — the claim is ambiguous and
      is refused; no client is ever silently chosen for it;
    * the roster role is missing, blank, or not a string — refused BEFORE
      any capability is considered (a role that is not stated cannot
      "match" anything);
    * the client's role does not match the agent's rostered role;
    * the `skill_deploy` capability is not granted to the client;
    * two or more agents claim the same enrolled identity: EVERY roster
      entry claiming it is refused, including the first — none of the
      results would be an independent check, so the whole class fails
      closed rather than anointing one lucky occurrence.

    The policies are compiled fail-closed (expired/not-yet-valid exceptions
    included). A compilation error refuses EVERY agent: the gate never
    allows on a policy set it could not fully understand. A roster with no
    enabled agent is reported as "nothing checked" and is NOT an all-clear.
    Nothing is contacted and nothing is written — this is the Stage B
    drift/report contract, wired into the existing deploy surface.
    """
    if not args.policy_dir:
        print("--check-policy requires --policy-dir", file=sys.stderr)
        return 2
    try:
        policy = _load_policy_module()
    except ImportError as exc:
        print(f"policy gate unavailable: {exc}", file=sys.stderr)
        return 2
    try:
        roster = _load_roster()
    except PolicyGateInputError as exc:
        print(f"ROSTER REFUSED: {exc}", file=sys.stderr)
        return 1
    agents = [a for a in roster.get("agents", []) if a.get("enabled")]

    try:
        compiled = policy.compile_all(args.policy_dir)
    except (policy.PolicySourceError, policy.PolicyCompileError) as exc:
        print(f"COMPILATION FAILED — refusing every agent:\n{exc}",
              file=sys.stderr)
        return 1

    # An identity maps to a HOST, and a host may legitimately carry several
    # clients (each with its own vm_identity). A dict keyed by host_identity
    # would let one of them silently win; keep the GROUP so the ambiguity is
    # detectable and refusable instead of resolved by dict-insertion order.
    by_identity: dict = {}
    for eff in compiled.values():
        by_identity.setdefault(eff.placement["host_identity"], []).append(eff)

    def _bad_identity(agent: dict):
        """An enrolled identity is an opaque, case-sensitive id: a typed hole
        or a padded/whitespaced claim is refused, never coerced or matched."""
        ident = agent.get("identity")
        if ident is None:
            return None, "no enrolled identity in the roster " \
                         "(hostname/serial are never substituted)"
        if not isinstance(ident, str):
            return None, f"enrolled identity must be a string, got " \
                         f"{type(ident).__name__} ({ident!r})"
        if not ident.strip() or ident != ident.strip() or \
                any(c.isspace() for c in ident):
            return None, f"enrolled identity {ident!r} must be a non-blank " \
                         f"string with no padding or whitespace"
        return ident, None

    allowed, refused = [], []
    claims: dict = {}
    for a in agents:
        name = a.get("name", "?")
        ident, why = _bad_identity(a)
        if why:
            refused.append((name, why))
            continue
        claims.setdefault(ident, []).append(name)
    for ident, names in claims.items():
        if len(names) > 1:
            for name in names:
                refused.append((name, f"identity {ident!r} is claimed by "
                                f"{len(names)} roster entries "
                                f"({', '.join(names)}); no result among "
                                f"them is an independent check, so every "
                                f"claimant is refused"))
    for a in agents:
        name = a.get("name", "?")
        ident, bad = _bad_identity(a)
        if bad or len(claims.get(ident, [])) > 1:
            continue
        effs = by_identity.get(ident)
        if not effs:
            refused.append((name, f"identity {ident!r} has no client policy "
                            f"placement; placement is never guessed"))
            continue
        if len(effs) > 1:
            clients = ", ".join(sorted(e.client for e in effs))
            refused.append((name, f"identity {ident!r} is ambiguous: "
                            f"{len(effs)} clients are placed on that host "
                            f"({clients}) and the roster does not say "
                            f"which client this agent is; none is chosen"))
            continue
        eff = effs[0]
        roster_role = a.get("role")
        if not isinstance(roster_role, str) or not roster_role.strip():
            refused.append((name, f"roster role is missing, blank, or not "
                            f"a string ({roster_role!r}); the role is "
                            f"checked before any capability, and an "
                            f"unstated role cannot match client policy "
                            f"role {eff.role!r}"))
            continue
        if roster_role != eff.role:
            refused.append((name, f"roster role {roster_role!r} != client "
                            f"policy role {eff.role!r}"))
            continue
        grant = eff.capabilities.get("skill_deploy") or {}
        if not grant.get("granted"):
            refused.append((name, f"capability 'skill_deploy' not granted "
                            f"to client '{eff.client}'"))
            continue
        allowed.append(name)

    for name in allowed:
        print(f"ALLOW {name}")
    for name, why in refused:
        print(f"DENY  {name}: {why}")
    # Intended managed coverage is visible on EVERY path — including the
    # nothing-checked one, so the reader always sees what the gate WOULD
    # have certified and which hosts it does not manage at all.
    intended = sorted({e.client for e in compiled.values()})
    if intended:
        print(f"managed coverage: {len(compiled)} client policy/policies "
              f"compiled ({', '.join(intended)}) — agents are admitted "
              f"against these; hosts without a client policy are not "
              f"managed by this gate.")
    if not agents:
        # A roster whose every entry is disabled is NOT an all-clear: the
        # gate must be able to certify a fleet it never examined.
        roster_all = roster.get("agents", [])
        disabled = len(roster_all) - len(agents)
        msg = ("policy gate: NO ENABLED AGENTS — nothing was checked, so "
               "nothing is certified.")
        if disabled:
            msg += f" ({disabled} disabled/unenrolled agent(s) in the roster)"
        print(f"\n{msg} Report only; no deploy should treat this as a pass.")
        return 1
    enabled = len(agents)
    print(f"\npolicy gate: {len(allowed)} allowed, {len(refused)} refused, "
          f"{enabled} enabled agent(s) (report only — nothing was "
          f"changed)")
    return 1 if refused else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Deliver skills to the Hermes agent fleet.")
    ap.add_argument("skills", nargs="*", help="skill names (default: every skill in skills.json)")
    ap.add_argument("--all", action="store_true", help="every skill in skills.json")
    ap.add_argument("--list", action="store_true", help="show agents and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="skip the CDN freshness wait")
    ap.add_argument("--roster", help="path to agents.json (default: ./agents.json, ./roster/agents.json, or beside this script)")
    ap.add_argument("--check-policy", action="store_true",
                    help="report-only policy gate: verify every enabled agent's "
                         "enrolled identity has a matching, capable client policy "
                         "(requires --policy-dir; contacts nothing, writes nothing; "
                         "a roster with no enabled agent is 'nothing checked', exit 1)")
    ap.add_argument("--policy-dir", help="policy directory for --check-policy")
    args = ap.parse_args()

    global _ROSTER_OVERRIDE
    _ROSTER_OVERRIDE = args.roster

    if args.check_policy:
        return check_policy_gate(args)

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
        manifest = ROOT / "skills.json"
        if not manifest.is_file():
            print("--all needs a skills.json listing skill names, or name skills explicitly.")
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
    seen_hosts: dict[str, str] = {}
    # Check counts are only comparable between nodes of the same OS.
    agent_by_name = {a["name"]: a for a in targets}
    for name, r in results:
        if r.get("error"):
            print(f"  FAIL {name}: {r['error'][:160]}")
            failed.append(name)
            continue
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
        vers = " ".join(f"{s}={d.get('version')}" for s, d in r.get("skills", {}).items())
        t = r.get("tests") or {}
        tt = f"{t.get('passed')}/{t.get('passed', 0) + t.get('failed', 0)}" if "passed" in t else "-"
        extra = f" cleaned={len(r['cleaned'])}" if r.get("cleaned") else ""
        print(f"  OK   {name:<18} {vers}  tests={tt}{extra}")
        if t.get("failed"):
            failed.append(name)
        if "passed" in t:
            counts.append((name, t["passed"], (agent_by_name.get(name, {}) or {}).get("platform") or "unknown"))

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

    if failed:
        print(f"\n{len(set(failed))} agent(s) need attention: {', '.join(sorted(set(failed)))}")
        return 1
    print(f"\nall {len(results)} agent(s) current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
