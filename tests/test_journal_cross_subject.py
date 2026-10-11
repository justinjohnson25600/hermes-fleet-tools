"""Cross-subject references in the evidence journal are refused; readiness
isolates legacy cross-subject rows already on disk.

A repair, a recurrence, and a lesson proposal are about ONE subject. A
reference that resolves inside the tenant but belongs to another subject is
the same confusion wearing a valid id: it is refused at append (and the
journal file is byte-identical after the refusal), and assess() never lets
another subject's recurrence unblock or block this subject — including rows
written before the validation existed.

    python3 -m pytest tests/test_journal_cross_subject.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.journal import Journal, RecordError, assess  # noqa: E402

TENANT = "tenant-a"


def incident(subject, **over):
    rec = {"kind": "incident", "subject": subject,
           "reported_by": "oncall-a", "summary": "failure"}
    rec.update(over)
    return rec


def good_evidence(src_a="monitor-a", src_b="audit-b"):
    return [{"source": src_a, "ref": "m#1", "verified": True},
            {"source": src_b, "ref": "a#1", "verified": True}]


def eligible_chain(j, subject):
    """A same-subject chain that is ELIGIBLE but for recurrences."""
    inc = j.append(incident(subject), ts="2026-01-01T00:00:00+00:00")
    j.append({"kind": "repair", "subject": subject, "performed_by": "eng-a",
              "for_incident": inc["id"], "outcome": "resolved",
              "summary": "fixed", "evidence": good_evidence()},
             ts="2026-01-02T00:00:00+00:00")
    return inc


def data_bytes(j):
    return j.data_path.read_bytes()


# ---------------------------------------------------------------------------
# Append-time refusal: reference resolves, but the subject differs.
# ---------------------------------------------------------------------------

def test_repair_for_cross_subject_incident_is_rejected(tmp_path):
    j = Journal(tmp_path, TENANT)
    inc = j.append(incident("s1"))
    before = data_bytes(j)
    with pytest.raises(RecordError) as e:
        j.append({"kind": "repair", "subject": "s2", "performed_by": "eng-a",
                  "for_incident": inc["id"], "outcome": "resolved",
                  "summary": "r"})
    assert "subject" in str(e.value) and inc["id"] in str(e.value)
    assert data_bytes(j) == before, "a refused append must not write"


def test_recurrence_of_cross_subject_incident_is_rejected(tmp_path):
    j = Journal(tmp_path, TENANT)
    inc = j.append(incident("s1"))
    before = data_bytes(j)
    with pytest.raises(RecordError) as e:
        j.append(incident("s2", recurrence_of=inc["id"]))
    assert "subject" in str(e.value) and inc["id"] in str(e.value)
    assert data_bytes(j) == before, "a refused append must not write"


def test_proposal_basis_of_another_subject_is_rejected(tmp_path):
    j = Journal(tmp_path, TENANT)
    inc = j.append(incident("s1"))
    before = data_bytes(j)
    with pytest.raises(RecordError) as e:
        j.append({"kind": "lesson_proposal", "subject": "s2",
                  "proposed_by": "reviewer-a", "proposal": "p",
                  "basis": [inc["id"]]})
    assert "subject" in str(e.value)
    assert data_bytes(j) == before, "a refused append must not write"


def test_same_subject_references_are_still_accepted(tmp_path):
    j = Journal(tmp_path, TENANT)
    inc = j.append(incident("s1"))
    rec = j.append(incident("s1", summary="again", recurrence_of=inc["id"]))
    j.append({"kind": "repair", "subject": "s1", "performed_by": "eng-a",
              "for_incident": inc["id"], "outcome": "resolved",
              "summary": "r"})
    prop = j.append({"kind": "lesson_proposal", "subject": "s1",
                     "proposed_by": "reviewer-a", "proposal": "p",
                     "basis": [inc["id"], rec["id"]]})
    assert prop["basis"] == [inc["id"], rec["id"]]
    assert len(j.read()) == 4


# ---------------------------------------------------------------------------
# Readiness: another subject's recurrence never gates this subject —
# including malformed legacy rows read straight from disk.
# ---------------------------------------------------------------------------

def _write_legacy_journal(tmp_path, rows):
    """Rows that predate the append-time check: hand-written, schema-valid,
    cross-subject linkage included."""
    d = tmp_path / TENANT
    d.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r, separators=(",", ":")) + "\n"
                      for r in rows)
    (d / "journal.jsonl").write_text(payload, encoding="utf-8")


def test_assess_ignores_another_subjects_recurrence_from_disk(tmp_path):
    rows = [
        {"id": "tenant-a-000001", "seq": 1, "kind": "incident",
         "subject": "s1", "reported_by": "oncall-a", "summary": "x",
         "ts": "2026-01-01T00:00:00+00:00"},
        {"id": "tenant-a-000002", "seq": 2, "kind": "repair",
         "subject": "s1", "performed_by": "eng-a",
         "for_incident": "tenant-a-000001", "outcome": "resolved",
         "summary": "fixed", "ts": "2026-01-02T00:00:00+00:00",
         "evidence": good_evidence()},
        # Historically malformed: s2's recurrence points at s1's incident.
        {"id": "tenant-a-000003", "seq": 3, "kind": "incident",
         "subject": "s2", "reported_by": "oncall-b",
         "summary": "other subject's incident",
         "recurrence_of": "tenant-a-000001",
         "ts": "2027-06-01T00:00:00+00:00"},
    ]
    _write_legacy_journal(tmp_path, rows)
    j = Journal(tmp_path, TENANT)
    on_disk = j.read()
    assert assess(on_disk, "s1")["verdict"] == "ELIGIBLE"
    assert not any("recurrence" in r for r in assess(on_disk, "s1")["reasons"])
    # And s2 stays INELIGIBLE for its own honest reasons (no repair).
    assert assess(on_disk, "s2")["verdict"] == "INELIGIBLE"


def test_assess_still_blocks_same_subject_recurrence(tmp_path):
    j = Journal(tmp_path, TENANT)
    eligible_chain(j, "s1")
    j.append(incident("s1", summary="returned",
                      recurrence_of="tenant-a-000001"),
             ts="2027-01-01T00:00:00+00:00")
    result = assess(j.read(), "s1")
    assert result["verdict"] == "INELIGIBLE"
    assert any("recurrence" in r for r in result["reasons"])
