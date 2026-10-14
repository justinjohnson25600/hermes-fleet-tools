"""E410 / E431 boundary: filesystem tenant aliases + read-time approval uniqueness.

Each finding was first reproduced raw against the pre-fix source on the real
APFS volume (case-insensitive, normalization-insensitive), no filesystem
mocks:

* ``Journal(root, 'cafe\u0301').append(...)`` then ``Journal(root,
  'café').read()`` silently opened the SAME directory (``os.path.samefile``
  True): the old admission only ``lower()``-ed names, so an NFC/NFD
  normalization alias — distinct UTF-8 byte strings, identical on-disk
  entry — passed a name comparison that never touches the disk and both
  handles shared one store under two identities. The whole class is any
  precheck decided from names alone; the authoritative answer is the exact
  directory entry the volume actually creates after an exclusive
  ``mkdir``/``FileExistsError``.
* a stored duplicate ``(subject, stage)`` approval pair (distinct
  seq/id — e.g. hand-repair or an old-writer race) was accepted by every
  read; ``promotion_status``'s ``done`` map then let one row silently
  shadow the other (last-write-wins over an approval identity).
* CLI ``--tenant``/``--journal`` path OSErrors (long tenant vs the 255-byte
  name cap, root path is a file) surfaced as raw ``OSError``/``
  NotADirectoryError`` tracebacks with rc=1 instead of a contextual
  refusal with exit 2.

    python3 -m pytest tests/test_journal_fs_tenant_alias.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.journal import (                      # noqa: E402
    ApprovalRefused, Journal, JournalCorrupt, RecordError, main,
    promotion_status, record_approval,
)

try:                                                  # typed seam: pre-fix red
    from fleettools.journal import JournalAliasRefused  # noqa: E402
    have_alias_error = True
except ImportError:                                   # pre-fix source
    JournalAliasRefused = ValueError
    have_alias_error = False

NFC = "caf\u00e9"          # é precomposed
NFD = "cafe\u0301"         # e + combining acute
INCIDENT = {"kind": "incident", "subject": "s", "reported_by": "actor",
            "summary": "fixture"}
STAGES = ("sandbox", "holdout", "canary", "soak")


def approval(subject="s", stage="sandbox", by="reviewer",
             at="2026-01-01T00:00:00Z"):
    return {"subject": subject, "stage": stage, "approved_by": by,
            "approved_at": at}


def eligible_journal(root, tenant):
    """A journal whose subject 's' is readiness-ELIGIBLE: two independent
    verified evidence sources on a resolved repair of its own incident."""
    j = Journal(root, tenant)
    inc = j.append({"kind": "incident", "subject": "s",
                    "reported_by": "oncall-a", "summary": "x"})
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": inc["id"], "outcome": "resolved",
              "summary": "fixed",
              "evidence": [{"source": "monitor-a", "ref": "m#1",
                            "verified": True},
                           {"source": "audit-b", "ref": "a#1",
                            "verified": True}]})
    return j


def _oserror(exc: OSError) -> str:
    """The contextual CLI text for a refused journal path (exit 2)."""
    if isinstance(exc, NotADirectoryError):
        return (f"journal root {exc.filename!r} is not a directory")
    if exc.errno == 36:                               # ENAMETOOLONG
        return (f"tenant name too long: {exc.filename!r} exceeds the "
                f"filesystem name limit")
    return f"cannot open journal path {exc.filename!r}: {exc.strerror}"


# ===========================================================================
# Raw filesystem semantics of the actual volume (no mocks, no journal code)
# ===========================================================================

def test_volume_normalizes_and_folds_case(tmp_path):
    """Pin what the real volume does, so the tests above it mean something."""
    normalization_distinct = _creates_distinct_dirs(tmp_path, NFC, NFD)
    case_distinct = _creates_distinct_dirs(tmp_path, "tenant-a", "TENANT-A")
    os.mkdir(tmp_path / NFC)
    if normalization_distinct:
        os.mkdir(tmp_path / NFD)
        assert not os.path.samefile(tmp_path / NFC, tmp_path / NFD)
    else:
        with pytest.raises(FileExistsError):
            os.mkdir(tmp_path / NFD)
    os.mkdir(tmp_path / "tenant-a")
    if case_distinct:
        os.mkdir(tmp_path / "TENANT-A")
        assert not os.path.samefile(tmp_path / "tenant-a", tmp_path / "TENANT-A")
    else:
        with pytest.raises(FileExistsError):
            os.mkdir(tmp_path / "TENANT-A")
    assert (tmp_path / NFC).is_dir() and (tmp_path / "tenant-a").is_dir()


# ===========================================================================
# E410: normalization/case alias — one store, two identities (fail closed)
# ===========================================================================

def test_nfd_then_nfc_opening_is_refused_on_aliasing_volume(tmp_path):
    """E410, the finding as filed: second spelling must refuse, not read.

    The refusal may fire at the constructor's advisory precheck or at the
    acquire-time exclusive-mkdir entry check — the boundary is what is
    pinned, not which layer fires it.
    """
    first = Journal(tmp_path, NFD)
    stored = first.append(dict(INCIDENT))
    assert stored["seq"] == 1
    if not _creates_distinct_dirs(tmp_path, NFC, NFD):
        assert os.path.samefile(first.dir, tmp_path / NFC)
    with pytest.raises(ValueError):
        Journal(tmp_path, NFC).read()
    # Prior store untouched: refusal never mutates another tenant's data.
    assert first.read() == [stored]


def test_nfc_then_nfd_opening_is_refused_on_aliasing_volume(tmp_path):
    """Symmetric direction: the first spelling must equally refuse the second."""
    Journal(tmp_path, NFC).append(dict(INCIDENT))
    with pytest.raises(ValueError):
        Journal(tmp_path, NFD).read()


def test_mkdir_race_is_settled_by_the_exact_stored_entry(tmp_path):
    """Admission must inspect what the volume actually stored.

    The advisory precheck can only see pre-existing siblings; the moment
    between two constructors is invisible to it. The exclusive mkdir (or
    the FileExistsError on an aliasing volume) is the serialization point:
    the exact entry it stored/created is then compared component-wise with
    the requested name.
    """
    a = Journal(tmp_path, NFD, lock_timeout=0.1)
    b = Journal(tmp_path, NFC, lock_timeout=0.1)
    a.append(dict(INCIDENT))                           # a wins creation
    with pytest.raises(ValueError):                    # b must refuse, late
        b.append(dict(INCIDENT))                       # ...at mkdir admission
    if _creates_distinct_dirs(tmp_path, NFC, NFD):
        assert not b.dir.exists()  # Refusal cleans only its newly-created empty alias.
    else:
        assert os.path.samefile(a.dir, b.dir)
    assert len(a.read()) == 1


def test_normalization_alias_post_directory_creation_still_refuses(tmp_path):
    """Even when creation succeeded earlier, a later aliased open refuses.

    Covers the reversed-order window: this journal dir was created by an
    earlier writer with the other spelling. On this volume the constructor
    advisory precheck now folds + normalizes, so it refuses before the
    mkdir admission can; the acquire-time check remains the authority for
    the between-constructors race (see the mkdir-race test above).
    """
    existing = tmp_path / NFC
    existing.mkdir()                                   # someone else made it
    (existing / Journal.DATA_FILENAME).write_bytes(b"")
    with pytest.raises(JournalAliasRefused) as e:
        Journal(tmp_path, NFD)                         # NFD handle, NFC entry
    assert NFC in str(e.value) and NFD in str(e.value)


def test_exact_spelling_reopen_remains_valid(tmp_path):
    """The guard must never break the normal same-spelling reopen path."""
    first = Journal(tmp_path, NFD)
    stored = first.append(dict(INCIDENT))
    again = Journal(tmp_path, NFD)
    assert again.read() == [stored]
    assert again.append(dict(INCIDENT, summary="again"))["seq"] == 2


# ---------------------------------------------------------------------------
# Portability: on a case-SENSITIVE, normalization-SENSITIVE volume the same
# NFC/NFD pair is two genuinely distinct directories. Refusing it there is a
# deliberate conservative trade: opaque tenant names are never normalized
# into sharing, and a name that is ambiguous on the estate's other volumes
# is refused everywhere rather than silently meaning two different stores.
# These tests skip honestly where the host volume cannot represent the
# hazard they pin.
# ---------------------------------------------------------------------------

def _creates_distinct_dirs(root: Path, a: str, b: str) -> bool:
    probe = root / "alias-probe"
    shutil.rmtree(probe, ignore_errors=True)
    probe.mkdir()
    try:
        os.mkdir(probe / a)
        os.mkdir(probe / b)
        distinct = not os.path.samefile(probe / a, probe / b)
    except FileExistsError:
        distinct = False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return distinct


def test_case_sensitive_host_refuses_sibling_case_spelling(tmp_path):
    """On a volume where the spellings WOULD be distinct stores, the
    sibling case spelling is still refused (conservative, pre-existing
    policy): an opaque name that aliases on any estate volume never gets
    to mean two different stores elsewhere. The distinct directory is
    never created.
    """
    if not _creates_distinct_dirs(tmp_path, "tenant-a", "TENANT-A"):
        pytest.skip("host volume is case-insensitive; the distinct-case "
                    "sibling cannot exist here — covered by alias-refusal "
                    "tests")
    Journal(tmp_path, "tenant-a").append(dict(INCIDENT))
    with pytest.raises(JournalAliasRefused):
        Journal(tmp_path, "TENANT-A").read()
    assert not (tmp_path / "TENANT-A").exists()   # refused before creation


def test_normalization_sensitive_host_refuses_sibling_spelling(tmp_path):
    """Same conservative refusal, normalization form: on a
    normalization-sensitive volume (NTFS, Linux) NFD and NFC would be two
    distinct stores, and the second spelling is still refused with both
    spellings named — never silently shared, never silently split.
    """
    if not _creates_distinct_dirs(tmp_path, NFC, NFD):
        pytest.skip("host volume normalizes NFC/NFD to one entry; the "
                    "distinct-spelling sibling cannot exist here — covered "
                    "by alias-refusal tests")
    Journal(tmp_path, NFD).append(dict(INCIDENT))
    with pytest.raises(JournalAliasRefused) as e:
        Journal(tmp_path, NFC).read()
    assert NFC in str(e.value) and NFD in str(e.value)
    assert not (tmp_path / NFC).exists()          # refused before creation


def test_conservative_alias_refusal_explains_portability(tmp_path):
    """Refusal text names both spellings — auditable, never a silent merge."""
    Journal(tmp_path, "tenant-a").append(dict(INCIDENT))
    with pytest.raises(ValueError) as e:
        Journal(tmp_path, "TENANT-A").read()
    msg = str(e.value)
    assert "TENANT-A" in msg and "tenant-a" in msg


def test_conservative_refusal_on_normalization_sensitive_volume_is_ok(tmp_path):
    """Where NFC/NFD would be two distinct stores, refusing NFD is still
    correct: a name aliasing on any estate volume is refused everywhere,
    with the portability reason in the message (never silently shared).
    """
    Journal(tmp_path, NFC).append(dict(INCIDENT))
    with pytest.raises(ValueError) as e:
        Journal(tmp_path, NFD).read()
    msg = str(e.value)
    assert NFC in msg and NFD in msg
    assert "normalization" in msg.lower() or "case" in msg.lower()


# ===========================================================================
# Read-time approval uniqueness: (subject, stage) identifies one approval
# ===========================================================================

def test_stored_duplicate_approval_distinct_seq_id_refused_on_all_reads(tmp_path):
    """Two rows, same (subject, stage), distinct seq AND id — corruption.

    The append guard refuses duplicates at write time; a stored pair (hand
    merge, older writer, restore splice) must equally be refused at read
    time on every locked reader, or one approval silently shadows another.
    """
    j = Journal(tmp_path, "dup-appr")
    first = j.append_approval(approval())
    dup = dict(first, seq=99, id="dup-appr-apr-000099",
               approved_by="other-reviewer")
    with j.approvals_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dup, ensure_ascii=False,
                            separators=(",", ":")) + "\n")
    before = j.approvals_path.read_bytes()
    consumers = (
        j.read_approvals,
        lambda: j.status_snapshot("s"),
        lambda: j.append_approval(approval(stage="holdout")),
        lambda: record_approval(j, "s", "canary", "rev"))
    for consumer in consumers:
        with pytest.raises(JournalCorrupt):
            consumer()
    assert j.approvals_path.read_bytes() == before     # refusal preserves


def test_full_four_stage_sequence_remains_allowed(tmp_path):
    """The legit positive control: four DIFFERENT stages stay allowed, the
    sequence completes, promotion reports complete (readiness ELIGIBLE).
    """
    j = eligible_journal(tmp_path, "four-stage")
    for stage in STAGES:
        record_approval(j, "s", stage, "human")
    rows = j.read_approvals()
    assert [(r["subject"], r["stage"]) for r in rows] == \
        [("s", s) for s in STAGES]
    assert len(rows) == 4
    snap = j.status_snapshot("s")
    assert snap["promotion"]["sequence_complete"] is True
    assert snap["promotion"]["complete"] is True
    assert snap["readiness"]["verdict"] == "ELIGIBLE"


def test_promotion_status_refuses_duplicate_identity_rows_directly(tmp_path):
    """The pure consumer must fail closed too, not last-write-wins."""
    j = Journal(tmp_path, "pure-appr")
    base = j.append_approval(approval())
    rows = [base, dict(base, seq=99, id="pure-appr-apr-000099",
                       approved_by="shadow")]
    with pytest.raises(JournalCorrupt):
        promotion_status([], rows, "s")


def test_append_after_read_time_duplicate_cleanup_still_guarded(tmp_path):
    """A hand-cleaned store is treated as trusted history again."""
    j = Journal(tmp_path, "clean-appr")
    first = j.append_approval(approval())
    dup = dict(first, seq=99, id="clean-appr-apr-000099")
    with j.approvals_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dup) + "\n")
    with pytest.raises(JournalCorrupt):
        j.read_approvals()
    text = j.approvals_path.read_text(encoding="utf-8")
    keep = [line for line in text.split("\n") if line]
    assert len(keep) == 2                        # the duplicate is line 2
    j.approvals_path.write_text(keep[0] + "\n", encoding="utf-8")
    eligible_journal(j.root, "clean-appr")        # readiness in j's own store
    j.append_approval(approval(stage="holdout"))          # guard still live
    with pytest.raises(ApprovalRefused):
        j.append_approval(approval(stage="holdout"))


# ===========================================================================
# CLI OSError translation: contextual message + exit 2, never a traceback
# ===========================================================================

def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "fleettools.journal", *args],
        capture_output=True, text=True, timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]))


def test_cli_long_tenant_name_is_contextual_exit2(tmp_path):
    """Pre-fix: raw ``OSError: [Errno 63] File name too long`` + rc=1."""
    r = run_cli("--journal", str(tmp_path), "--tenant", "L" * 300, "read")
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr
    assert repr("L" * 300) in r.stderr  # Contextual path refusal; native errno wording varies.
    try:
        absent = not (tmp_path / ("L" * 300)).exists()
    except OSError:                     # stat on a too-long name IS the errno
        absent = True
    assert absent


def test_cli_root_is_a_file_is_contextual_exit2(tmp_path):
    """Pre-fix: raw ``NotADirectoryError: [Errno 20]`` + rc=1."""
    root_file = tmp_path / "not-a-dir"
    root_file.write_text("", encoding="utf-8")
    r = run_cli("--journal", str(root_file), "--tenant", "t", "read")
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr
    assert "not a directory" in r.stderr.lower()
    # ...while corruption/refusal keep their own classes and exits.
    assert run_cli("--journal", str(tmp_path), "--tenant", "a/b",
                   "read").returncode == 2


def test_journal_library_raises_typed_alias_error(tmp_path):
    """The typed error exists for programmatic callers; CLI maps it to 2."""
    Journal(tmp_path, NFD).append(dict(INCIDENT))
    with pytest.raises(JournalAliasRefused) as e:
        Journal(tmp_path, NFC).read()
    assert not isinstance(e.value, OSError)
    if have_alias_error:                    # typed subclass, not bare ValueError
        assert isinstance(e.value, ValueError)
    msg = str(e.value)
    assert NFC in msg and NFD in msg
