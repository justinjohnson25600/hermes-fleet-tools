# fleet-report — know what your agents are actually running

Read-only drift reporting for a fleet of [Hermes](https://github.com/NousResearch/hermes-agent)
agents. It answers one question honestly, for every machine at once:

> Is this agent up to date, and if not, what is in the way?

It mutates nothing. No installs, no git writes, no service restarts. Updating is
a separate, supervised decision — this tool exists so that decision is made on
facts instead of assumptions.

## Why not just run `hermes update` on a cron?

That was the original plan on the estate this was written for. Discovery killed
it. Across seven machines:

- every one had uncommitted stashes (1–19 each)
- three were sitting 117–207 commits behind
- three were **shallow clones**, whose "commits behind" arithmetic is meaningless
- one had a CLI addressing a directory that wasn't its git repo at all
- all seven were running the desktop app **from inside the tree an update
  rewrites**
- a single parallel sweep of seven machines drew HTTP 429 from GitHub

An unattended updater would have rewritten live trees under running apps on
machines nobody had inspected. The reporter comes first; automation is only safe
once the fleet is boring.

## Install

Python 3.9+. No dependencies.

```bash
git clone https://github.com/YOUR-ORG/YOUR-TOOLS-REPO
cd YOUR-TOOLS-REPO
python3 fleettools/fleet_report.py --roster path/to/agents.json
```

## The roster

All estate detail lives in one JSON file, so adding a machine is a data edit and
never a code edit:

```json
{
  "agents": [
    {
      "name": "control-box",
      "host": "local",
      "platform": "macos",
      "enabled": true,
      "cli": "/Users/USER/.local/bin/hermes",
      "repo": "$HOME/.hermes/hermes-agent"
    },
    {
      "name": "worker-1",
      "host": "USER@10.0.0.5",
      "platform": "windows",
      "enabled": true,
      "cli": "C:\\Users\\USER\\AppData\\Local\\hermes\\bin\\hermes.exe",
      "repo": "%LOCALAPPDATA%\\hermes\\hermes-agent"
    }
  ]
}
```

`cli` is **required per agent and must not be guessed.** On the estate this was
built for, auto-discovery by globbing `hermes*.exe` found `hermes-acp.exe` — the
ACP protocol binary. It accepted `update --check` and printed *nothing*, which a
naive parser reads as "no update available". Six machines would have reported
green forever. Name the binary explicitly; `hermes --version` on the box tells
you which one is real.

## Usage

```bash
fleet_report.py                      # full table
fleet_report.py --quiet              # only changes + problems (cron mode)
fleet_report.py --json               # machine-readable
fleet_report.py --no-sweep           # read-only: serve the last sweep's stored
                                     # report, banner it if the state dir is old
fleet_report.py --workers 1          # fully serialised, gentlest on rate limits
fleet_report.py --state ./state      # where the heartbeat + last verdicts live
```

Default concurrency is **2, with jitter**, deliberately. Seven agents fetching at
once is enough to get rate-limited.

## Verdicts

| Verdict | Meaning |
|---|---|
| `CURRENT` | HEAD matches upstream. Nothing to do. |
| `BEHIND` | Real commits behind, on a full clone. Trustworthy count. |
| `DIVERGED` | Local commits not upstream. Someone's work lives here — do not clobber it. |
| `SHALLOW` | Depth-limited clone. Counts are arithmetic on truncated history; apparent local commits are an artifact. |
| `BLOCKED` | Rate-limited, repo missing, CLI missing, or CLI pointing at a non-repo. |
| `UNKNOWN` | The probe ran and the output wasn't understood. |
| `UNREACHABLE` | SSH failed or timed out. |

**`UNKNOWN` is a first-class verdict, not an error path.** The whole failure mode
this tool defends against is silence being mistaken for health, so anything
unparsed is reported loudly rather than defaulted to `CURRENT`.

## STALE DATA banner and the cry-wolf fix

`--no-sweep` is the **consumer-side view**: it serves the last sweep's stored
report from the state dir and labels it with a `STALE DATA` banner when the
state dir's heartbeat is older than `--max-age-mins` (default 180). This is
the banner's only home.

The banner originally sat at the top of `main()`, *before* the sweep — so it
always read the **previous** run's heartbeat (~24 h old in the nightly cron)
and fired on fresh data every single night. A guard that cries wolf nightly is
worse than none. Moving it after the sweep would be no better: the sweep
unconditionally writes a fresh heartbeat when it finishes (probing never
raises), so a post-sweep banner would read an age of zero forever — an
unreachable guard is guard deletion. So:

- **the producer** (a normal sweep run) never banners — its own output is
  fresh by construction;
- **the consumer** (`--no-sweep`) labels exactly what it serves, including
  when it is old.

`--no-sweep` is read-only: no probing, no state writes. Missing stored
verdicts are a visible failure (`exit 1`, message on stderr), never silence.
With `--json`, stdout stays valid JSON and the banner goes to stderr.

## Cron mode and the silence contract

`--quiet` prints only agents whose verdict **changed** since the last run, plus
anything currently in a problem state. A steady fleet produces zero output — and
with most cron delivery, zero output means no notification.

That makes silence ambiguous on its own, so every run writes
`state/fleet-heartbeat.json` with a timestamp, per-agent verdicts and HEADs.
Liveness is proved by heartbeat freshness, not by chatty success messages. Pair
it with a deadman check that speaks when the heartbeat ages past ~26h.

```bash
30 2 * * *  cd /path/to/fleetops && python3 /path/to/fleet_report.py --quiet
```

## Design notes (each one paid for)

- **The version string does not track commits.** Six machines reported an
  identical `v0.20.6` while sitting 1 to 207 commits behind. Progress is verified
  by HEAD hash, never by version.
- **`update --check` emits prose, not JSON**, in at least four shapes. Its
  behind-count is treated as advisory; git plumbing is the source of truth.
- **`rev-list --count` lies on a shallow clone.** Two machines at a
  byte-identical HEAD with a byte-identical upstream reported 141 and 1 commits
  behind; the second was depth-1. Only `is-shallow-repository` is trusted as
  evidence — an earlier version also inferred shallowness from a missing
  merge-base and wrongly flagged a 26,480-commit repo.
- **A broken probe is not an unreachable machine.** A malformed remote payload
  once surfaced as six `UNREACHABLE` agents with raw PowerShell XML in the notes
  column, hiding a one-character bug behind what looked like an outage. Shell
  parse errors are now classified `UNKNOWN` with a plain-English cause.
- **`%LOCALAPPDATA%` is a cmd.exe variable.** PowerShell does not expand it; the
  payload expands it explicitly, or every Windows agent reports `BLOCKED`.

## Tests

```bash
python3 -m pytest tests/ -q      # 32 tests
```

Fixtures are real captured output, not invented strings. Tests named
`test_falsify_*` break the thing a guard protects and assert it goes red — a
guard that has never failed is decoration.

## Publishing

`check_no_estate_data.py` runs as a pre-commit hook and refuses to publish
tailnet IPs, LAN addresses, machine names, hardware serials, or real user paths.
If it blocks a commit, the fix is a placeholder or moving the file to a private
repo — never a bypass.

## Licence

MIT.
