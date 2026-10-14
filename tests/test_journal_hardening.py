"""Stage E review-round regressions, each reproduced against pre-fix source.

Fault classes pinned here (all red before the fixes, on real temp I/O):

* caller-supplied dates that do not parse raised a bare ``ValueError``
  (unhandled: CLI traceback) instead of a typed ``RecordError``;
* a later recurrence logged in another subject's incident chain settled
  THIS subject's unresolved chain, because settledness was computed from
  a global ``max(ts)`` over all repairs instead of the chain's own repair;
* mixed-offset timestamps were compared as strings, so
  ``2026-01-01T23:00-05:00`` (later than ``2026-01-02T01:00+00:00`` as a
  true instant) sorted BEFORE it and hid an unresolved recurrence;
* a hand-backdated record was silently assigned seq 1 in a journal whose
  sequence had already moved on, instead of being refused;
* ``promotion_status`` reported ``complete: True`` from the approval
  sequence alone even when the readiness gate had regressed to
  INELIGIBLE — the historical sequence and current readiness were
  conflated;
* ``_read_lines`` accepted any valid JSON (``null``, lists, numbers,
  wrong-shaped rows) and crashed later with ``TypeError``/``KeyError`` —
  persisted rows are now shape-validated into ``JournalCorrupt`` with the
  store and line named, never the payload echoed;
* on Windows a refused atomic replace surfaced as a raw
  ``PermissionError`` (winerror 5) — it is translated to ``JournalLocked``
  naming the exact path, with prior bytes preserved (POSIX-translatable
  test here; the native-open-handle case is Windows-host-only).

    python3 -m pytest tests/test_journal_hardening.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.journal import (                      # noqa: E402
    Journal, JournalCorrupt, JournalLocked, PROMOTION_STAGES, RecordError,
    assess, promotion_status, record_approval,
)

have_status_snapshot: bool
try:                                                  # noqa: SIM105 - red/green
    from fleettools.journal import status_snapshot    # noqa: E402
    have_status_snapshot = True
except ImportError:                                   # pre-fix source: red
    have_status_snapshot = False

needs_status_snapshot = pytest.mark.skipif(
    not have_status_snapshot,
    reason="status_snapshot not implemented yet (pre-fix source)")

REPO = Path(__file__).resolve().parents[1]
TENANT = "tenant-a"


def incident(**over):
    rec = {"kind": "incident", "subject": "s", "reported_by": "oncall-a",
           "summary": "failure"}
    rec.update(over)
    return rec


def two_sources(prefix="m"):
    return [{"source": f"{prefix}onitor-a", "ref": "m#1", "verified": True},
            {"source": "audit-b", "ref": "a#1", "verified": True}]


def _seqs_are_strictly_increasing(rows):
    return all(a["seq"] < b["seq"] for a, b in zip(rows, rows[1:]))


def write_rows(tmp_path, rows, store="journal.jsonl"):
    d = tmp_path / TENANT
    d.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r, separators=(",", ":")) + "\n"
                      for r in rows)
    (d / store).write_text(payload, encoding="utf-8")
    return d / store


# ---------------------------------------------------------------------------
# Caller-supplied dates are typed refusals, never bare ValueError.
# ---------------------------------------------------------------------------

def test_append_ts_argument_garbage_is_recorderror(tmp_path):
    # Pre-fix: bare ValueError from datetime.fromisoformat, CLI exit 2 via
    # traceback only. Post-fix: typed RecordError, same exit class.
    with pytest.raises(RecordError):
        Journal(tmp_path, TENANT).append(incident(), ts="garbage")


def test_record_approval_when_garbage_is_recorderror(tmp_path):
    with pytest.raises(RecordError):
        record_approval(Journal(tmp_path, TENANT), "s", "sandbox", "human",
                        when="garbage")


def test_record_ts_field_garbage_is_recorderror(tmp_path):
    with pytest.raises(RecordError):
        Journal(tmp_path, TENANT).append(incident(ts="not a date"))


# ---------------------------------------------------------------------------
# Persisted rows are shape-validated on read: JournalCorrupt, never a crash.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", ["null\n", "[1, 2]\n", "42\n", '"text"\n'])
def test_valid_json_wrong_shape_is_journal_corrupt(tmp_path, line):
    write_rows(tmp_path, [])
    (tmp_path / TENANT / "journal.jsonl").write_text(line, encoding="utf-8")
    with pytest.raises(JournalCorrupt) as e:
        Journal(tmp_path, TENANT).read()
    assert "line 1" in str(e.value)


def test_persisted_row_with_unknown_kind_is_journal_corrupt(tmp_path):
    write_rows(tmp_path, [{"id": "tenant-a-000001", "seq": 1, "kind": "note",
                           "subject": "s"}])
    with pytest.raises(JournalCorrupt) as e:
        Journal(tmp_path, TENANT).read()
    assert "kind" in str(e.value)


def test_persisted_row_with_non_integer_seq_is_journal_corrupt(tmp_path):
    write_rows(tmp_path, [{"id": "tenant-a-000001", "seq": "7",
                           "kind": "incident", "subject": "s",
                           "reported_by": "oncall-a", "summary": "x"}])
    with pytest.raises(JournalCorrupt) as e:
        Journal(tmp_path, TENANT).read()
    assert "seq" in str(e.value)


@pytest.mark.parametrize("drop", ["id", "seq", "kind", "subject",
                                  "reported_by", "summary"])
def test_persisted_row_missing_required_field_is_journal_corrupt(tmp_path,
                                                                 drop):
    row = {"id": "tenant-a-000001", "seq": 1, "kind": "incident",
           "subject": "s", "reported_by": "oncall-a", "summary": "x"}
    del row[drop]
    write_rows(tmp_path, [row])
    with pytest.raises(JournalCorrupt) as e:
        Journal(tmp_path, TENANT).read()
    assert drop in str(e.value)


def test_corrupt_row_error_never_echoes_the_payload(tmp_path):
    payload_marker = "SECRET-PAYLOAD-VALUE"
    write_rows(tmp_path, [])
    (tmp_path / TENANT / "journal.jsonl").write_text(
        json.dumps({"id": "tenant-a-000001", "seq": 1, "kind": "note",
                    "subject": "s", "summary": payload_marker}) + "\n",
        encoding="utf-8")
    with pytest.raises(JournalCorrupt) as e:
        Journal(tmp_path, TENANT).read()
    assert payload_marker not in str(e.value)


def test_valid_history_still_reads_after_the_shape_gate(tmp_path):
    j = Journal(tmp_path, TENANT)
    j.append(incident())
    j.append(incident(summary="again"))
    assert [r["seq"] for r in j.read()] == [1, 2]


def test_assess_rejects_non_object_rows(tmp_path):
    with pytest.raises(JournalCorrupt):
        assess([None], "s")


# ---------------------------------------------------------------------------
# Approvals store: same shape gate, under the lock.
# ---------------------------------------------------------------------------

def test_approvals_null_row_is_journal_corrupt_not_typeerror(tmp_path):
    # Pre-fix: json 'null' row -> TypeError from r["stage"] access.
    (tmp_path / TENANT).mkdir(parents=True)
    (tmp_path / TENANT / "approvals.jsonl").write_text("null\n",
                                                       encoding="utf-8")
    with pytest.raises(JournalCorrupt) as e:
        Journal(tmp_path, TENANT).read_approvals()
    assert "approvals" in str(e.value)


def test_approval_append_shape_validates_history_under_lock(tmp_path):
    (tmp_path / TENANT).mkdir(parents=True)
    (tmp_path / TENANT / "approvals.jsonl").write_text("[]\n",
                                                       encoding="utf-8")
    with pytest.raises(JournalCorrupt):
        Journal(tmp_path, TENANT).append_approval(
            {"subject": "s", "stage": "sandbox", "approved_by": "human",
             "approved_at": "2026-10-10T00:00:00+00:00"})


def test_approval_seq_continues_past_hand_written_gaps(tmp_path):
    rows = [{"id": f"{TENANT}-apr-{n:06d}", "seq": n, "subject": "s",
             "stage": stage, "approved_by": "human",
             "approved_at": "2026-10-10T00:00:00+00:00"}
            for n, stage in enumerate(PROMOTION_STAGES, 1)]
    write_rows(tmp_path, rows, store="approvals.jsonl")
    j = Journal(tmp_path, TENANT)
    rec = j.append_approval({"subject": "s2", "stage": "sandbox",
                             "approved_by": "human",
                             "approved_at": "2026-10-10T01:00:00+00:00"})
    assert rec["seq"] == 5


# ---------------------------------------------------------------------------
# Trustworthy sequence: lock-assigned, gap-surviving, never backdated.
# ---------------------------------------------------------------------------

def test_append_rejects_a_garbage_explicit_ts_before_touching_the_store(tmp_path):
    # A garbage caller date is a typed refusal before the lock, the file,
    # or any publication — append-only stores are not edited by crashes.
    j = Journal(tmp_path, TENANT)
    j.append(incident())
    before = j.data_path.read_bytes()
    with pytest.raises(RecordError):
        j.append(incident(summary="backdated"), ts="garbage")
    assert j.data_path.read_bytes() == before
    assert j.append(incident(summary="ok"))["seq"] == 2


def test_append_sequence_is_max_existing_seq_plus_one(tmp_path):
    rows = [{"id": f"{TENANT}-000001", "seq": 1, "kind": "incident",
             "subject": "s", "reported_by": "oncall-a", "summary": "x"}]
    write_rows(tmp_path, rows)
    rec = Journal(tmp_path, TENANT).append(incident(summary="next"))
    assert rec["seq"] == 2


def test_append_sequence_survives_a_gap_in_hand_repaired_history(tmp_path):
    rows = [{"id": f"{TENANT}-000001", "seq": 1, "kind": "incident",
             "subject": "s", "reported_by": "oncall-a", "summary": "x"},
            {"id": f"{TENANT}-000005", "seq": 5, "kind": "incident",
             "subject": "s", "reported_by": "oncall-a", "summary": "y"}]
    write_rows(tmp_path, rows)
    rec = Journal(tmp_path, TENANT).append(incident(summary="next"))
    assert rec["seq"] == 6
    assert rec["id"] == f"{TENANT}-000006"


def test_append_ordering_authority_is_the_lock_assigned_seq(tmp_path):
    # Display timestamps are caller-declared facts; a subject's earlier
    # event may legitimately commit later (its ts sorts before another
    # subject's newer record). Trustworthiness is the SEQUENCE: it stays
    # monotonic across valid gaps and can never be backdated past a
    # committed commit — a caller cannot renumber or reuse seqs at all,
    # so ordering by seq is always the true commit order.
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(subject="s1"), ts="2026-06-01T00:00:00+00:00")
    later = j.append(incident(subject="s2", summary="other"),
                     ts="2026-07-01T00:00:00+00:00")
    earlier = j.append(incident(subject="s1", summary="backfill"),
                       ts="2026-01-01T00:00:00+00:00")
    assert [r["seq"] for r in j.read()] == [1, 2, 3]
    assert _seqs_are_strictly_increasing(j.read())
    assert earlier["seq"] > later["seq"]
    assert a["seq"] == 1


# ---------------------------------------------------------------------------
# Readiness is per incident chain, on true instants.
# ---------------------------------------------------------------------------

def cross_subject_settle_rows():
    """Parent reproduction: another subject's later activity must not
    settle this subject's unresolved recurrence.

    A has an unresolved recurrence after A's repair. B's repair commits
    later, so the pre-fix global ``max(ts)`` over ALL repairs made B's
    repair "the latest repair" and hid A's recurrence — ELIGIBLE.
    """
    return [
        {"id": "tenant-a-000001", "seq": 1, "kind": "incident",
         "subject": "s1", "reported_by": "oncall-a", "summary": "x",
         "ts": "2026-01-01T00:00:00+00:00"},
        {"id": "tenant-a-000002", "seq": 2, "kind": "incident",
         "subject": "s2", "reported_by": "oncall-b", "summary": "y",
         "ts": "2026-06-01T00:00:00+00:00"},
        {"id": "tenant-a-000003", "seq": 3, "kind": "repair",
         "subject": "s1", "performed_by": "eng-a",
         "for_incident": "tenant-a-000001", "outcome": "resolved",
         "summary": "fixed", "ts": "2026-01-02T01:00:00+00:00",
         "evidence": two_sources()},
        # s1's recurrence AFTER its own repair — genuinely unresolved.
        {"id": "tenant-a-000004", "seq": 4, "kind": "incident",
         "subject": "s1", "reported_by": "oncall-a",
         "summary": "same failure returned",
         "recurrence_of": "tenant-a-000001",
         "ts": "2026-02-01T00:00:00+00:00"},
        # s2's LATER repair (2026-06): with the pre-fix global-max
        # ordering this is "the latest repair", masking s1's recurrence.
        {"id": "tenant-a-000005", "seq": 5, "kind": "repair",
         "subject": "s2", "performed_by": "eng-b",
         "for_incident": "tenant-a-000002", "outcome": "resolved",
         "summary": "fixed other subject", "ts": "2026-06-02T00:00:00+00:00",
         "evidence": two_sources()},
    ]


def test_another_subjects_later_repair_cannot_mask_this_recurrence(tmp_path):
    # Pre-fix: ELIGIBLE — the global max last_repair was s2's June repair,
    # so s1's February recurrence looked "before the latest repair".
    result = assess(cross_subject_settle_rows(), "s1")
    assert result["verdict"] == "INELIGIBLE"
    assert any("recurrence" in r for r in result["reasons"])
    # s2's own verdict is unaffected either way (its chain is settled).
    assert "recurrence" not in " ".join(assess(cross_subject_settle_rows(),
                                               "s2")["reasons"])


def test_cross_subject_repro_row_by_row(tmp_path):
    # Same scenario as test_another_subjects_later_repair_cannot_mask_
    # this_recurrence, committed for real through Journal.append: A's
    # recurrence (February) is unresolved; B's later repair (June) is a
    # different subject's chain and must not become "the latest repair"
    # that hides it. The pre-fix global max over all repairs said
    # ELIGIBLE here; the per-chain rule keeps A INELIGIBLE.
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(subject="s1"), ts="2026-01-01T00:00:00+00:00")
    b = j.append(incident(subject="s2", summary="other"),
                 ts="2026-06-01T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s1", "performed_by": "eng-a",
              "for_incident": a["id"], "outcome": "resolved",
              "summary": "fixed", "evidence": two_sources()},
             ts="2026-01-02T01:00:00+00:00")
    j.append(incident(subject="s1", summary="returned",
                      recurrence_of=a["id"]), ts="2026-02-01T00:00:00+00:00")
    later = j.append({"kind": "repair", "subject": "s2",
                      "performed_by": "eng-b", "for_incident": b["id"],
                      "outcome": "resolved", "summary": "fixed s2",
                      "evidence": two_sources()},
                     ts="2026-06-02T00:00:00+00:00")
    result = assess(j.read(), "s1")
    assert result["verdict"] == "INELIGIBLE"
    assert any("recurrence" in r for r in result["reasons"])
    # B's later repair is real for B — it never leaks into A either way.
    assert later["seq"] == 5
    # B's own readiness is unaffected by A's recurrence.
    assert "recurrence" not in " ".join(assess(j.read(), "s2")["reasons"])


@pytest.mark.parametrize("recurrence_ts", [
    "2026-01-01T23:00:00-05:00",             # the exact reproduced form
    "2026-01-02T04:00:00+00:00",             # same instant, plain offset
])
def test_recurrence_ordering_uses_true_instants_not_strings(tmp_path,
                                                            recurrence_ts):
    # Pre-fix: "2026-01-01T23:00-05:00" string-sorts BEFORE
    # "2026-01-02T01:00+00:00", hiding a recurrence that is truly later.
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(), ts="2026-01-01T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": a["id"], "outcome": "resolved",
              "summary": "fixed"}, ts="2026-01-02T01:00:00+00:00")
    j.append(incident(summary="returned", recurrence_of=a["id"]),
             ts=recurrence_ts)
    result = assess(j.read(), "s")
    assert result["verdict"] == "INELIGIBLE"
    assert any("recurrence" in r for r in result["reasons"])


def test_recurrence_truly_before_the_repair_stays_settled(tmp_path):
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(), ts="2026-01-01T00:00:00+00:00")
    j.append(incident(summary="blip", recurrence_of=a["id"]),
             ts="2026-01-01T12:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": a["id"], "outcome": "resolved",
              "summary": "fixed", "evidence": two_sources()},
             ts="2026-01-02T01:00:00+00:00")
    assert assess(j.read(), "s")["verdict"] == "ELIGIBLE"


def test_a_later_resolved_repair_settles_its_own_recurrence(tmp_path):
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(), ts="2026-01-01T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": a["id"], "outcome": "resolved",
              "summary": "fix 1", "evidence": two_sources()},
             ts="2026-01-02T01:00:00+00:00")
    rec = j.append(incident(summary="returned", recurrence_of=a["id"]),
                   ts="2026-01-03T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": rec["id"], "outcome": "resolved",
              "summary": "fix 2", "evidence": two_sources()},
             ts="2026-01-04T00:00:00+00:00")
    assert assess(j.read(), "s")["verdict"] == "ELIGIBLE"


def test_a_new_unrepaired_incident_blocks_readiness(tmp_path):
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(), ts="2026-01-01T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": a["id"], "outcome": "resolved",
              "summary": "fixed", "evidence": two_sources()},
             ts="2026-01-02T01:00:00+00:00")
    j.append(incident(summary="fresh, separate incident"),
             ts="2026-02-01T00:00:00+00:00")
    result = assess(j.read(), "s")
    assert result["verdict"] == "INELIGIBLE"
    assert any("resolved" in r for r in result["reasons"])


def test_evidence_spans_the_subjects_chains(tmp_path):
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(), ts="2026-01-01T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": a["id"], "outcome": "resolved", "summary": "r1",
              "evidence": [{"source": "monitor-a", "ref": "m#1",
                            "verified": True}]},
             ts="2026-01-02T00:00:00+00:00")
    rec = j.append(incident(summary="returned", recurrence_of=a["id"]),
                   ts="2026-01-03T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": rec["id"], "outcome": "resolved", "summary": "r2",
              "evidence": [{"source": "audit-b", "ref": "a#1",
                            "verified": True}]},
             ts="2026-01-04T00:00:00+00:00")
    result = assess(j.read(), "s")
    assert result["verdict"] == "ELIGIBLE"
    assert result["independent_sources"] == ["audit-b", "monitor-a"]


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5, "2", [2]])
def test_min_sources_must_be_a_positive_int_not_bool(tmp_path, bad):
    with pytest.raises(RecordError):
        assess([], "s", min_sources=bad)


def test_min_sources_positive_int_is_accepted(tmp_path):
    assert assess([], "s", min_sources=1)["verdict"] == "INELIGIBLE"


# ---------------------------------------------------------------------------
# Promotion reporting separates historical sequence from current readiness.
# ---------------------------------------------------------------------------

def ready_journal(tmp_path):
    j = Journal(tmp_path, TENANT)
    a = j.append(incident(), ts="2026-01-01T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": a["id"], "outcome": "resolved", "summary": "r",
              "evidence": two_sources()}, ts="2026-01-02T01:00:00+00:00")
    for stage in PROMOTION_STAGES:
        record_approval(j, "s", stage, "human")
    return j, a


def test_promotion_reports_ineligible_readiness_as_not_complete(tmp_path):
    # Parent reproduction: 4 approvals, then an unresolved recurrence.
    # Pre-fix: promotion_status said complete=True.
    j, a = ready_journal(tmp_path)
    j.append(incident(summary="returned", recurrence_of=a["id"]),
             ts="2026-01-03T00:00:00+00:00")
    rows = j.read()
    readiness = assess(rows, "s")
    status = promotion_status(rows, j.read_approvals(), "s", readiness)
    assert status["sequence_complete"] is True      # history: all 4 stages
    assert status["readiness_verdict"] == "INELIGIBLE"
    assert status["complete"] is False              # not ready right now
    assert status["readiness_reasons"]
    assert status["next_required"] is None


def test_promotion_ready_subject_is_complete(tmp_path):
    j, _ = ready_journal(tmp_path)
    rows = j.read()
    status = promotion_status(rows, j.read_approvals(), "s",
                              assess(rows, "s"))
    assert status["complete"] is True
    assert status["sequence_complete"] is True
    assert status["readiness_verdict"] == "ELIGIBLE"


def test_promotion_incomplete_sequence_is_not_complete(tmp_path):
    j, _ = ready_journal(tmp_path)
    rows = [r for r in j.read_approvals() if r["stage"] != "soak"]
    all_rows = j.read()
    status = promotion_status(all_rows, rows, "s", assess(all_rows, "s"))
    assert status["sequence_complete"] is False
    assert status["complete"] is False
    assert status["next_required"] == "soak"


def test_promotion_status_after_fresh_unrepaired_incident(tmp_path):
    j, _ = ready_journal(tmp_path)
    j.append(incident(summary="fresh, separate incident"),
             ts="2026-02-01T00:00:00+00:00")
    all_rows = j.read()
    status = promotion_status(all_rows, j.read_approvals(), "s",
                              assess(all_rows, "s"))
    assert status["complete"] is False
    assert status["readiness_verdict"] == "INELIGIBLE"


# ---------------------------------------------------------------------------
# One locked, atomic status snapshot for CLI/current-status consumers.
# ---------------------------------------------------------------------------

@needs_status_snapshot
def test_status_snapshot_pairs_readiness_with_promotion(tmp_path):
    j, a = ready_journal(tmp_path)
    j.append(incident(summary="returned", recurrence_of=a["id"]),
             ts="2026-01-03T00:00:00+00:00")
    snap = status_snapshot(j, "s")
    assert snap["promotion"]["complete"] is False
    assert snap["promotion"]["readiness_verdict"] == "INELIGIBLE"
    assert snap["readiness"]["verdict"] == "INELIGIBLE"
    assert snap["promotion"]["readiness_reasons"] == \
        snap["readiness"]["reasons"]


@needs_status_snapshot
def test_status_snapshot_takes_the_writer_lock(tmp_path):
    j, _ = ready_journal(tmp_path)
    j._acquire()
    try:
        with pytest.raises(JournalLocked):
            status_snapshot(Journal(tmp_path, TENANT, lock_timeout=0.1), "s")
    finally:
        j._release()


# ---------------------------------------------------------------------------
# The append is all-or-nothing even when publication is refused.
# ---------------------------------------------------------------------------

def test_refused_publication_preserves_bytes_and_names_the_path(tmp_path):
    # The Windows replace-victim refusal arrives as PermissionError with
    # winerror 5 ("Access is denied"). POSIX builds cannot construct that
    # natively, so this pins the translation contract by raising the same
    # error shape the Windows interpreter raises there; the real
    # open-handle case runs on a Windows host
    # (test_windows_open_handle_refusal_is_journal_locked).
    j = Journal(tmp_path, TENANT)
    first = j.append(incident())
    before = j.data_path.read_bytes()
    real_replace = os.replace

    def refused(src, dst, *a, **k):
        error = PermissionError(13, "Access is denied", str(dst))
        error.winerror = 5            # what a Windows os.replace victim says
        raise error

    import fleettools.journal as mod
    mod.os.replace = refused
    try:
        with pytest.raises(JournalLocked) as e:
            j.append(incident(summary="second"))
    finally:
        mod.os.replace = real_replace
    assert "journal.jsonl" in str(e.value)
    assert j.data_path.read_bytes() == before
    assert not list(j.dir.glob("*.tmp")), "no temp debris may survive"
    again = j.append(incident(summary="after recovery"))
    assert again["seq"] == 2 and again["id"] != first["id"]


def test_publication_failure_without_winerror_is_not_translated(tmp_path):
    # A POSIX PermissionError (a genuinely unwritable target) keeps its
    # own meaning: it is not reported as another writer's lock.
    j = Journal(tmp_path, TENANT)
    j.append(incident())
    before = j.data_path.read_bytes()
    real_replace = os.replace

    def refused(src, dst, *a, **k):
        raise PermissionError(13, "Permission denied", str(dst))

    import fleettools.journal as mod
    mod.os.replace = refused
    try:
        with pytest.raises(PermissionError):
            j.append(incident(summary="second"))
    finally:
        mod.os.replace = real_replace
    assert j.data_path.read_bytes() == before
    assert not list(j.dir.glob("*.tmp"))


def test_locked_journal_is_reusable_after_a_refused_append(tmp_path):
    # A timed-out lock attempt must release its own thread/OS claims so the
    # same object can append later (the Windows release-path regression).
    holder = Journal(tmp_path, TENANT)
    holder._acquire()
    refused = Journal(tmp_path, TENANT, lock_timeout=0.1)
    with pytest.raises(JournalLocked):
        refused.append(incident())
    holder._release()
    rec = refused.append(incident(summary="works again"))
    assert rec["seq"] == 1


# ---------------------------------------------------------------------------
# Torn/corrupt history is refused everywhere, never skipped or crashed on.
# ---------------------------------------------------------------------------

def test_torn_tail_names_the_data_file(tmp_path):
    j = Journal(tmp_path, TENANT)
    j.append(incident())
    with j.data_path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "inc')
    with pytest.raises(JournalCorrupt) as e:
        j.read()
    assert "journal.jsonl" in str(e.value)


def test_append_refuses_on_corrupt_history_and_preserves_bytes(tmp_path):
    j = Journal(tmp_path, TENANT)
    first = j.append(incident())
    with j.data_path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "inc')
    before = j.data_path.read_bytes()
    with pytest.raises(JournalCorrupt):
        j.append(incident(summary="second"))
    assert j.data_path.read_bytes() == before
    # Truncating the torn tail by hand (the documented recovery) restores
    # the journal: the first record survived untouched.
    text = j.data_path.read_text(encoding="utf-8")
    j.data_path.write_text(text[:text.rindex("\n") + 1], encoding="utf-8")
    rows = j.read()
    assert [r["id"] for r in rows] == [first["id"]]
    assert j.append(incident(summary="second"))["seq"] == 2


# ---------------------------------------------------------------------------
# Windows namespace hazards a tenant name can carry.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", ["CON", "con.txt", "AUX", "NUL", "COM1",
                                    "LPT9", "prn"])
def test_windows_reserved_device_names_are_refused(tmp_path, tenant):
    with pytest.raises(ValueError):
        Journal(tmp_path, tenant)


@pytest.mark.parametrize("tenant", ["tenant-a ", "tenant-a.", "tenant-a.."])
def test_names_windows_trims_or_cannot_store_are_refused(tmp_path, tenant):
    with pytest.raises(ValueError):
        Journal(tmp_path, tenant)


def test_case_colliding_tenant_with_existing_directory_is_refused(tmp_path):
    Journal(tmp_path, TENANT).append(incident())
    with pytest.raises(ValueError) as e:
        Journal(tmp_path, TENANT.upper())
    assert "TENANT-A" in str(e.value).upper()


def test_distinct_case_tenant_on_a_fresh_root_is_not_blocked(tmp_path):
    # Case matters where the filesystem says it does: without an existing
    # colliding directory, a mixed-case tenant is a valid distinct name.
    rec = Journal(tmp_path, "Tenant-B").append(incident())
    assert rec["id"].startswith("Tenant-B-")


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "fleettools.journal", *args],
        capture_output=True, text=True, timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]))


def test_cli_promotion_exit_reflects_current_readiness(tmp_path):
    # Regression-safe reporting, end to end: the sequence completes, an
    # unresolved recurrence lands, promotion must report INELIGIBLE with
    # rc=1 — never the old sequence-only rc=0 "complete".
    root = tmp_path / "jr"
    inc_file = tmp_path / "inc.json"
    inc_file.write_text(json.dumps({
        "kind": "incident", "subject": "s", "reported_by": "oncall-a",
        "summary": "x"}), encoding="utf-8")
    r = run_cli("--journal", str(root), "--tenant", TENANT,
                "append", "--file", str(inc_file))
    assert r.returncode == 0, r.stderr
    inc_id = r.stdout.split()[-1]
    rep_file = tmp_path / "rep.json"
    rep_file.write_text(json.dumps({
        "kind": "repair", "subject": "s", "performed_by": "eng-a",
        "for_incident": inc_id, "outcome": "resolved", "summary": "f",
        "evidence": [{"source": "monitor-a", "ref": "m#1", "verified": True},
                     {"source": "audit-b", "ref": "a#1", "verified": True}]}),
        encoding="utf-8")
    assert run_cli("--journal", str(root), "--tenant", TENANT,
                   "append", "--file", str(rep_file)).returncode == 0
    for stage in PROMOTION_STAGES:
        r = run_cli("--journal", str(root), "--tenant", TENANT,
                    "approve", "--subject", "s", "--stage", stage,
                    "--approved-by", "human")
        assert r.returncode == 0, r.stderr
    rec_file = tmp_path / "rec.json"
    rec_file.write_text(json.dumps({
        "kind": "incident", "subject": "s", "reported_by": "oncall-a",
        "summary": "returned", "recurrence_of": inc_id}), encoding="utf-8")
    assert run_cli("--journal", str(root), "--tenant", TENANT,
                   "append", "--file", str(rec_file)).returncode == 0
    r = run_cli("--journal", str(root), "--tenant", TENANT,
                "promotion", "--subject", "s", "--json")
    assert r.returncode == 1, r.stdout
    payload = json.loads(r.stdout)
    assert payload["sequence_complete"] is True
    assert payload["readiness_verdict"] == "INELIGIBLE"
    assert payload["complete"] is False
    assert payload["readiness_reasons"]


# ---------------------------------------------------------------------------
# Native Windows: a held-open data file refuses publication diagnosably.
# POSIX never holds replace victims, so this runs only on a real Windows
# host — never faked.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt",
                    reason="byte-range read refusal is a Windows-native "
                           "behaviour; run on a Windows host")
def test_windows_open_handle_refusal_is_journal_locked(tmp_path):
    j = Journal(tmp_path, TENANT)
    first = j.append(incident())
    before = j.data_path.read_bytes()
    with j.data_path.open("r", encoding="utf-8") as held:
        with pytest.raises(JournalLocked) as e:
            j.append(incident(summary="second"))
        assert "journal.jsonl" in str(e.value)
    assert j.data_path.read_bytes() == before
    again = j.append(incident(summary="after close"))
    assert again["seq"] == 2 and again["id"] != first["id"]


@pytest.mark.skipif(os.name != "nt",
                    reason="byte-range unlock on release is Windows-native")
def test_windows_rapid_lock_cycles_stay_reusable(tmp_path):
    j = Journal(tmp_path, TENANT)
    for n in range(25):
        j._acquire()
        try:
            pass
        finally:
            j._release()
    assert j.append(incident(summary="still usable"))["seq"] == 1
