#!/usr/bin/env python3
"""Stage E: tenant-scoped evidence journal + human-gated promotion. APPEND-ONLY.

One JSONL file per tenant, one record per line, append-only by contract.
Writes are serialised by an OS lock on a persistent local file:

* POSIX flock / Windows byte-range locking exclude competing writers;
* lock age never evicts a live owner; process death releases the OS lock;
* lock files are never unlinked, avoiding split-inode ownership;
* the append itself is one ``os.replace`` of a fully rewritten file, fsynced
  before the lock drops: publication is process-crash atomic, not a power-loss
  durability guarantee. A truncated final line from damage or older writes is refused on
  both read and append rather than skipped. On Windows a replace refused
  because another handle holds the data file open (winerror 5) is
  translated to ``JournalLocked`` naming the exact path — prior bytes are
  preserved and no temp debris is left;
* reads serialise on the same lock and validate every persisted row's
  shape: a store of valid-JSON nonsense (null / list / wrong envelope /
  unknown kind / duplicate seq) is ``JournalCorrupt`` naming the store,
  line, and field — never the payload.

The lock-assigned ``seq`` is the ordering authority. Timestamps are
caller-declared display facts (any ISO-8601 offset accepted; a garbage
date is a typed ``RecordError``, the CLI's exit-2 class) and never order
records; each append commits at max(existing seq) + 1, so the sequence
stays monotonic across valid gaps and can never be backdated.

Record kinds (closed schemas — unknown fields are RecordError, never payload):

* ``incident`` — subject/summary/actor labels. Dedicated raw payload fields
  are refused; free text still requires caller sanitisation and local access
  controls. Closed schemas do not establish a security boundary.
* ``repair`` — what was done about an incident, its outcome, and the
  *independent outcome-evidence references* (source + external ref +
  verified flag). References, not copies: evidence lives in the external
  system named by ``source``.
* ``lesson_proposal`` — proposed lessons as stored text for a human. Nothing
  in this module applies one, edits a skill, or mutates any agent state.
* ``approval`` — a named approval attestation (separate store), not an
  authenticated human identity or production promotion authorisation.

Readiness / promotion (in fleettools.journal.assess / promotion_status):

* ELIGIBLE requires >= N verified, independent evidence SOURCES (N a
  positive integer; a bool is refused), EVERY incident chain of the
  subject settled by a resolved repair for an incident in that chain
  (chain links are walked same-subject only; missing, cyclic, or
  cross-subject legacy links fail closed), and no unresolved recurrence
  after its chain's latest committed repair — commit sequences, never display
  dates, determine settlement. Only the assessed subject's own recurrences gate it.
  Actor-owned evidence is not independent. Insufficient verified
  evidence is INELIGIBLE, full stop.
* Promotion follows sandbox -> holdout -> canary -> soak, each stage
  requiring an explicit human approval record; holdout/canary/soak
  additionally require the readiness gate to be ELIGIBLE at approval time.
  Approvals are append-only records — re-approval is refused, not
  overwritten. ``promotion_status`` separates ``sequence_complete``
  (history: all four approvals recorded) from ``complete`` (the sequence
  AND readiness ELIGIBLE now), so a post-approval evidence regression is
  never reported as complete; ``Journal.status_snapshot`` gives the CLI
  one locked, atomic readiness+promotion answer.
* No automatic workflow replay, no identity failover, no self-editing:
  this module reports and gates; only a human closes a stage.

Tenant admission is fail-closed on real volumes: an exclusive mkdir, then
inspection of the exact directory entry the volume actually stored — never
a name-only decision — admits a journal. A case- or normalization-aliased
spelling of an existing tenant directory is refused with
``JournalAliasRefused`` on every volume (opaque tenant names are never
normalized into sharing; a name that could mean one store here and two on
another volume is refused everywhere). On sensitive volumes with pre-existing
folding siblings, every conflicting spelling is refused, including exact
entries; conflict-free exact reopen remains valid. An unopenable root/tenant
path (including lock-file acquisition) is a typed ``JournalPathRefused`` — the
CLI's exit 2 — not a raw OSError.

Commands::

    python3 -m fleettools.journal --journal DIR --tenant T append --file REC.json
    python3 -m fleettools.journal ... read [--kind K] [--json]
    python3 -m fleettools.journal ... readiness --subject S [--json]
    python3 -m fleettools.journal ... approve --subject S --stage STAGE \
        --approved-by NAME
    python3 -m fleettools.journal ... promotion --subject S [--json]

Exit codes: 0 ok / eligible / promotion-ready now, 1 refused (corrupt /
ineligible / gate / locked / missing / not promotion-ready), 2 bad record,
path or alias refusal, or usage.
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROMOTION_STAGES = ("sandbox", "holdout", "canary", "soak")
EVIDENCE_STAGES = ("holdout", "canary", "soak")   # need readiness ELIGIBLE
APPROVAL_REQUIRED_FIELDS = {"subject", "stage", "approved_by", "approved_at"}

SCHEMAS = {
    "incident": {
        "required": {"kind", "subject", "reported_by", "summary"},
        "optional": {"ts", "recurrence_of", "evidence", "metrics"},
    },
    "repair": {
        "required": {"kind", "subject", "performed_by", "for_incident",
                     "outcome", "summary"},
        "optional": {"ts", "evidence", "metrics"},
    },
    "lesson_proposal": {
        "required": {"kind", "subject", "proposed_by", "proposal", "basis"},
        "optional": {"ts"},
    },
}

METRIC_KEYS = {"quality", "cost_units", "latency_ms"}
RECORD_STRING_FIELDS = ("subject", "summary", "proposal", "reported_by",
                        "performed_by", "proposed_by", "outcome",
                        "for_incident", "recurrence_of")

METRIC_LIMITS = {
    "quality": (0.0, 1.0),
    "cost_units": (0.0, None),
    "latency_ms": (0.0, None),
}


class RecordError(ValueError):
    """A record violates its closed schema and is refused, never coerced."""


class JournalLocked(Exception):
    """Another writer owns the journal's OS lock."""


class JournalCorrupt(Exception):
    """The journal file is torn/malformed; refused, never skipped."""


class ApprovalRefused(Exception):
    """The human approval gate refused: sequence, evidence, or duplicate."""


class JournalAliasRefused(ValueError):
    """A tenant name aliases an existing store on this volume.

    Case-insensitive (macOS default, Windows) and normalization-insensitive
    (APFS HFS+) volumes open two differently-cased or differently-spelled
    names as the SAME directory: two tenant handles, one store, split lock
    identity. Tenant names are opaque identifiers, so they are never
    normalized into sharing — the aliased spelling is refused with both
    spellings named. On a volume where the two spellings would be distinct
    directories the refusal is conservative (a name that is ambiguous
    somewhere in the estate must not silently mean two different stores
    elsewhere). Exact-spelling reopen is supported only in a conflict-free
    namespace. If a sensitive volume already holds multiple folding siblings,
    all conflicting spellings are refused, even their exact entries; admission
    never guesses which existing tenant should win or alters their bytes.
    """


class JournalPathRefused(OSError):
    """A journal root/tenant path cannot be opened for use.

    Wraps the raw OSError (name too long, root is a file, ...) with the
    journal context; the CLI translates it to exit 2. Corruption, alias
    refusal and lock conflicts keep their own classes and exits.
    """


WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"] +
    [f"COM{i}" for i in range(1, 10)] + [f"LPT{i}" for i in range(1, 10)]
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value) -> datetime:
    """Parse an accepted ISO-8601 form to an aware UTC datetime.

    Raises RecordError (not a bare ValueError) so a caller-supplied bad
    date is a typed record refusal — the CLI's exit-2 class — while torn
    or hand-edited persisted rows stay JournalCorrupt.
    """
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise RecordError(f"ts {value!r} is not an ISO-8601 timestamp") \
            from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _norm_ts(value) -> str:
    """Normalise any accepted ISO-8601 form to one canonical string."""
    return _parse_ts(value).isoformat()


def _check_evidence(evidence, where: str) -> None:
    """Evidence is a list of {source, ref, verified} REFERENCES.

    References to independent evidence stored elsewhere — never copies of
    logs, transcripts, or payloads, which the schema has no room for.
    """
    if not isinstance(evidence, list):
        raise RecordError(f"{where}.evidence must be a list of "
                          f"{{source, ref, verified}} references")
    for i, e in enumerate(evidence):
        label = f"{where}.evidence[{i}]"
        if not isinstance(e, dict) or set(e) != {"source", "ref", "verified"}:
            raise RecordError(
                f"{label} must be exactly {{source, ref, verified}}")
        if not isinstance(e["source"], str) or not e["source"]:
            raise RecordError(f"{label}.source must be a non-empty string")
        if not isinstance(e["ref"], str) or not e["ref"]:
            raise RecordError(f"{label}.ref must be a non-empty string")
        if not isinstance(e["verified"], bool):
            raise RecordError(f"{label}.verified must be a boolean")


def _check_metrics(metrics, where: str) -> None:
    if not isinstance(metrics, dict):
        raise RecordError(f"{where}.metrics must be an object")
    unknown = sorted(set(metrics) - METRIC_KEYS)
    if unknown:
        raise RecordError(f"{where}.metrics: unknown key(s) "
                          f"{', '.join(unknown)} (allowed: "
                          f"{', '.join(sorted(METRIC_KEYS))})")
    for k, v in metrics.items():
        lo, hi = METRIC_LIMITS[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                (isinstance(v, float) and not math.isfinite(v)) or v < lo \
                or (hi is not None and v > hi):
            limit = f"{lo}..{hi}" if hi is not None else f">= {lo}"
            raise RecordError(f"{where}.metrics.{k} must be a number "
                              f"{limit}")


def _validate_unique_envelopes(rows: list, store: str) -> None:
    """IDs and commit sequences identify exactly one row in each store."""
    seen: set = set()
    ids: set = set()
    for i, r in enumerate(rows, 1):
        seq = r.get("seq") if isinstance(r, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise JournalCorrupt(f"{store}: line {i} seq must be an integer")
        if seq in seen:
            raise JournalCorrupt(f"{store}: duplicate seq {seq}")
        seen.add(seq)
        if r['id'] in ids:
            raise JournalCorrupt(f"{store}: line {i} duplicate id")
        ids.add(r['id'])


def _validate_approval_identity_uniqueness(approvals: list,
                                           store: str = "approvals") -> None:
    """One approval per (subject, stage) — at read time, not only append.

    The append guard refuses a repeated (subject, stage); a stored pair
    with distinct seq/id (hand merge, older writer, restore splice) must
    equally be refused wherever the store is read, or one approval row
    silently shadows the other in every consumer's stage map — a
    last-write-wins over a human approval identity.
    """
    taken: set = set()
    for i, r in enumerate(approvals, 1):
        if not isinstance(r, dict) or any(
                not isinstance(r.get(field), str) or not r[field]
                for field in APPROVAL_REQUIRED_FIELDS):
            raise JournalCorrupt(
                f"{store}: line {i} requires string subject, stage, approved_by and approved_at")
        key = (r["subject"], r["stage"])
        if key in taken:
            raise JournalCorrupt(
                f"{store}: line {i} repeats the approval identity "
                f"(subject, stage) of an earlier line; one stage of one "
                f"subject is exactly one approval, distinct seq/id do not "
                f"make a second one valid")
        taken.add(key)


def _json_object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecordError('JSON object contains a repeated field')
        result[key] = value
    return result


def _check_utf8_strings(value) -> None:
    try:
        _walk_utf8_strings(value)
    except RecursionError as exc:
        raise RecordError('record nesting exceeds supported limits') from exc


def _walk_utf8_strings(value) -> None:
    """JSON permits surrogate escapes that cannot be published as UTF-8."""
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise RecordError('strings must contain valid UTF-8 Unicode, not lone surrogates') from exc
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RecordError('JSON object keys must be strings')
            _walk_utf8_strings(key)
            _walk_utf8_strings(item)
    elif isinstance(value, list):
        for item in value:
            _walk_utf8_strings(item)


def _check_record(rec: dict) -> dict:
    """Validate one record against its closed schema; returns a clean copy."""
    if not isinstance(rec, dict):
        raise RecordError("record must be a JSON object")
    _check_utf8_strings(rec)
    kind = rec.get("kind")
    if not isinstance(kind, str) or kind not in SCHEMAS:
        raise RecordError(f"unknown record kind {kind!r} (expected one of "
                          f"{', '.join(sorted(SCHEMAS))})")
    schema = SCHEMAS[kind]
    known = schema["required"] | schema["optional"]
    missing = sorted(schema["required"] - set(rec))
    if missing:
        raise RecordError(f"record is missing required field(s): "
                          f"{', '.join(missing)}")
    unknown = sorted(set(rec) - known)
    if unknown:
        raise RecordError(f"unknown field(s) {', '.join(unknown)} — record "
                          f"schemas are closed; this journal stores facts "
                          f"and references, not payloads")
    for f in RECORD_STRING_FIELDS:
        if f in rec and (not isinstance(rec[f], str) or not rec[f].strip()):
            raise RecordError(f"{f} must be a non-empty string")
    if "basis" in rec:
        if not isinstance(rec["basis"], list) or not rec["basis"] or \
                any(not isinstance(x, str) or not x for x in rec["basis"]):
            raise RecordError("basis must be a list of record ids")
    if "ts" in rec:
        try:
            _norm_ts(rec["ts"])
        except (ValueError, TypeError) as exc:
            raise RecordError(f"ts is not an ISO-8601 timestamp") from exc
    if "evidence" in rec:
        _check_evidence(rec["evidence"], "record")
    if "metrics" in rec:
        _check_metrics(rec["metrics"], "record")
    return dict(rec)


# ---------------------------------------------------------------------------

class Journal:
    """Append/read access to one tenant's journal under ``root``."""

    LOCK_FILENAME = "journal.lock"
    DATA_FILENAME = "journal.jsonl"
    APPROVALS_FILENAME = "approvals.jsonl"

    def __init__(self, root, tenant: str, lock_timeout: float = 30.0):
        _check_utf8_strings(tenant)
        if not isinstance(tenant, str) or not tenant or "/" in tenant or \
                "\\" in tenant or tenant in (".", "..") or \
                tenant != tenant.strip() or " " in tenant or \
                any(ord(c) < 32 or ord(c) == 127 for c in tenant) or \
                tenant.startswith(".") or tenant.endswith((".", " ")) or \
                ":" in tenant or "<" in tenant or ">" in tenant or \
                '"' in tenant or "|" in tenant or "?" in tenant or \
                "*" in tenant or tenant.upper() in \
                WINDOWS_RESERVED_DEVICE_NAMES or \
                (("." in tenant) and
                 tenant.split(".", 1)[0].upper() in
                 WINDOWS_RESERVED_DEVICE_NAMES) or \
                self._collides_case_insensitively(root, tenant):
            stored = self._stored_entry_name(root, tenant)
            if stored is not None and stored != tenant:
                # Alias of an existing sibling: typed refusal with BOTH
                # spellings named — auditable, never normalized into
                # sharing. A ValueError subclass, so generic callers that
                # only ever saw the old unsafe-name ValueError keep
                # working.
                raise JournalAliasRefused(
                    f"tenant {tenant!r} aliases existing journal directory "
                    f"{stored!r} on this filesystem (case- or "
                    f"normalization-insensitive volume); tenant names are "
                    f"opaque and are never normalized into sharing")
            raise ValueError(f"tenant name {tenant!r} is not a safe "
                             f"single directory name")
        self.root = Path(root)
        self.dir = self.root / tenant
        self.tenant = tenant
        self.lock_timeout = lock_timeout
        self._lock_fd = None
        self._transaction_owner = None
        self._thread_lock = threading.Lock()

    @staticmethod
    def _stored_entry_name(root, requested: str) -> str | None:
        """The exact existing directory entry `requested` would open.

        Directory listing order is filesystem-defined and a case- or
        normalization-insensitive volume may store a different byte
        sequence than the one requested, so entries are compared
        component-wise (case-folded, NFC-normalized) — the one true test
        of what the volume actually keeps. Prefer any conflicting folding
        sibling over an exact entry: a pre-existing ambiguous namespace must
        not select a winner based on scandir order. None: no such entry.
        """
        import unicodedata
        wanted = unicodedata.normalize("NFC", requested).casefold()
        exact = None
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if unicodedata.normalize("NFC", entry.name).casefold() == wanted:
                        if entry.name != requested:
                            return entry.name
                        exact = entry.name
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as exc:
            raise JournalPathRefused(f"cannot inspect journal root {root}: {exc}") from exc
        return exact

    @staticmethod
    def _collides_case_insensitively(root, tenant: str) -> bool:
        """Would storing `tenant` be ambiguous with an existing sibling?

        Advisory precheck only: the authoritative admission is the
        exclusive mkdir plus the exact stored entry in
        :meth:`_admit_directory` — a sibling created between this scan and
        the mkdir is invisible here. Case/normalization folding follows
        the stored-entry comparison; the colliding spelling is refused
        with its exact name (never normalized into sharing).
        """
        stored = Journal._stored_entry_name(root, tenant)
        return stored is not None and stored != tenant

    # -- paths ------------------------------------------------------------

    @property
    def data_path(self) -> Path:
        return self.dir / self.DATA_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.dir / self.LOCK_FILENAME

    @property
    def approvals_path(self) -> Path:
        return self.dir / self.APPROVALS_FILENAME

    # -- lock -------------------------------------------------------------

    def _admit_directory(self) -> None:
        """Admit this tenant's directory on the real volume, fail-closed.

        The exclusive mkdir is the serialization point; ``FileExistsError``
        is never accepted as "fine" — the exact entry the volume actually
        stores is inspected instead, because a case- or
        normalization-insensitive volume may have stored a different byte
        sequence than the one requested (E410: the old name-only ``lower()``
        precheck let an NFC/NFD aliasing spelling silently open another
        tenant's directory — two handles, one store, split lock identity).
        The entry is inspected on the success path too: a volume that
        rewrote the requested bytes would otherwise be trusted unseen, and
        on store-what-you-spell volumes the exact spelling matches here
        exactly as it does on reopen. A sibling created between the
        constructor's advisory precheck and this mkdir is caught here —
        the race class is settled by the stored entry itself, never by
        names alone.
        """
        created = False
        try:
            if self.root.exists() and not self.root.is_dir():
                raise JournalPathRefused(f"journal root {self.root} is not a directory")
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                self.dir.mkdir(exist_ok=False)
                created = True
            except FileExistsError:
                pass  # The stored spelling and type still need inspection.
        except OSError as exc:
            raise JournalPathRefused(
                f"cannot open journal directory {self.dir} for tenant "
                f"{self.tenant!r}: {exc.strerror or exc}") from exc
        stored = self._stored_entry_name(self.root, self.tenant)
        if stored is None:
            # The directory we just created (or found) is gone, or the
            # root became unreadable: refuse closed, never guess.
            raise JournalPathRefused(
                f"journal directory {self.dir} for tenant "
                f"{self.tenant!r} could not be inspected after "
                f"creation; refusing closed") from None
        if stored != self.tenant:
            if created:
                try:
                    self.dir.rmdir()
                except OSError as exc:
                    raise JournalPathRefused(
                        f"alias refused but empty-directory cleanup failed for {self.dir}: {exc}") from exc
            raise JournalAliasRefused(
                f"tenant {self.tenant!r} aliases existing journal "
                f"directory {stored!r} on this filesystem (case- or "
                f"normalization-insensitive volume); tenant names are "
                f"opaque and are never normalized into sharing") from None
        if not os.path.isdir(self.dir):
            raise JournalPathRefused(
                f"journal path {self.dir} for tenant {self.tenant!r} "
                f"exists and is not a directory") from None

    def _acquire(self) -> None:
        self._admit_directory()
        deadline = time.monotonic() + self.lock_timeout
        if not self._thread_lock.acquire(timeout=self.lock_timeout):
            raise JournalLocked(f"journal {self.tenant} has an active transaction")
        fd = None
        try:
            try:
                fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:
                raise JournalPathRefused(
                    f"cannot open journal lock {self.lock_path}: {exc}") from exc
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._lock_fd = fd
                    self._transaction_owner = (os.getpid(), threading.get_ident())
                    return
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise JournalLocked(f"journal {self.tenant} is locked by another writer") from error
                    time.sleep(min(0.05, max(0, deadline-time.monotonic())))
        except BaseException:
            if fd is not None:
                os.close(fd)
            self._thread_lock.release()
            raise

    def _release(self) -> None:
        fd = self._lock_fd
        if fd is None:
            raise RuntimeError("journal transaction is not held")
        if self._transaction_owner != (os.getpid(), threading.get_ident()):
            raise RuntimeError("journal transaction belongs to another owner")
        self._lock_fd = None
        self._transaction_owner = None
        try:
            if os.name == "nt":
                # Byte-range locks are per-process but the documented
                # contract is an explicit unlock, and a live owner must
                # never be rejected on its own release path (age checks
                # are not part of release). Errors must not skip close.
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            try:
                os.close(fd)
            finally:
                self._thread_lock.release()

    # -- core I/O ---------------------------------------------------------

    def _read_lines(self, path: Path, *, lock: bool = True) -> list:
        """Validated rows from a JSONL store, in journal order.

        Every persisted row is shape-checked: a valid-JSON store of the
        wrong shape (null, a list, a number, a row missing required
        fields, a row of an unknown kind) is JournalCorrupt naming the
        store and line — error text names structure, never payload.
        Co-operating readers serialise on the same writer lock, so a
        locked read can never interleave with a locked append; a
        self-deadlock is avoided by reusing the transaction when this
        object already holds it in the current process/thread. Other
        cooperating readers acquire the same transaction lock.
        """
        if self._lock_fd is not None and self._transaction_owner == (os.getpid(), threading.get_ident()):
            lock = False                    # transaction held: no re-entry
        acquired = False
        if lock:
            self._acquire()
            acquired = True
        try:
            return self._read_validated_lines(path)
        finally:
            if acquired:
                self._release()

    def _read_validated_lines(self, path: Path) -> list:
        if not path.is_file():
            return []
        rows = []
        try:
            text = path.read_bytes().decode('utf-8')
        except UnicodeError as exc:
            raise JournalCorrupt(f"{path.name}: invalid UTF-8 encoding") from exc
        if not text:
            return []
        lines = text.split("\n")
        if lines[-1] != "":
            # A torn final line: a crash mid-append. Refuse on both read and
            # append — a partially written record is not a record.
            raise JournalCorrupt(
                f"{path.name}: torn final line (crash mid-write?) — "
                f"truncate it by hand after inspecting, the journal will "
                f"not guess")
        parsed = []
        for n, line in enumerate((l for l in lines[:-1]), 1):
            if not line.strip():
                raise JournalCorrupt(f"{path.name}: blank line at {n}")
            try:
                parsed.append((n, json.loads(line, object_pairs_hook=_json_object_no_duplicates)))
            except (ValueError, RecursionError) as exc:
                raise JournalCorrupt(
                    f"{path.name}: line {n} is not valid JSON or exceeds parser limits") from exc
        # Parse every line first, then shape-validate: a malformed JSON
        # line is reported even when an earlier line is the wrong shape.
        approvals_store = path.name == self.APPROVALS_FILENAME
        for n, row in parsed:
            if not isinstance(row, dict):
                raise JournalCorrupt(
                    f"{path.name}: line {n} must be a JSON object, got "
                    f"{type(row).__name__}")
            self._validate_row_shape(row, n, path.name, approvals_store)
            rows.append(row)
        _validate_unique_envelopes(rows, path.name)
        if approvals_store:
            _validate_approval_identity_uniqueness(rows, path.name)
        return rows

    def _validate_row_shape(self, row: dict, n: int, store: str,
                            approvals_store: bool = False) -> None:
        """Structural gate for one persisted row.

        This journal was written by append() against closed schemas; a
        store that no longer satisfies the stored shapes was corrupted
        after the fact. Legacy rows written before later append-time
        cross-subject rules remain readable: shape is validated, not
        reference coupling. Error names store, line, field — no payload.
        """
        try:
            _check_utf8_strings(row)
        except RecordError as exc:
            raise JournalCorrupt(f'{store}: line {n} has invalid UTF-8 strings') from exc
        if approvals_store:
            required = APPROVAL_REQUIRED_FIELDS
            optional: set = set()
            ts_field = "approved_at"
        else:
            kind = row.get("kind")
            if not isinstance(kind, str) or kind not in SCHEMAS:
                raise JournalCorrupt(f"{store}: line {n} has unknown "
                                     f"record kind")
            required = SCHEMAS[kind]["required"]
            optional = SCHEMAS[kind]["optional"]
            ts_field = "ts"
        # Every row the journal itself writes carries the storage
        # envelope: the lock-assigned commit sequence and its id. A row
        # without them cannot take part in sequence ordering.
        envelope = {"id", "seq"}
        missing = sorted((required | envelope) - set(row))
        if missing:
            raise JournalCorrupt(f"{store}: line {n} missing required "
                                 f"field(s): {', '.join(missing)}")
        unknown = sorted(set(row) - (required | optional | envelope |
                                     {ts_field}))
        if unknown:
            raise JournalCorrupt(f"{store}: line {n} has unknown field(s) "
                                 f"{', '.join(unknown)}")
        seq = row["seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            raise JournalCorrupt(f"{store}: line {n} seq must be an "
                                 f"integer")
        if not isinstance(row["id"], str) or not row["id"]:
            raise JournalCorrupt(f"{store}: line {n} id must be a "
                                 f"non-empty string")
        ts = row.get(ts_field)
        if ts_field in row:
            try:
                _norm_ts(ts)
            except RecordError:
                raise JournalCorrupt(f"{store}: line {n} {ts_field} is "
                                     f"not an ISO-8601 timestamp")
        if approvals_store:
            for f in ("subject", "approved_by"):
                if not isinstance(row[f], str) or not row[f].strip():
                    raise JournalCorrupt(f"{store}: line {n} field {f} must be a non-empty string")
            if not isinstance(row['stage'], str) or row['stage'] not in PROMOTION_STAGES:
                raise JournalCorrupt(f"{store}: line {n} has an unknown approval stage")
            return
        for f in RECORD_STRING_FIELDS:
            if f in row and (not isinstance(row[f], str) or
                             not row[f].strip()):
                raise JournalCorrupt(f"{store}: line {n} field {f} must "
                                     f"be a non-empty string")
        if 'basis' in row and (not isinstance(row['basis'], list) or not row['basis'] or
                               any(not isinstance(x, str) or not x for x in row['basis'])):
            raise JournalCorrupt(f"{store}: line {n} basis must be a non-empty list of record ids")
        if "evidence" in row:
            try:
                _check_evidence(row["evidence"], "row")
            except RecordError as exc:
                raise JournalCorrupt(f"{store}: line {n} evidence is "
                                     f"malformed") from exc
        if "metrics" in row:
            try:
                _check_metrics(row["metrics"], "row")
            except RecordError as exc:
                raise JournalCorrupt(f"{store}: line {n} metrics are "
                                     f"malformed") from exc

    def _publish(self, path: Path, rows: list) -> None:
        """One atomic rewrite of `path`, fsynced before the lock drops.

        A refused publication is diagnosable: Windows keeps a
        byte-range hold on a file another handle has open, which os.replace
        surfaces as PermissionError (winerror 5). That is a lock
        conflict with the exact path named — JournalLocked, the caller's
        refusal class — not a crash; prior bytes are untouched and no
        temp debris survives. POSIX either replaces or the failure is a
        real OSError, which propagates unchanged.
        """
        payload = "".join(json.dumps(r, ensure_ascii=False,
                                     separators=(",", ":")) + "\n"
                          for r in rows)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name,
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.replace(tmp, path)
            except PermissionError as error:
                # POSIX has no victim-hold on rename; this is the
                # Windows open-handle case. On Windows a PermissionError
                # carries winerror 5 ("Access is denied") for exactly
                # this replace-victim conflict.
                if getattr(error, "winerror", None) not in (5, 32):
                    raise
                raise JournalLocked(
                    f"{path.name} is held open by another handle; the "
                    f"append was refused and prior bytes are preserved "
                    f"(close the reader — including an editor or scanner "
                    f"holding {path} — and retry)") from error
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _write_all(self, path: Path, rows: list) -> None:
        """Publication seam: one atomic rewrite of ``path``.

        Kept as the single choke point every append publishes through —
        subclasses and tests hook exactly here (a paused writer holds the
        lock mid-transaction). See :meth:`_publish`.
        """
        self._publish(path, rows)

    # -- append / read ------------------------------------------------------

    def _next_seq(self, rows: list) -> int:
        """One past the highest lock-assigned sequence present.

        Rows may carry gaps (a hand-repaired journal after a crash), so
        the line count is not authoritative; the sequence must move
        forward past every recorded commit, never reuse or renumber a
        retired one. That is what makes backdating the ordering
        impossible: timestamps are display facts, seq is the order.
        """
        return max((r["seq"] for r in rows if isinstance(r, dict)), default=0) \
            + 1

    def append(self, rec: dict, ts: str | None = None) -> dict:
        clean = _check_record(rec)
        if ts is not None:
            _norm_ts(ts)                 # caller date: typed RecordError
        self._acquire()
        try:
            rows = self._read_validated_lines(self.data_path)
            seq = self._next_seq(rows)
            rec_id = f"{self.tenant}-{seq:06d}"
            clean["id"] = rec_id
            clean["seq"] = seq
            clean["ts"] = _norm_ts(ts or clean.get("ts") or _now_iso())
            self._validate_references(clean, rows)
            rows.append(clean)
            self._write_all(self.data_path, rows)
            return clean
        finally:
            self._release()

    def _validate_references(self, clean: dict, rows: list) -> None:
        kind = clean["kind"]
        by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
        if kind == "repair":
            target = by_id.get(clean["for_incident"])
            if target is None:
                raise RecordError(f"repair.for_incident "
                                  f"{clean['for_incident']!r} does not "
                                  f"exist in journal {self.tenant}")
            if target.get("kind") != "incident":
                raise RecordError(f"repair.for_incident must name an "
                                  f"incident, not a "
                                  f"{target.get('kind')}")
            if target.get("subject") != clean["subject"]:
                raise RecordError(
                    f"repair.for_incident {clean['for_incident']!r} belongs "
                    f"to subject {target.get('subject')!r}, not "
                    f"{clean['subject']!r}; a repair is about one subject "
                    f"and cannot reach across subjects")
        if kind == "incident" and "recurrence_of" in clean:
            target = by_id.get(clean["recurrence_of"])
            if target is None:
                raise RecordError(f"recurrence_of "
                                  f"{clean['recurrence_of']!r} does not "
                                  f"exist in journal {self.tenant}")
            if target.get("kind") != "incident":
                raise RecordError(f"recurrence_of must name an incident")
            if target.get("subject") != clean["subject"]:
                raise RecordError(
                    f"recurrence_of {clean['recurrence_of']!r} belongs to "
                    f"subject {target.get('subject')!r}, not "
                    f"{clean['subject']!r}; a recurrence is the same "
                    f"subject's incident returning")
        if kind == "lesson_proposal":
            if not clean["basis"]:
                raise RecordError("lesson_proposal.basis must name at least "
                                  "one existing record")
            for ref in clean["basis"]:
                if ref not in by_id:
                    raise RecordError(f"lesson_proposal.basis references "
                                      f"{ref!r} which does not exist in "
                                      f"journal {self.tenant}")
                if by_id[ref].get("subject") != clean["subject"]:
                    raise RecordError(
                        f"lesson_proposal.basis references {ref!r} which "
                        f"belongs to subject {by_id[ref].get('subject')!r}, "
                        f"not {clean['subject']!r}; a proposal's basis is "
                        f"its own subject's record")

    def read(self, kind: str | None = None) -> list:
        if kind is not None and kind not in SCHEMAS:
            raise RecordError(f"unknown kind {kind!r} (expected one of "
                              f"{', '.join(sorted(SCHEMAS))})")
        rows = self._read_lines(self.data_path)
        return [r for r in rows if kind is None or r.get("kind") == kind]

    # -- approvals ----------------------------------------------------------

    def append_approval(self, rec: dict) -> dict:
        if not isinstance(rec, dict):
            raise RecordError('approval must be a JSON object')
        _check_utf8_strings(rec)
        stage = rec.get("stage")
        if stage not in PROMOTION_STAGES:
            raise RecordError(f"stage must be one of "
                              f"{', '.join(PROMOTION_STAGES)}")
        missing = sorted(APPROVAL_REQUIRED_FIELDS - set(rec))
        unknown = sorted(set(rec) - APPROVAL_REQUIRED_FIELDS)
        if missing:
            raise RecordError(f"approval missing {', '.join(missing)}")
        if unknown:
            raise RecordError(f"unknown approval field(s) "
                              f"{', '.join(unknown)}")
        for f in ("subject", "approved_by"):
            if not isinstance(rec[f], str) or not rec[f].strip():
                raise RecordError(f"approval.{f} must be a non-empty string")
        _norm_ts(rec["approved_at"])
        self._acquire()
        try:
            rows = self._read_validated_lines(self.approvals_path)

            done = {r["stage"] for r in rows if r["subject"] == rec["subject"]}
            for previous in PROMOTION_STAGES[:PROMOTION_STAGES.index(stage)]:
                if previous not in done:
                    raise ApprovalRefused(f"{stage} requires human approval of '{previous}' first")
            taken = {(r["subject"], r["stage"]) for r in rows}
            if (rec["subject"], stage) in taken:
                raise ApprovalRefused(
                    f"{rec['subject']} / {stage} is already approved; "
                    f"approvals are append-only and never overwritten")
            if stage in EVIDENCE_STAGES:
                data_rows = self._read_validated_lines(self.data_path)
                status = assess(data_rows, rec["subject"])
                if status["verdict"] != "ELIGIBLE":
                    raise ApprovalRefused(f"{stage} requires readiness ELIGIBLE: {'; '.join(status['reasons'])}")
            clean = dict(rec)
            clean["seq"] = self._next_seq(rows)
            clean["id"] = f"{self.tenant}-apr-{clean['seq']:06d}"
            clean["approved_at"] = _norm_ts(clean["approved_at"])
            rows.append(clean)
            self._publish(self.approvals_path, rows)
            return clean
        finally:
            self._release()

    def read_approvals(self) -> list:
        return self._read_lines(self.approvals_path)

    def status_snapshot(self, subject: str) -> dict:
        """Readiness AND promotion status from one locked, atomic read.

        The CLI and other current-status consumers need the pair to agree;
        reading the stores separately lets an append land between them and
        pair new records with a stale verdict. This holds the writer lock
        across both reads — the same serialisation an append sees — so the
        answer describes one instant of the journal.
        """
        self._acquire()
        try:
            data_rows = self._read_validated_lines(self.data_path)
            approval_rows = self._read_validated_lines(self.approvals_path)
        finally:
            self._release()
        readiness = assess(data_rows, subject)
        promotion = promotion_status(data_rows, approval_rows, subject,
                                     readiness)
        return {"readiness": readiness, "promotion": promotion}


# ---------------------------------------------------------------------------

def record_approval(journal: Journal, subject: str, stage: str,
                    approved_by: str, when: str | None = None) -> dict:
    """Record one human approval, enforcing the documented gate sequence.

    sandbox requires nothing but a human; holdout/canary/soak require the
    readiness gate to be ELIGIBLE for the subject at approval time. The
    gate is deliberately ahead of the write, inside the same lock.
    """
    return journal.append_approval({
        "subject": subject, "stage": stage, "approved_by": approved_by,
        "approved_at": _now_iso() if when is None else when})


# -- readiness ---------------------------------------------------------------

def _assess_rows(rows: list) -> list:
    if not isinstance(rows, list):
        raise RecordError("assess expects the journal's rows as a list")
    for r in rows:
        if not isinstance(r, dict):
            raise JournalCorrupt(
                "assess received a row that is not a JSON object")
    return rows


def _resolve_chain_root(incident: dict, by_id: dict, store: str) -> dict:
    """Walk a recurrence chain to its root incident, same subject only.

    Missing, cyclic or non-incident legacy links fail closed with the
    store named rather than being ignored: an untraceable chain cannot
    be called settled by anything.
    """
    seen = set()
    current = incident
    while current.get("recurrence_of"):
        ref = current["recurrence_of"]
        if ref in seen:
            raise JournalCorrupt(
                f"{store}: incident chain is cyclic at {ref}")
        seen.add(ref)
        parent = by_id.get(ref)
        if parent is None:
            raise JournalCorrupt(
                f"{store}: recurrence_of {ref} does not resolve in the "
                f"journal")
        if parent.get("kind") != "incident":
            raise JournalCorrupt(
                f"{store}: recurrence_of {ref} names a "
                f"{parent.get('kind')}, not an incident")
        if parent.get("subject") != current.get("subject"):
            raise JournalCorrupt(
                f"{store}: recurrence_of {ref} crosses subjects")
        current = parent
    return current


def assess(rows: list, subject: str, min_sources: int = 2) -> dict:
    """Readiness for one subject: evidence math, nothing else.

    ELIGIBLE means: every incident chain of the subject is settled by a
    resolved repair FOR an incident IN that chain, at least
    ``min_sources`` DISTINCT verified evidence sources across the
    subject's incident(s) and resolved repair(s), actor-owned evidence
    excluded, and no unresolved recurrence after the latest repair.
    Proposals are counted, never acted on; quality/cost/latency metrics
    are surfaced as advisory metadata, never gating. ``min_sources``
    must be a positive integer (a bool is not an int here).
    """
    rows = _assess_rows(rows)
    if isinstance(min_sources, bool) or not isinstance(min_sources, int) or \
            min_sources < 1:
        raise RecordError("min_sources must be a positive integer")

    def _seq(r) -> int:
        value = r.get("seq")
        if type(value) is not int or value < 1:
            raise JournalCorrupt("readiness row has no valid commit sequence")
        return value

    incidents = [r for r in rows if r.get("kind") == "incident" and
                 r.get("subject") == subject]
    repairs = [r for r in rows if r.get("kind") == "repair" and
               r.get("subject") == subject]
    proposals = [r for r in rows if r.get("kind") == "lesson_proposal" and
                 r.get("subject") == subject]
    reasons: list = []

    if not incidents:
        reasons.append("no incident recorded for this subject")

    by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    resolved_repairs = [r for r in repairs if r.get("outcome") == "resolved"]

    # Settledness is per incident chain. Every incident of this subject
    # must be settled by a resolved repair whose for_incident is an
    # incident IN ITS OWN chain — never by a repair of another root (and
    # a fortiori never by another subject's activity). Legacy rows read
    # from disk may carry links that no longer resolve or cross
    # subjects: those chains fail closed instead of pretending to be
    # settled.
    unsettled: list = []
    roots: dict = {}
    for inc in incidents:
        try:
            roots[inc.get("id")] = _resolve_chain_root(inc, by_id, "journal")
        except JournalCorrupt as exc:
            reasons.append(f"incident {inc.get('id')} has an untraceable "
                           f"chain: {exc}")
            continue
    for inc in incidents:
        root = roots.get(inc.get("id"))
        if root is None:
            continue
        chain_ids = {r.get("id") for r in incidents
                     if roots.get(r.get("id")) is not None and
                     roots[r.get("id")]["id"] == root["id"]}
        settle = [r for r in resolved_repairs
                  if r.get("for_incident") in chain_ids and _seq(r) > _seq(inc)]
        if not settle:
            unsettled.append(inc.get("id"))

    if incidents and unsettled and len(unsettled) == len(incidents):
        reasons.append("no repair with outcome 'resolved' for this "
                       "subject's incident chain(s)")
    elif incidents and unsettled:
        reasons.append(f"incident(s) {', '.join(sorted(unsettled))} have no "
                       f"resolved repair in their own incident chain")

    actors = ({r.get("reported_by") for r in incidents} |
              {r.get("performed_by") for r in repairs}) - {None}
    sources: set = set()
    verified_refs = 0
    for rec in incidents + resolved_repairs:
        for e in rec.get("evidence") or []:
            if not e.get("verified"):
                continue
            verified_refs += 1
            if e.get("source") not in actors:
                sources.add(e["source"])
    if len(sources) < min_sources:
        reasons.append(
            f"insufficient independent verified evidence: "
            f"{len(sources)} source(s) {sorted(sources)} verified, "
            f"need {min_sources}; unverified or actor-owned references "
            f"do not count")

    recurrences = [r for r in incidents
                   if r.get("recurrence_of") and r.get("id") in unsettled]
    if recurrences:
        reasons.append(f"{len(recurrences)} unresolved recurrence(s) after the "
                       "latest committed repair of their own chain")

    metrics_present = {k: any(k in (r.get("metrics") or {})
                              for r in incidents + repairs)
                       for k in sorted(METRIC_KEYS)}
    return {
        "subject": subject,
        "verdict": "ELIGIBLE" if not reasons else "INELIGIBLE",
        "reasons": reasons,
        "counts": {
            "incidents": len(incidents),
            "repairs_resolved": len(resolved_repairs),
            "verified_evidence_refs": verified_refs,
            "verified_independent_sources": len(sources),
            "proposals": len(proposals),
        },
        "independent_sources": sorted(sources),
        "metrics_present": metrics_present,
        "assessed_at": _now_iso(),
    }


def promotion_status(rows: list, approvals: list, subject: str,
                     readiness: dict | None = None) -> dict:
    """Where a subject stands in the human promotion sequence.

    ``sequence_complete`` is history: all four stages carry a recorded
    human approval. It is deliberately distinct from ``complete``,
    which is current readiness AND a complete sequence — approvals are
    not a licence, and a subject whose evidence has regressed (a new
    unresolved recurrence, a fresh unrepaired incident) is reported
    not-ready now even though its sequence was completed.
    """
    _validate_approval_identity_uniqueness(approvals, "approvals")
    done = {a["stage"]: a for a in approvals if a["subject"] == subject}
    stages = [{"stage": s,
               "approved": s in done,
               "approved_by": done[s]["approved_by"] if s in done else None,
               "approved_at": done[s]["approved_at"] if s in done else None}
              for s in PROMOTION_STAGES]
    next_required = next((s for s in PROMOTION_STAGES if s not in done), None)
    sequence_complete = next_required is None
    if readiness is not None:
        verdict = readiness.get("verdict")
        reasons = list(readiness.get("reasons", []))
        complete = sequence_complete and verdict == "ELIGIBLE"
    else:
        # Sequence-only view (caller has no journal rows to assess):
        # ``complete`` can then only mean the sequence is complete.
        # Callers that hold the journal rows must pass readiness — the
        # CLI and status_snapshot always do — so a completed sequence
        # with regressed evidence is never reported as complete.
        verdict, reasons = None, []
        complete = sequence_complete
    return {
        "subject": subject,
        "stages": stages,
        "sequence_complete": sequence_complete,
        "readiness_verdict": verdict,
        "readiness_reasons": reasons,
        "complete": complete,
        "next_required": next_required,
        "note": "every stage is a human approval; nothing here promotes, "
                "replays, or fails anything over on its own. complete "
                "means the sequence is finished AND readiness is "
                "currently ELIGIBLE; sequence_complete alone is history.",
    }


def status_snapshot(journal: Journal, subject: str) -> dict:
    """One locked, atomic readiness+promotion answer — Journal.status_snapshot."""
    return journal.status_snapshot(subject)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="fleettools.journal",
        description="Tenant-scoped evidence journal with a human-gated "
                    "promotion sequence (append-only; nothing auto-applies).")
    ap.add_argument("--journal", required=True, help="journal root directory")
    ap.add_argument("--tenant", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_app = sub.add_parser("append", help="append a record from a JSON file")
    p_app.add_argument("--file", required=True)
    p_read = sub.add_parser("read", help="read records")
    p_read.add_argument("--kind", choices=sorted(SCHEMAS))
    p_read.add_argument("--json", action="store_true")
    p_ready = sub.add_parser("readiness", help="evidence readiness per subject")
    p_ready.add_argument("--subject", required=True)
    p_ready.add_argument("--json", action="store_true")
    p_appr = sub.add_parser("approve", help="record a human promotion approval")
    p_appr.add_argument("--subject", required=True)
    p_appr.add_argument("--stage", required=True, choices=list(PROMOTION_STAGES))
    p_appr.add_argument("--approved-by", required=True)
    p_appr.add_argument("--at", help="ISO timestamp (default: now)")
    p_prom = sub.add_parser("promotion", help="promotion gate status")
    p_prom.add_argument("--subject", required=True)
    p_prom.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        j = Journal(args.journal, args.tenant)
    except (ValueError, OSError) as exc:
        print(f"fleettools.journal: {exc}", file=sys.stderr)
        return 2

    try:
        if args.cmd == "append":
            try:
                rec = json.loads(Path(args.file).read_text(encoding="utf-8"),
                                 object_pairs_hook=_json_object_no_duplicates)
            except (OSError, UnicodeError) as exc:
                print(f"fleettools.journal: unreadable ({exc})", file=sys.stderr)
                return 2
            except json.JSONDecodeError as exc:
                print(f"fleettools.journal: not valid JSON (line "
                      f"{exc.lineno})", file=sys.stderr)
                return 2
            except RecordError as exc:
                print(f"fleettools.journal: {exc}", file=sys.stderr)
                return 2
            except (ValueError, RecursionError) as exc:
                print(f"fleettools.journal: JSON exceeds supported parser limits "
                      f"({type(exc).__name__})", file=sys.stderr)
                return 2
            clean = j.append(rec)
            print(f"appended {clean['id']}")
            return 0
        if args.cmd == "read":
            rows = j.read(kind=args.kind)
            print(json.dumps(rows, indent=2) if args.json else
                  "\n".join(f"{r['id']}  {r['kind']:16}  {r.get('subject', '')}"
                            for r in rows))
            return 0
        if args.cmd == "readiness":
            # One locked, atomic snapshot: readiness and the records it
            # describes come from the same instant of the journal.
            snap = j.status_snapshot(args.subject)
            result = snap["readiness"]
            print(json.dumps(result, indent=2) if args.json else
                  "\n".join([f"{result['verdict']}: {args.subject}"] +
                            [f"  - {r}" for r in result["reasons"]]))
            return 0 if result["verdict"] == "ELIGIBLE" else 1
        if args.cmd == "approve":
            try:
                rec = record_approval(j, args.subject, args.stage,
                                      args.approved_by, args.at)
            except ApprovalRefused as exc:
                print(f"fleettools.journal: approval refused — {exc}",
                      file=sys.stderr)
                return 1
            print(f"approved: {args.subject} / {rec['stage']} "
                  f"by {rec['approved_by']}")
            return 0
        # promotion — one locked, atomic snapshot: the promotion report
        # pairs the approval sequence with a readiness verdict from the
        # same instant; separate reads could pair new records with a
        # stale verdict.
        status = j.status_snapshot(args.subject)["promotion"]
        print(json.dumps(status, indent=2) if args.json else
              "\n".join(
                  [f"promotion for {args.subject}:"]
                  + [f"  {'x' if s['approved'] else ' '} "
                     f"{s['stage']:8} "
                     f"{('by ' + s['approved_by']) if s['approved_by'] else ''}"
                     for s in status["stages"]]
                  + [f"  readiness: {status['readiness_verdict']}"]
                  + [f"    - {r}" for r in status["readiness_reasons"]]
                  + [("  sequence complete — all four stages human-approved"
                      if status["sequence_complete"] else
                      f"  next required: {status['next_required']}")]
                  + [("  complete — sequence finished and readiness "
                      "currently ELIGIBLE" if status["complete"] else
                      "  NOT complete — see readiness above")]))
        # The exit code answers the current question, like readiness:
        # 0 = promotion-ready right now, 1 = sequence unfinished OR
        # readiness regressed. The report on stdout always names which.
        return 0 if status["complete"] else 1
    except RecordError as exc:
        print(f"fleettools.journal: {exc}", file=sys.stderr)
        return 2
    except (JournalLocked, JournalCorrupt) as exc:
        print(f"fleettools.journal: {exc}", file=sys.stderr)
        return 1
    except (JournalAliasRefused, OSError) as exc:
        print(f"fleettools.journal: cannot use journal {j.dir}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
