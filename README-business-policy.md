# business-policy — compile what each client may actually do (Stage B)

Deterministic, read-only compiler for effective business-client policy. Three
layers — **common → role → client** — fold into one canonical effective
policy per client, plus a content hash, and a **report-only** drift
comparison against a stored snapshot. Nothing here applies a policy, touches
an agent, or writes anywhere except an explicit `snapshot --out`.

## Why a compiler and not "just read the files"

The same class of silent failure as everything else in this repo: a fleet
where client-B's backup job quietly reaches a path the estate protects,
because two layers disagreed and nobody could say what the *effective*
policy was. A layered policy you cannot compile into one deterministic
answer is not a policy; it is three documents and a hope.

## Layout

```text
policy/
├── common.json              shared layer (required)
├── roles/<role>.json        role layer — REQUIRED whenever a client names
│                            that role, even if the file is an empty object
└── clients/<client>.json    per-client layer (required, ≥ 1)
```

Every file is JSON and **fail-closed**: unknown keys are errors, a file that
cannot be fully understood is an error naming the file and field, and *all*
problems are reported together — an operator fixing twelve client files
should not discover them one run at a time.

## Paths: host-independent lexical semantics

Policy paths are compared as **path models**, never with a filesystem:
`/`-absolute strings by `PurePosixPath`, except that leading `//server/share`
explicitly selects the Windows UNC namespace. Drive/UNC/backslash strings use
`PureWindowsPath` (case- and separator-insensitive, per that model's
documented semantics). POSIX comparison is case-sensitive. Overlap is
containment in **both directions**: a grant of a protected path's
descendant (`/var/secrets/db` under `/var/secrets`) AND a broad ancestor
grant covering it (`/var` over `/var/secrets`) both reach the protection
and both require a covering exception; an unrelated sibling
(`/var/secrets-other`) stays usable. The original spellings are preserved
verbatim in records and snapshots.

Refused rather than modeled (an error naming the file and field): relative
paths, `.`/`..` segments (including hidden ones like `/a/./b`), glob
metacharacters (`* ? [ ]`), padding/control characters, and anything else
that would force invented semantics. Enrolled identities (`host_identity`,
`vm_identity`) are **opaque and case-sensitive**: shape-checked (non-blank,
no padding or whitespace, no control characters) and never case-folded or
normalized.

## Merge rules (per key, so drift is always explainable)

- `capabilities` fold in layer order under **narrowing-only** rules: a
  grant set to `false` is **final** — no lower layer may revive it. A
  constrained grant may only be narrowed DOWNWARD by naming paths at or
  under the upper layer's paths; naming an unrelated path, an ANCESTOR of
  them, or unconstraining a constrained grant is a widen and fails.
  Narrowing an unconstrained grant (`null`) to a concrete set is the
  intended refinement and compiles. Aliased spellings of the same modeled
  path collapse, keeping the upper layer's spelling;
- `protected_paths` only ever **grow** across layers — a lower layer cannot
  unprotect a path an upper layer protected, and an aliased spelling of an
  already-protected path adds no second entry;
- `role`, `placement` and `exceptions` are client-layer-only and are taken
  from the client file, never merged across layers;
- a grant **reaching** a protected path is legal **only** while an
  explicit, approved, in-window exception whose path CONTAINS the whole
  reached region (the deeper of the grant path and the protected path)
  covers it. Exceptions live ONLY in client layers, carry `id`,
  `capability`, `path` (required, a modelable absolute path — a scoped
  leaf exception does not bless a broader grant), `approved_by` (required
  non-empty string — `true`/`1` are refused, not coerced), `approved_at`,
  `expires_at`, and are evaluated against the compile time. Both source
  timestamps must include `Z` or an explicit numeric UTC offset; date-only
  or timezone-less approvals are refused, never interpreted as local time
  or silently assigned UTC. Historical snapshot drift retains its original
  UTC interpretation for older naive timestamps. Expired or
  not-yet-in-effect load-bearing exceptions **fail closed** — the compile
  errors, naming the client, capability, grant path, protected path and
  exception id. Non-load-bearing ones compile but are recorded in
  `exceptions_inactive` (status `expired`, `not_yet_in_effect`, or
  `unused` — a declared exception never vanishes from the record);
- a grant with an **empty path list is refused at load**: it would display
  as `granted` while authorising nothing. Revoke with `false`; `null`
  means unconstrained (everything except protected paths);
- a role named by a client must have a `roles/<role>.json` layer file,
  even an empty one — a missing role file is a load error naming the file,
  never a runtime refusal (and never an empty layer invented to hide an
  operator's typo);
- placement is validated fully at LOAD time (unknown keys, blank or
  non-string identities are errors naming the file and field), so a bad
  client file surfaces as a typed diagnostic in the one-pass report, not a
  traceback from deep in the compile.

## Placement and identity — supplied, never guessed

Each client names its host by **stable enrolled identity**, as data in its
own file:

```json
{"role": "worker", "placement": {"host_identity": "enr-host-1"}}
```

The identity is whatever your enrolment process issues (an inventory tag, an
enrolment record id). It is never inferred from hostname, `COMPUTERNAME`, or
BIOS serial — imaged machines share all three, and deploy.py documents what
that does to anything keyed on them.

Multiple clients on one physical host are refused **unless** every client on
that host declares its own distinct `vm_identity` (isolated VM identities):

```json
{"role": "worker", "placement": {"host_identity": "enr-host-1", "vm_identity": "vm-a"}}
```

Two clients silently colocated on one box would each believe they have the
host to themselves; the compile refuses instead. A duplicate `vm_identity`
on the same host is also refused.

## Commands

```bash
python3 -m fleettools.policy --policy-dir DIR compile [--json]
python3 -m fleettools.policy --policy-dir DIR --now TS snapshot --out FILE
python3 -m fleettools.policy --policy-dir DIR --now TS drift --against FILE
```

- `compile` prints the effective policies (or `--json`); writes nothing.
- `snapshot` is the **only writing command**: it stores
  `{tool, format, compiled_at, hashes, policies}` atomically (temp file +
  `os.replace`, fsynced) to the explicit `--out` path.
- `drift` compares a fresh compile against a snapshot and prints
  human-readable entries (or `--json`): `capability_revoked/granted`,
  `capability_paths_changed`, `role_changed`, `placement_changed`,
  `protected_path_added/removed`, `exception_added/expired/removed`,
  `client_added/removed`, `hash_changed`, and the integrity kinds —
  `snapshot_hash_mismatch`, `snapshot_hash_missing`,
  `snapshot_orphan_hash`. Integrity is checked over **every stored
  entry**, including clients that no longer compile: the snapshot's
  tamper detector does not stop covering a client the moment its file is
  deleted. A structurally broken snapshot (non-object policy, missing
  canonical keys, wrong field types) is refused outright with a typed
  error. **Report-only**: it never reverts, patches, or applies anything.
- `--now` pins the evaluation instant so compiles are reproducible;
  default is the current time.

Exit codes: 0 ok · 1 policy/compile/drift error (stderr names every
problem) · 2 usage.

## Canonical hash

`policy_hash` is SHA-256 over the canonical JSON form (sorted keys, no
whitespace), so the hash is stable across runs, machines, and key order in
the source files. Consumers store/compare hashes, never file bytes. The
hash is an integrity check, not authentication: it reports mismatch between
stored policies and their recorded hashes. Editing both policies and hashes
can evade that check; signatures and a trusted key lifecycle remain held.

## Consumer wiring

`deploy.py --check-policy --policy-dir DIR` is the existing-consumer gate:
it joins every enabled roster agent's `identity` field (the same stable
enrolled identity, supplied as roster data) to its client policy and
refuses — per agent, with the reason — on a missing identity (hostnames are
never substituted), an identity that is not an opaque non-blank string
(lists, dicts, numbers, padded or whitespaced values are typed DENYs, never
coerced or stripped — and never case-folded, because the enrolment contract
grants no Windows-principal equivalence), a missing placement, an AMBIGUOUS
placement (several clients on that host and the roster does not name the
client — none is chosen), a missing/blank/non-string roster role (checked
before any capability), a role mismatch, a revoked `skill_deploy`
capability, or a duplicated identity claim (every claimant is refused,
including the first: none of the results is an independent check). A policy
that fails to compile refuses *everyone*; an unparseable roster is a typed
refusal (`ROSTER REFUSED`, not a traceback).

The gate is **non-vacuous**: with zero enabled agents it reports
`NO ENABLED AGENTS — nothing was checked, so nothing is certified` with
the disabled/unenrolled count and exit 1 — an all-disabled roster can
never read as an all-clear. Every run also prints the intended managed
coverage (the compiled client policies the gate admits against), including
on the nothing-checked path; hosts without a client policy are not managed
by this gate. The gate contacts nothing and writes nothing.

## Tests

```bash
python3 -m pytest tests/test_policy_compiler.py tests/test_policy_drift.py \
  tests/test_policy_exception_path.py tests/test_policy_paths.py \
  tests/test_policy_regressions.py tests/test_deploy_policy_gate.py \
  tests/test_gate_host_and_role.py -q
```
