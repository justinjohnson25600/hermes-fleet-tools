"""Readiness, the promotion gate, and the journal CLI.

Readiness is evidence math: ELIGIBLE only with enough VERIFIED INDEPENDENT
evidence, no unresolved recurrence. Promotion is human-gated end to end:
sandbox -> holdout -> canary -> soak, each stage requiring an explicit
approval a human recorded. Proposals are stored text for humans — nothing
here applies one, replays a workflow, or fails an identity over.

    python3 -m pytest tests/test_journal_readiness.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.journal import (           # noqa: E402
    ApprovalRefused, Journal, PROMOTION_STAGES, assess, promotion_status,
    record_approval,
)

REPO = Path(__file__).resolve().parents[1]


def journal(tmp_path):
    return Journal(tmp_path, "tenant-a")


def chain(j, subject="backup-rotation", repair_evidence=None,
          outcome="resolved", incident_evidence=None):
    """incident -> repair with verified independent evidence by default."""
    inc = j.append({"kind": "incident", "subject": subject,
                    "reported_by": "oncall-a", "summary": "backups skipping",
                    "evidence": incident_evidence if incident_evidence is not None
                    else [{"source": "monitor-a", "ref": "mon#1",
                           "verified": True}]})
    rep = j.append({"kind": "repair", "subject": subject,
                    "performed_by": "eng-a",
                    "for_incident": inc["id"], "outcome": outcome,
                    "summary": "re_enabled the timer",
                    "evidence": repair_evidence if repair_evidence is not None
                    else [{"source": "monitor-a", "ref": "mon#2",
                           "verified": True},
                          {"source": "audit-b", "ref": "audit#9",
                           "verified": True}]})
    return inc, rep


# ---------------------------------------------------------------------------
# Readiness: evidence sufficiency.
# ---------------------------------------------------------------------------

def test_full_chain_with_two_independent_sources_is_eligible(tmp_path):
    chain(journal(tmp_path))
    result = assess(journal(tmp_path).read(), "backup-rotation")
    assert result["verdict"] == "ELIGIBLE"
    assert result["independent_sources"] == ["audit-b", "monitor-a"]
    assert result["reasons"] == []


def test_no_evidence_is_ineligible(tmp_path):
    j = journal(tmp_path)
    inc = j.append({"kind": "incident", "subject": "s",
                    "reported_by": "oncall-a", "summary": "x"})
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": inc["id"], "outcome": "resolved", "summary": "y"})
    result = assess(j.read(), "s")
    assert result["verdict"] == "INELIGIBLE"
    assert any("independent" in r for r in result["reasons"])


def test_unverified_evidence_does_not_count(tmp_path):
    j = journal(tmp_path)
    chain(j,
          incident_evidence=[{"source": "monitor-a", "ref": "mon#1",
                              "verified": False}],
          repair_evidence=[
        {"source": "monitor-a", "ref": "mon#2", "verified": False},
        {"source": "audit-b", "ref": "audit#9", "verified": False}])
    result = assess(j.read(), "backup-rotation")
    assert result["verdict"] == "INELIGIBLE"
    assert result["counts"]["verified_independent_sources"] == 0


def test_actor_owned_evidence_is_not_independent(tmp_path):
    j = journal(tmp_path)
    chain(j,
          incident_evidence=[{"source": "eng-a", "ref": "self#0",
                              "verified": True}],
          repair_evidence=[
        {"source": "eng-a", "ref": "self#1", "verified": True}])
    result = assess(j.read(), "backup-rotation")
    assert result["verdict"] == "INELIGIBLE"
    assert result["independent_sources"] == []


def test_min_sources_parameter(tmp_path):
    j = journal(tmp_path)
    chain(j, repair_evidence=[
        {"source": "monitor-a", "ref": "mon#2", "verified": True}])
    assert assess(j.read(), "backup-rotation", min_sources=1)["verdict"] == \
        "ELIGIBLE"
    assert assess(j.read(), "backup-rotation", min_sources=2)["verdict"] == \
        "INELIGIBLE"


def test_incident_evidence_counts_toward_independence(tmp_path):
    j = journal(tmp_path)
    chain(j, incident_evidence=[
        {"source": "pager-c", "ref": "page#1", "verified": True}],
        repair_evidence=[
            {"source": "monitor-a", "ref": "mon#2", "verified": True}])
    result = assess(j.read(), "backup-rotation", min_sources=2)
    assert result["verdict"] == "ELIGIBLE"
    assert "pager-c" in result["independent_sources"]
    assert "monitor-a" in result["independent_sources"]


def test_no_resolved_repair_is_ineligible(tmp_path):
    j = journal(tmp_path)
    chain(j, outcome="partial")
    result = assess(j.read(), "backup-rotation")
    assert result["verdict"] == "INELIGIBLE"
    assert any("resolved" in r for r in result["reasons"])


def test_no_incident_at_all_is_ineligible(tmp_path):
    j = journal(tmp_path)
    result = assess(j.read(), "never-happened")
    assert result["verdict"] == "INELIGIBLE"


def test_recurrence_after_the_repair_blocks_readiness(tmp_path):
    j = journal(tmp_path)
    inc, rep = chain(j)   # repair ts = now (2026-10-10 by test default)
    j.append({"kind": "incident", "subject": "backup-rotation",
              "reported_by": "oncall-a", "summary": "same failure returned",
              "recurrence_of": inc["id"]},
             ts="2027-01-01T00:00:00+00:00")   # AFTER the latest repair
    result = assess(j.read(), "backup-rotation")
    assert result["verdict"] == "INELIGIBLE"
    assert any("recurrence" in r for r in result["reasons"])


def test_recurrence_before_the_latest_repair_is_settled(tmp_path):
    j = journal(tmp_path)
    inc, rep = chain(j)
    j.append({"kind": "incident", "subject": "backup-rotation",
              "reported_by": "oncall-a", "summary": "early recurrence",
              "recurrence_of": inc["id"]}, ts="2026-01-02T00:00:00+00:00")
    j.append({"kind": "repair", "subject": "backup-rotation",
              "performed_by": "eng-a", "for_incident":
              j.read(kind="incident")[-1]["id"],
              "outcome": "resolved", "summary": "final fix",
              "evidence": [{"source": "monitor-a", "ref": "mon#3",
                            "verified": True},
                           {"source": "audit-b", "ref": "audit#10",
                            "verified": True}]}, ts="2026-01-05T00:00:00+00:00")
    assert assess(j.read(), "backup-rotation")["verdict"] == "ELIGIBLE"


def test_metrics_are_advisory_not_blocking(tmp_path):
    j = journal(tmp_path)
    inc, _ = chain(j)
    result = assess(j.read(), "backup-rotation")
    assert result["verdict"] == "ELIGIBLE"
    assert result["metrics_present"] == {"quality": False, "cost_units": False,
                                         "latency_ms": False}


def test_proposals_are_ignored_by_readiness(tmp_path):
    j = journal(tmp_path)
    inc, _ = chain(j)
    j.append({"kind": "lesson_proposal", "subject": "backup-rotation",
              "proposed_by": "reviewer-a",
              "proposal": "add an alert at 03:00",
              "basis": [inc["id"]]})
    result = assess(j.read(), "backup-rotation")
    assert result["verdict"] == "ELIGIBLE"
    assert result["counts"]["proposals"] == 1


def test_subjects_do_not_leak_into_each_other(tmp_path):
    j = journal(tmp_path)
    chain(j, subject="s1")
    j.append({"kind": "incident", "subject": "s2",
              "reported_by": "oncall-a", "summary": "x"})
    assert assess(j.read(), "s1")["verdict"] == "ELIGIBLE"
    assert assess(j.read(), "s2")["verdict"] == "INELIGIBLE"


# ---------------------------------------------------------------------------
# The promotion gate: human approvals, enforced sequence.
# ---------------------------------------------------------------------------

def test_promotion_stages_are_the_documented_four():
    assert PROMOTION_STAGES == ("sandbox", "holdout", "canary", "soak")


def test_promotion_status_starts_empty(tmp_path):
    j = journal(tmp_path)
    chain(j)
    status = promotion_status(j.read(), j.read_approvals(), "backup-rotation")
    assert status["complete"] is False
    assert status["next_required"] == "sandbox"
    assert [s["approved"] for s in status["stages"]] == [False] * 4


def test_first_stage_approval_does_not_require_evidence(tmp_path):
    j = journal(tmp_path)
    j.append({"kind": "incident", "subject": "s",
              "reported_by": "oncall-a", "summary": "x"})
    rec = record_approval(j, "s", "sandbox", "approver-a")
    assert rec["stage"] == "sandbox"


def test_approval_sequence_is_enforced(tmp_path):
    j = journal(tmp_path)
    chain(j)
    with pytest.raises(ApprovalRefused) as e:
        record_approval(j, "backup-rotation", "holdout", "approver-a")
    assert "sandbox" in str(e.value)


def test_evidence_based_stages_require_eligibility(tmp_path):
    j = journal(tmp_path)
    inc = j.append({"kind": "incident", "subject": "s",
                    "reported_by": "oncall-a", "summary": "x"})
    j.append({"kind": "repair", "subject": "s", "performed_by": "eng-a",
              "for_incident": inc["id"], "outcome": "resolved", "summary": "y"})
    record_approval(j, "s", "sandbox", "approver-a")     # no evidence yet
    with pytest.raises(ApprovalRefused) as e:
        record_approval(j, "s", "holdout", "approver-a")
    assert "ELIGIBLE" in str(e.value)


def test_re_approval_is_refused_not_overwritten(tmp_path):
    j = journal(tmp_path)
    chain(j)
    record_approval(j, "backup-rotation", "sandbox", "approver-a")
    with pytest.raises(ApprovalRefused):
        record_approval(j, "backup-rotation", "sandbox", "approver-b")


def test_full_human_promotion_path_completes(tmp_path):
    j = journal(tmp_path)
    chain(j)
    for stage in PROMOTION_STAGES:
        record_approval(j, "backup-rotation", stage, "approver-a")
    status = promotion_status(j.read(), j.read_approvals(), "backup-rotation")
    assert status["complete"] is True
    assert status["next_required"] is None
    assert all(s["approved_by"] == "approver-a" for s in status["stages"])


# ---------------------------------------------------------------------------
# CLI: the real command surface (subprocess, exit codes, JSON on stdout).
# ---------------------------------------------------------------------------

def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "fleettools.journal", *args],
        capture_output=True, text=True, timeout=120, cwd=str(REPO))


def write_rec(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


def test_cli_cycle_append_readiness_approve_promotion(tmp_path):
    root = tmp_path / "jr"
    inc_file = write_rec(tmp_path, "inc.json", {
        "kind": "incident", "subject": "backup-rotation",
        "reported_by": "oncall-a", "summary": "backups skipping"})
    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "append", "--file", str(inc_file))
    assert r.returncode == 0, r.stderr
    inc_id = r.stdout.split()[-1]

    rep_file = write_rec(tmp_path, "rep.json", {
        "kind": "repair", "subject": "backup-rotation",
        "performed_by": "eng-a", "for_incident": inc_id,
        "outcome": "resolved", "summary": "fixed",
        "evidence": [{"source": "monitor-a", "ref": "m#1", "verified": True},
                     {"source": "audit-b", "ref": "a#1", "verified": True}]})
    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "append", "--file", str(rep_file))
    assert r.returncode == 0, r.stderr

    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "readiness", "--subject", "backup-rotation", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["verdict"] == "ELIGIBLE"

    for stage in PROMOTION_STAGES:
        r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                    "approve", "--subject", "backup-rotation",
                    "--stage", stage, "--approved-by", "approver-a")
        assert r.returncode == 0, r.stderr

    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "promotion", "--subject", "backup-rotation", "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["complete"] is True


def test_cli_readiness_ineligible_exits_1_with_json(tmp_path):
    root = tmp_path / "jr"
    inc_file = write_rec(tmp_path, "inc.json", {
        "kind": "incident", "subject": "s",
        "reported_by": "oncall-a", "summary": "x"})
    assert run_cli("--journal", str(root), "--tenant", "tenant-a",
                   "append", "--file", str(inc_file)).returncode == 0
    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "readiness", "--subject", "s", "--json")
    assert r.returncode == 1
    assert json.loads(r.stdout)["verdict"] == "INELIGIBLE"


def test_cli_bad_record_exits_2_with_field_name(tmp_path):
    root = tmp_path / "jr"
    bad = write_rec(tmp_path, "bad.json", {
        "kind": "incident", "subject": "s", "reported_by": "oncall-a",
        "summary": "x", "console_output": "secret-ish payload"})
    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "append", "--file", str(bad))
    assert r.returncode == 2
    assert "console_output" in r.stderr


def test_cli_out_of_order_approval_exits_1(tmp_path):
    root = tmp_path / "jr"
    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "approve", "--subject", "s", "--stage", "canary",
                "--approved-by", "approver-a")
    assert r.returncode == 1
    assert "sandbox" in r.stderr


def test_cli_read_reports_records(tmp_path):
    root = tmp_path / "jr"
    f = write_rec(tmp_path, "inc.json", {
        "kind": "incident", "subject": "s",
        "reported_by": "oncall-a", "summary": "x"})
    run_cli("--journal", str(root), "--tenant", "tenant-a",
            "append", "--file", str(f))
    r = run_cli("--journal", str(root), "--tenant", "tenant-a",
                "read", "--json")
    assert r.returncode == 0
    assert len(json.loads(r.stdout)) == 1
