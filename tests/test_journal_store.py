"""Tests for the Stage E evidence journal store.

Append/read with durable concurrent-write protection, closed record schemas,
and real temp files everywhere — no mocking of the filesystem.

    python3 -m pytest tests/test_journal_store.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.journal import (           # noqa: E402
    Journal, JournalCorrupt, JournalLocked, RecordError,
)

REPO = Path(__file__).resolve().parents[1]


def incident(**over):
    rec = {"kind": "incident", "subject": "backup-rotation",
           "reported_by": "oncall-a", "summary": "nightly backup skipped"}
    rec.update(over)
    return rec


# ---------------------------------------------------------------------------
# Append / read roundtrip.
# ---------------------------------------------------------------------------

def test_append_read_roundtrip_assigns_monotonic_seq_and_stable_ids(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    first = j.append(incident())
    second = j.append(incident(summary="again"))
    assert first["seq"] == 1 and second["seq"] == 2
    assert first["id"] == "tenant-a-000001"
    assert second["id"] == "tenant-a-000002"
    rows = j.read()
    assert [r["id"] for r in rows] == [first["id"], second["id"]]
    assert rows[0]["summary"] == "nightly backup skipped"


def test_read_kind_filter(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    j.append(incident())
    j.append({"kind": "lesson_proposal", "subject": "backup-rotation",
              "proposed_by": "reviewer-a", "proposal": "add a retry",
              "basis": ["tenant-a-000001"]})
    assert len(j.read()) == 2
    assert [r["kind"] for r in j.read(kind="incident")] == ["incident"]
    with pytest.raises(RecordError):
        j.read(kind="nonsense")


def test_ts_is_normalized_and_stable(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    rec = j.append(incident(ts="2026-01-01T00:00:00Z"))
    assert rec["ts"] == "2026-01-01T00:00:00+00:00"
    assert j.read()[0]["ts"] == "2026-01-01T00:00:00+00:00"


def test_explicit_ts_argument_wins(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    rec = j.append(incident(), ts="2026-02-02T03:04:05+00:00")
    assert rec["ts"] == "2026-02-02T03:04:05+00:00"


# ---------------------------------------------------------------------------
# Concurrent writers: the durable guarantee, exercised for real.
# ---------------------------------------------------------------------------

def test_append_is_durable_under_concurrent_writers(tmp_path):
    writers = ["writer-a", "writer-b", "writer-c", "writer-d"]
    procs = []
    for w in writers:
        script = tmp_path / f"writer-{w}.py"
        script.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "from fleettools.journal import Journal\n"
            f"j = Journal({str(tmp_path)!r}, 'tenant-a')\n"
            "for _ in range(5):\n"
            f"    j.append({{'kind': 'incident', 'subject': 's',"
            f" 'reported_by': {w!r}, 'summary': 'x'}})\n",
            encoding="utf-8")
        procs.append(subprocess.Popen([sys.executable, str(script)],
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True))
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err

    rows = Journal(tmp_path, "tenant-a").read()
    assert len(rows) == 20
    seqs = sorted(r["seq"] for r in rows)
    assert seqs == list(range(1, 21))
    assert len({r["id"] for r in rows}) == 20
    from collections import Counter
    counts = Counter(r["reported_by"] for r in rows)
    assert all(counts[w] == 5 for w in writers)


def test_append_refuses_while_a_live_lock_is_held(tmp_path):
    holder = Journal(tmp_path, "tenant-a")
    holder._acquire()
    try:
        with pytest.raises(JournalLocked):
            Journal(tmp_path, "tenant-a", lock_timeout=0.1).append(incident())
    finally:
        holder._release()


def test_unowned_lock_file_does_not_wedge_journal(tmp_path):
    j = Journal(tmp_path, "tenant-a", lock_timeout=2.0)
    lock = tmp_path / "tenant-a" / Journal.LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("crashed writer", encoding="utf-8")
    old = time.time() - 60
    import os
    os.utime(lock, (old, old))
    rec = j.append(incident())
    assert rec["seq"] == 1


# ---------------------------------------------------------------------------
# Integrity: a torn or malformed tail is refused, never skipped.
# ---------------------------------------------------------------------------

def _write_journal(tmp_path, lines):
    d = tmp_path / "tenant-a"
    d.mkdir(parents=True, exist_ok=True)
    (d / "journal.jsonl").write_text("".join(lines), encoding="utf-8")


def test_torn_tail_refuses_read_and_append(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    j.append(incident())
    path = tmp_path / "tenant-a" / "journal.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "inc')          # crash mid-write, no newline
    with pytest.raises(JournalCorrupt):
        j.read()
    with pytest.raises(JournalCorrupt):
        j.append(incident(summary="second"))


def test_malformed_line_names_the_line_number(tmp_path):
    _write_journal(tmp_path, ['{"kind": "incident"}\n', "not json\n"])
    with pytest.raises(JournalCorrupt) as e:
        Journal(tmp_path, "tenant-a").read()
    assert "line 2" in str(e.value)


# ---------------------------------------------------------------------------
# Closed record schemas — privacy by construction: arbitrary payload cannot
# ride along, and references must resolve.
# ---------------------------------------------------------------------------

def test_unknown_field_is_rejected(tmp_path):
    with pytest.raises(RecordError) as e:
        Journal(tmp_path, "tenant-a").append(incident(console_output="..."))
    assert "console_output" in str(e.value)


def test_unknown_kind_is_rejected(tmp_path):
    with pytest.raises(RecordError):
        Journal(tmp_path, "tenant-a").append({"kind": "note", "subject": "s"})


def test_missing_required_field_is_rejected(tmp_path):
    with pytest.raises(RecordError) as e:
        Journal(tmp_path, "tenant-a").append(
            {"kind": "incident", "subject": "s"})
    assert "reported_by" in str(e.value)


@pytest.mark.parametrize("metrics,fragment", [
    ({"quality": 1.5}, "quality"),
    ({"quality": "high"}, "quality"),
    ({"latency_ms": -1}, "latency_ms"),
    ({"cost_units": -0.1}, "cost_units"),
    ({"mystery": 1}, "mystery"),
])
def test_metric_validation(tmp_path, metrics, fragment):
    with pytest.raises(RecordError) as e:
        Journal(tmp_path, "tenant-a").append(incident(metrics=metrics))
    assert fragment in str(e.value)


def test_valid_metrics_roundtrip(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    rec = j.append(incident(metrics={"quality": 0.92, "cost_units": 1.4,
                                     "latency_ms": 812}))
    assert rec["metrics"] == {"quality": 0.92, "cost_units": 1.4,
                              "latency_ms": 812}


@pytest.mark.parametrize("evidence,fragment", [
    ([{"source": "", "ref": "x", "verified": True}], "source"),
    ([{"source": "mon", "ref": "", "verified": True}], "ref"),
    ([{"source": "mon", "ref": "x", "verified": "yes"}], "verified"),
    ([{"source": "mon", "ref": "x"}], "verified"),
    ({"source": "mon", "ref": "x", "verified": True}, "list"),
])
def test_evidence_validation(tmp_path, evidence, fragment):
    with pytest.raises(RecordError) as e:
        Journal(tmp_path, "tenant-a").append(incident(evidence=evidence))
    assert fragment in str(e.value)


def test_bad_ts_is_rejected(tmp_path):
    with pytest.raises(RecordError) as e:
        Journal(tmp_path, "tenant-a").append(incident(ts="yesterday"))
    assert "ts" in str(e.value)


# ---------------------------------------------------------------------------
# Cross-references must resolve inside the same tenant journal.
# ---------------------------------------------------------------------------

def test_repair_for_unknown_incident_is_rejected(tmp_path):
    with pytest.raises(RecordError) as e:
        Journal(tmp_path, "tenant-a").append(
            {"kind": "repair", "subject": "s", "performed_by": "eng",
             "summary": "r", "for_incident": "tenant-a-000099",
             "outcome": "resolved"})
    assert "tenant-a-000099" in str(e.value)


def test_repair_for_a_non_incident_is_rejected(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    inc = j.append(incident())
    # Same subject on purpose: the refusal this test pins is the KIND
    # mismatch (proposal is not an incident), not a cross-subject one.
    prop = j.append({"kind": "lesson_proposal", "subject": "backup-rotation",
                     "proposed_by": "reviewer-a", "proposal": "p",
                     "basis": [inc["id"]]})
    with pytest.raises(RecordError) as e:
        j.append({"kind": "repair", "subject": "backup-rotation",
                  "performed_by": "eng", "summary": "r",
                  "for_incident": prop["id"], "outcome": "resolved"})
    assert "incident" in str(e.value)
    assert "subject" not in str(e.value).split("must name")[0]


def test_recurrence_of_unknown_incident_is_rejected(tmp_path):
    with pytest.raises(RecordError):
        Journal(tmp_path, "tenant-a").append(
            incident(recurrence_of="tenant-a-000042"))


def test_recurrence_of_existing_incident_is_accepted(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    inc = j.append(incident())
    rec = j.append(incident(summary="again", recurrence_of=inc["id"]))
    assert rec["recurrence_of"] == inc["id"]


def test_proposal_basis_must_resolve(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    with pytest.raises(RecordError):
        j.append({"kind": "lesson_proposal", "subject": "backup-rotation",
                  "proposed_by": "reviewer-a", "proposal": "p",
                  "basis": ["tenant-a-000007"]})
    inc = j.append(incident())
    rec = j.append({"kind": "lesson_proposal", "subject": "backup-rotation",
                    "proposed_by": "reviewer-a", "proposal": "p",
                    "basis": [inc["id"]]})
    assert rec["basis"] == [inc["id"]]


# ---------------------------------------------------------------------------
# Tenant scoping is structural: the name IS the directory.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant", ["../escape", "a/b", "", "a b", ".hidden"])
def test_bad_tenant_names_are_rejected(tmp_path, tenant):
    with pytest.raises(ValueError):
        Journal(tmp_path, tenant)


def test_tenants_are_isolated_by_directory(tmp_path):
    a = Journal(tmp_path, "tenant-a")
    b = Journal(tmp_path, "tenant-b")
    ra = a.append(incident())
    rb = b.append(incident(summary="other tenant"))
    assert [r["id"] for r in a.read()] == [ra["id"]]
    assert [r["id"] for r in b.read()] == [rb["id"]]
    assert ra["id"] != rb["id"]


def test_approvals_roundtrip(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    rec = j.append_approval({"subject": "backup-rotation", "stage": "sandbox",
                             "approved_by": "approver-a",
                             "approved_at": "2026-10-10T00:00:00+00:00"})
    assert rec["id"] == "tenant-a-apr-000001"
    rows = j.read_approvals()
    assert len(rows) == 1 and rows[0]["stage"] == "sandbox"


def test_approval_schema_is_closed(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    with pytest.raises(RecordError):
        j.append_approval({"subject": "s", "stage": "sandbox",
                           "approved_by": "a",
                           "approved_at": "2026-10-10T00:00:00+00:00",
                           "notes": "extra"})
    with pytest.raises(RecordError):
        j.append_approval({"subject": "s", "stage": "production",
                           "approved_by": "a",
                           "approved_at": "2026-10-10T00:00:00+00:00"})
