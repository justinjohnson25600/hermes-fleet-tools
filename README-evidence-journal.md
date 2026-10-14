# evidence-journal — append-only facts, human-gated promotion (Stage E)

A tenant-scoped, append-only evidence journal with durable concurrent-write
protection, plus a readiness gate and an explicit approval sequence.
Approver names are attestations, not authenticated human identities; this
library does not authorise production promotion. Proposals are stored text for people; nothing here
self-edits, replays a workflow, or fails an identity over.

## Portable tenant admission

Tenant names are opaque. Case/NFC aliases are refused on every volume, not
normalized into a shared store. Exact-spelling reopen requires a conflict-free
namespace. If a sensitive volume already contains folding siblings, **all**
conflicting spellings are refused, including exact matches; no scan-order winner
is selected and neither store is modified. Resolving legacy namespace conflicts
is a separate operator action, never automatic deletion or merging. Unopenable
directory or lock-file acquisition is a typed `JournalPathRefused` (CLI exit 2);
corrupt history remains `JournalCorrupt` (CLI exit 1).

## Why append-only with a torn-tail refusal

An evidence store that can be edited after the fact is not evidence. One
JSONL file per tenant, one record per line, appended under an exclusive
lock; the only permitted "change" is a new record. A crash mid-append can
leave a truncated final line — that state is **refused on read and append**
(naming the file, telling you to truncate by hand after inspecting) rather
than silently skipped, because a partially written record is not a record.
A journal that skips a torn line trains you to trust a store that lies.

## Concurrency (durable, exercised by tests)

- a persistent local lock file uses POSIX `flock` or Windows byte-range locking;
- a live lock is waited on (`lock_timeout`, then `JournalLocked`);
- a live owner is never evicted by age; process death releases the OS lock;
- never delete the lock file to recover ownership: doing so can split lock identity;
- the append rewrites the file atomically (temp file + fsync +
  `os.replace`) and the seq/id assignment happens under the lock, so ids
  are unique and the sequence is monotonic across valid gaps (a
  hand-repaired journal may skip ids; the next append takes
  max(existing seq) + 1). 4 processes × 5 appends land as
  seq 1..20 with no duplicates — there is a real-subprocess test for it.
- On Windows, publication can be refused because another handle holds
  the data file open (editors, scanners, an open reader): `os.replace`
  fails with PermissionError/winerror 5. That refusal is translated to
  `JournalLocked` naming the exact path; prior bytes are preserved, no
  temp debris is left, and the journal is immediately usable again.
  POSIX has no replace-victim hold, so a POSIX PermissionError keeps its
  own meaning and is not translated. A native-host test covers the real
  open-handle case.
- Reads co-operate with writers: `read`, `read_approvals`, and
  `status_snapshot` validate every persisted row's shape and serialise
  on the same lock as appends (a transaction holder reuses its own lock
  instead of re-entering). A store of valid-JSON nonsense — null lines,
  lists, rows missing required fields or the `id`/`seq` envelope, rows
  of an unknown kind, duplicate sequences — is `JournalCorrupt` naming
  the store, line, and field, never the payload. Legacy rows that
  predate the cross-subject append rules still read: shape is
  validated, not reference coupling.

## Records — closed schemas, local privacy boundary

`incident`, `repair`, `lesson_proposal` (+ a separate `approvals` store).
Schemas are **closed**: unknown fields are `RecordError`, never payload.
Dedicated raw-output payload fields are rejected. Text fields such as summary
and proposal can still contain sensitive content: callers must sanitise them,
keep tenant stores local, and apply access controls. A closed schema alone
does not prove that text is private or safe to share.

- **incident**: `subject`, `reported_by`, `summary`; optional `ts`,
  `recurrence_of` (must reference an existing incident),
  `evidence`, `metrics`.
- **repair**: `subject`, `performed_by`, `for_incident` (must reference an
  existing incident), `outcome`, `summary`; optional `evidence`, `metrics`.
- **lesson_proposal**: `subject`, `proposed_by`, `proposal`, `basis`
  (≥ 1 existing record id). Proposals are inert: readiness counts them,
  nothing applies them.
- **evidence** is a list of `{source, ref, verified}` **references** to
  independent systems (a monitor, an audit log), never copies of their
  content.
- **metrics** (advisory, never gating): `quality` (0..1),
  `cost_units` (≥ 0), `latency_ms` (≥ 0). Unknown metric keys are refused.
- **timestamps**: every stored record carries a `ts` (approvals an
  `approved_at`). Timestamps are caller-declared display facts and may
  carry any ISO-8601 offset (`2026-01-01T23:00-05:00` is fine); a
  garbage date is a typed `RecordError` — the CLI's exit-2 class —
  whether passed as the record's `ts`, the `append(ts=...)` argument,
  or an approval's `approved_at`. ORDERING never uses them: the
  lock-assigned `seq` is the commit order, and it can never be
  backdated (each append takes max(existing seq) + 1 under the lock).

Cross-references must resolve inside the same tenant journal at append
time: a repair for an unknown or non-incident record is refused, a
recurrence of an unknown incident is refused, a proposal citing unknown
records is refused. They must also be SAME-SUBJECT: a repair, recurrence,
or proposal basis pointing at another subject's record — even one that
resolves inside the tenant — is refused, and the journal file is unchanged
after the refusal. Readiness therefore never counts another subject's
recurrence for or against a subject, including legacy rows on disk that
predate this rule.

## Readiness — evidence math, nothing else

`readiness --subject S` (`assess()` in code) returns ELIGIBLE only when:

1. an incident exists for the subject;
2. **every incident chain of the subject is settled by a resolved repair
   FOR an incident in that same chain** — the chain is walked through
   `recurrence_of` links (same subject only; a missing, cyclic, or
   cross-subject legacy link fails closed as an untraceable chain
   instead of being ignored), and a repair of a different root — even a
   later one for another subject — never settles this chain. A settling
   repair must have a later lock-assigned commit sequence than the incident.
   A fresh
   unrepaired incident makes the subject INELIGIBLE;
3. **≥ N (default 2, a positive integer; a bool is refused) distinct
   verified evidence sources** span the subject's incident(s) and
   resolved repair(s). Unverified references do not count;
   references whose `source` is one of the actors (`reported_by` /
   `performed_by`) are **not independent** and do not count;
4. no recurrence of the subject remains unresolved after its chain's latest
   committed repair. Compare lock-assigned sequences, never caller dates:
   a later committed recurrence blocks even when its display date is backdated.
   Only the assessed subject's own recurrences gate it.

Insufficient verified independent evidence is INELIGIBLE and says why in
`reasons`. Metrics are surfaced as `metrics_present` (advisory metadata for
cost/quality/latency review) and never gate. `rc=0` eligible · `rc=1`
ineligible (with the reasons) · `--json` for consumers.

## Promotion — human approvals end to end

`approve --subject S --stage STAGE --approved-by NAME` records a human
approval attestation. Stages are **sandbox → holdout → canary → soak**, enforced:

- each stage requires the previous stage's approval first;
- holdout/canary/soak additionally require readiness **ELIGIBLE** at
  approval time — the gate runs inside the same locked section, ahead of
  the write;
- approvals are append-only: re-approval is refused, never overwritten.

`promotion --subject S` prints the four stages with who approved and what's
next; rc=0 only when the subject is promotion-ready right now (rc=1
otherwise), and `promotion_status()` deliberately separates the two
questions:

- `sequence_complete` is **history**: all four stages carry a recorded
  human approval.
- `complete` is **current**: the sequence is complete AND readiness is
  ELIGIBLE *now*. A subject whose evidence regressed after its fourth
  approval (a new unresolved recurrence, a fresh unrepaired incident)
  reports `sequence_complete: true, readiness_verdict: INELIGIBLE,
  complete: false` with the reasons — the old sequence-only report
  would have kept saying complete.

The CLI `readiness` and `promotion` commands build their answer from one
locked, atomic snapshot (`Journal.status_snapshot`): the records and the
verdict describe the same instant of the journal, not two reads with an
append landing between them. When all four are human-approved the
sequence is complete — and even then this tool promotes nothing: it
records that a human approved each stage. **No automatic workflow
replay; no identity failover** — an actor whose stage fails is reported,
not retried as someone else.

## Tenant scoping

The tenant name **is** the directory (`--journal ROOT --tenant T` →
`ROOT/T/journal.jsonl`). Unsafe names are refused at construction, so a
tenant cannot escape the root: path separators, spaces, dotfiles,
trailing dots/spaces (Windows trims or refuses them), the Windows
reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1..9`,
`LPT1..9`, including dotted forms like `con.txt`), and any alias that
differs only by case from a sibling directory that already exists (on
case-insensitive volumes both names would silently open ONE store).
Names are opaque identifiers and are never normalised or case-folded —
the colliding alias is refused with its exact spelling. Ids are
prefixed with the tenant name (`tenant-a-000042`), so records are
unambiguous if ever exported.

## Commands

```bash
python3 -m fleettools.journal --journal DIR --tenant T append --file REC.json
python3 -m fleettools.journal ... read [--kind K] [--json]
python3 -m fleettools.journal ... readiness --subject S [--json]
python3 -m fleettools.journal ... approve --subject S --stage STAGE --approved-by NAME
python3 -m fleettools.journal ... promotion --subject S [--json]
```

Exit codes: 0 ok · eligible / promotion-ready now · 1 refused
(ineligible / gate / locked / torn journal / not promotion-ready) ·
2 bad record (stderr names the field) or usage.

## Tests

```bash
python3 -m pytest tests/test_journal_store.py tests/test_journal_readiness.py tests/test_journal_transactions.py tests/test_journal_cross_subject.py tests/test_journal_hardening.py tests/test_journal_commit_order.py -q
```

`test_journal_hardening.py` carries the review-round regressions (each
was red on the pre-fix source): typed caller-date refusals, shape-validated
locked reads, gap-surviving sequence, per-chain readiness,
regression-safe promotion reporting, the diagnosable Windows
publication refusal, and the tenant-namespace refusals. Two tests there
run only on a real Windows host (`os.name == "nt"`, never faked): the
open-handle publication refusal and rapid lock-cycle reuse.

`test_journal_commit_order.py` proves backdated recurrence refusal, later
commit settlement independent of display dates, and transaction ownership:
only the owning process/thread may reuse a held transaction for reads.
