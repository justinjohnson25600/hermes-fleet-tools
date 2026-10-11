"""Real ownership and admission races, not lock-file age snapshots."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools.journal import Journal, JournalLocked, ApprovalRefused, record_approval

ROOT = Path(__file__).resolve().parents[1]


def incident(subject="s", **extra):
    return dict(kind="incident", subject=subject, reported_by="actor", summary="failure", **extra)


def ready(journal):
    row = journal.append(incident(), ts="2026-01-01T00:00:00+00:00")
    journal.append(dict(kind="repair", subject="s", performed_by="actor", summary="repaired", for_incident=row["id"], outcome="resolved", evidence=[dict(source="observer-a", ref="proof-a", verified=True), dict(source="observer-b", ref="proof-b", verified=True)]), ts="2026-01-01T01:00:00+00:00")
    record_approval(journal, "s", "sandbox", "human")
    return row


def test_old_lock_never_evicts_a_live_writer(tmp_path):
    code = '''
import sys
sys.path.insert(0,sys.argv[1])
from fleettools.journal import Journal
class PausedWriter(Journal):
    def _write_all(self,path,rows):
        print('held',flush=True)
        sys.stdin.readline()
        return super()._write_all(path,rows)
PausedWriter(sys.argv[2],'tenant-a').append({'kind':'incident','subject':'s','reported_by':'writer','summary':'slow transaction'})
'''
    child = subprocess.Popen([sys.executable, "-c", code, str(ROOT), str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        j = Journal(tmp_path, "tenant-a", lock_timeout=0.1)
        os.utime(j.lock_path, (1, 1))
        with pytest.raises(JournalLocked):
            j.append(incident())
    finally:
        out, err = child.communicate("finish\n", timeout=15)
        assert child.returncode == 0, err


def test_promotion_rechecks_evidence_after_acquiring_writer_lock(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    row = ready(j)
    original = j._acquire
    intervened = False

    def competing_commit_before_acquire():
        nonlocal intervened
        if not intervened:
            intervened = True
            Journal(tmp_path, "tenant-a").append(incident(recurrence_of=row["id"]), ts="2026-01-01T02:00:00+00:00")
        original()

    j._acquire = competing_commit_before_acquire
    with pytest.raises(ApprovalRefused):
        record_approval(j, "s", "holdout", "human")
    assert [a["stage"] for a in j.read_approvals()] == ["sandbox"]


def test_direct_approval_cannot_bypass_stage_sequence(tmp_path):
    j = Journal(tmp_path, "tenant-a")
    with pytest.raises(ApprovalRefused):
        j.append_approval(dict(subject="s", stage="soak", approved_by="human", approved_at="2026-01-01T00:00:00+00:00"))
    assert j.read_approvals() == []
