"""Point every profile's SOUL.md at a fleet skill, idempotently.

The directive is one marked block appended to SOUL.md (identity slot #1, loaded at session start;
a running session keeps its prompt, so nothing is mutated mid-conversation). The block names a
skill the profile actually has: a home without the skill, or one opted out of bundled skills, is
left alone. A home with no SOUL.md is reported, never given one: an absent file means the default
identity, and creating a file would replace it.

Rules, in the payload:
  * text outside the markers is never changed; CRLF files stay CRLF; a UTF-8 BOM is kept;
  * a changed block is replaced in place, an unchanged one is a no-op;
  * one marker without the other, or a repeated marker, is an error and the file is untouched;
  * the original bytes are backed up under <home>/backups/soul-directive/ before any write;
  * the file is rewritten in place, keeping its owner and ACL (Windows elevated-SSH trap);
    a symlinked SOUL.md is reported, not followed.

  python soul_directive.py                 # dry run: what would change, per agent and profile
  python soul_directive.py --apply         # write
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy  # noqa: E402  (roster, interpreter probe and profile identity rules live there)

SKILL = "failure-is-not-an-option"
BLOCK = (
    "<!-- fleet-directive:{skill} begin (managed by FleetOps; this block is rewritten) -->\n"
    "## Failure is not an option\n"
    "At the start of every task, load the `{skill}` skill and follow it: assume it can be done, "
    "find a way, finish everything and prove it.\n"
    "<!-- fleet-directive:{skill} end -->"
)

PAYLOAD = r'''import json, os, pathlib, re, sys, time
SKILL = %(skill)r
BLOCK = %(block)r
APPLY = %(apply)r
HOME_OVERRIDE = %(home_override)r
%(honing)s
h = os.environ.get("HERMES_HOME") or os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes")
if HOME_OVERRIDE:
    h = os.path.expanduser(os.path.expandvars(HOME_OVERRIDE))
HOME = pathlib.Path(h) if h and pathlib.Path(h).is_dir() else pathlib.Path.home() / ".hermes"
BEGIN = re.compile(r"<!-- fleet-directive:%%s begin[^\n]*?-->" %% re.escape(SKILL))
END = re.compile(r"<!-- fleet-directive:%%s end -->" %% re.escape(SKILL))


def has_skill(home):
    """An active copy in a category dir, or a native-Hub flat copy; never a dot-dir archive."""
    hits = [f for f in home.glob("skills/*/%%s/SKILL.md" %% SKILL) if not f.parent.parent.name.startswith(".")]
    return bool(hits) or (home / "skills" / SKILL / "SKILL.md").is_file()


def merged(text):
    """(new_text, error). Text outside the managed block is returned unchanged."""
    b, e = list(BEGIN.finditer(text)), list(END.finditer(text))
    if not b and not e:
        body = text.rstrip("\n")
        return (body + "\n\n" if body else "") + BLOCK + "\n", None
    if len(b) != 1 or len(e) != 1 or e[0].start() < b[0].end():
        return None, "malformed directive markers (%%d begin, %%d end); file left untouched" %% (len(b), len(e))
    return text[:b[0].start()] + BLOCK + text[e[0].end():], None


def visit(home, name):
    soul = home / "SOUL.md"
    if name != "default" and (home / ".no-bundled-skills").exists():
        return {"profile": name, "status": "skip", "why": "opted out of bundled skills"}
    if not has_skill(home):
        return {"profile": name, "status": "skip", "why": "skill not installed"}
    if soul.is_symlink():
        return {"profile": name, "status": "warn", "why": "SOUL.md is a symlink; not followed"}
    if not soul.is_file():
        return {"profile": name, "status": "warn", "why": "no SOUL.md (default identity); not created"}
    raw = soul.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw[3 if bom else 0:].decode("utf-8")
    except UnicodeDecodeError:
        return {"profile": name, "status": "error", "why": "SOUL.md is not UTF-8; left untouched"}
    crlf = b"\r\n" in raw
    norm = text.replace("\r\n", "\n")
    new, err = merged(norm)
    if err:
        return {"profile": name, "status": "error", "why": err}
    if new == norm:
        return {"profile": name, "status": "current"}
    verb = "update" if BEGIN.search(norm) else "add"
    if not APPLY:
        return {"profile": name, "status": "would-" + verb}
    stamp = time.strftime("%%Y%%m%%dT%%H%%M%%S")
    bdir = HOME / "backups" / "soul-directive" / name
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / ("SOUL.md." + stamp)).write_bytes(raw)
    out = new.replace("\n", "\r\n") if crlf else new
    data = (b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8")
    # Rewrite in place: the file keeps its owner and ACL. A replacement file minted over an
    # elevated Windows SSH session can come out Administrators-only, and the non-elevated
    # gateway would then lose the profile's identity. The backup above is the recovery path.
    with open(soul, "r+b") as fh:
        fh.write(data)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())
    return {"profile": name, "status": verb + "ed" if verb == "add" else "updated"}


rep = {"host": os.environ.get("COMPUTERNAME") or os.uname().nodename, "home": str(HOME), "profiles": []}
if not HOME.is_dir():
    rep["error"] = "no Hermes home found"
else:
    homes = [(HOME, "default")]
    root = HOME / "profiles"
    if root.is_dir():
        homes += [(p, p.name) for p in sorted(root.iterdir()) if is_profile(p)]
    for home, name in homes:
        try:
            rep["profiles"].append(visit(home, name))
        except OSError as exc:
            rep["profiles"].append({"profile": name, "status": "error", "why": str(exc)[:200]})
print(json.dumps(rep))
'''


def render(apply: bool, home_override: str = "", skill: str = SKILL) -> str:
    return PAYLOAD % {"skill": skill, "block": BLOCK.format(skill=skill), "apply": apply,
                      "home_override": home_override, "honing": deploy.HONING_SRC}


def run_on(agent: dict, apply: bool, skill: str) -> tuple[str, dict]:
    code = render(apply, agent.get("hermes_home") or "", skill)
    if agent["host"] == "local":
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300)
        out = p.stdout
    elif deploy.is_posix_remote(agent):
        out, err = deploy.run_posix_payload(agent["host"], agent.get("python") or "python3", code, 300)
        if err:
            return agent["name"], {"error": err}
    else:
        py, note = deploy.resolve_python(agent["host"], agent.get("python"))
        if not py:
            return agent["name"], {"error": note}
        b64 = base64.b64encode(code.encode()).decode()
        writer = (f'"{py}" -c "import base64,os,sys;'
                  "open(os.environ['TEMP']+chr(92)+'_hssoul.py','wb')"
                  '.write(base64.b64decode(sys.stdin.read()))"')
        w = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", agent["host"], writer],
                           input=b64, capture_output=True, text=True, timeout=180)
        if w.returncode:
            return agent["name"], {"error": f"payload write failed: {(w.stderr or '').strip()[:200]}"}
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", agent["host"], f'"{py}" %TEMP%\\_hssoul.py'],
                           capture_output=True, text=True, timeout=300)
        out = p.stdout
    try:
        return agent["name"], json.loads(out)
    except ValueError:
        return agent["name"], {"error": (out or p.stderr or "no output").strip()[-300:]}


def summarize(name: str, rep: dict) -> tuple[list[str], bool]:
    """Report lines for one agent and whether it failed. Skips are quiet; warnings and errors name the profile."""
    if rep.get("error"):
        return [f"  FAIL {name}: {rep['error']}"], True
    lines, failed, counts = [], False, {}
    for p in rep.get("profiles", []):
        counts[p["status"]] = counts.get(p["status"], 0) + 1
        if p["status"] == "error":
            failed = True
            lines.append(f"  FAIL {name:<18} {p['profile']}: {p['why']}")
        elif p["status"] == "warn":
            lines.append(f"  WARN {name:<18} {p['profile']}: {p['why']}")
    tally = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return [f"  {'FAIL' if failed else 'OK  '} {name:<18} {tally}"] + lines, failed


def main() -> int:
    ap = argparse.ArgumentParser(description="Point every profile's SOUL.md at a fleet skill.")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--skill", default=SKILL)
    ap.add_argument("--roster")
    args = ap.parse_args()
    deploy._ROSTER_OVERRIDE = args.roster
    cfg = deploy.load_agents()
    targets = deploy.deploy_targets(cfg["agents"])
    print(f"SOUL.md directive -> {args.skill} on {len(targets)} agent(s){'' if args.apply else ' [DRY RUN]'}")
    for a in cfg["agents"]:
        if a not in targets:
            print(f"  SKIP {a['name']}: {a.get('blocked_reason', 'disabled')[:110]}")
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as ex:
        results = list(ex.map(lambda a: run_on(a, args.apply, args.skill), targets))
    failed = 0
    for name, rep in results:
        lines, bad = summarize(name, rep)
        failed += bad
        print("\n".join(lines))
    print(f"\n{failed} agent(s) need attention" if failed else "\nall reachable agents processed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
