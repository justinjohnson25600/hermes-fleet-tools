"""A logical JSON object cannot present two different values for one field."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools.journal import Journal, JournalCorrupt, promotion_status, record_approval

ROOT = Path(__file__).resolve().parents[1]
BASE = {'kind': 'incident', 'subject': 's', 'reported_by': 'fixture', 'summary': 'fixture'}


def eligible(root):
    j = Journal(root, 'tenant')
    inc = j.append(BASE)
    j.append({'kind': 'repair', 'subject': 's', 'performed_by': 'fixture',
              'for_incident': inc['id'], 'outcome': 'resolved', 'summary': 'fixture',
              'evidence': [{'source': 'monitor-a', 'ref': 'm#1', 'verified': True},
                           {'source': 'audit-b', 'ref': 'a#1', 'verified': True}]})
    return j


def test_duplicate_stage_key_cannot_finish_sequence(tmp_path):
    j = eligible(tmp_path)
    for stage in ['sandbox', 'holdout', 'canary', 'soak']:
        record_approval(j, 's', stage, 'fixture')
    lines = j.approvals_path.read_text(encoding='utf-8').splitlines()
    lines[-1] = lines[-1].replace('"stage":"soak"', '"stage":"canary","stage":"soak"')
    before = ('\n'.join(lines) + '\n').encode('utf-8')
    j.approvals_path.write_bytes(before)
    with pytest.raises(JournalCorrupt):
        j.status_snapshot('s')
    assert j.approvals_path.read_bytes() == before


@pytest.mark.parametrize('store', ['data', 'approvals'])
def test_duplicate_field_is_corruption_on_both_stores(tmp_path, store):
    j = Journal(tmp_path, 'tenant')
    if store == 'data':
        row = j.append(BASE)
        path = j.data_path
        raw = json.dumps(row).replace('"summary": "fixture"', '"summary":"first","summary":"second"')
        consume = j.read
    else:
        row = j.append_approval({'subject': 's', 'stage': 'sandbox',
                                 'approved_by': 'fixture', 'approved_at': '2026-01-01T00:00:00Z'})
        path = j.approvals_path
        raw = json.dumps(row).replace('"approved_by": "fixture"', '"approved_by":"first","approved_by":"second"')
        consume = j.read_approvals
    before = (raw + '\n').encode('utf-8')
    path.write_bytes(before)
    with pytest.raises(JournalCorrupt):
        consume()
    assert path.read_bytes() == before


@pytest.mark.parametrize('nested', [False, True])
def test_cli_duplicate_caller_fields_are_input_refusal(tmp_path, nested):
    record = tmp_path / 'record.json'
    if nested:
        raw = json.dumps(BASE)[:-1] + ',"metrics":{"cost_units":1,"cost_units":2}}'
    else:
        raw = json.dumps(BASE).replace('"summary": "fixture"', '"summary":"first","summary":"second"')
    record.write_text(raw, encoding='utf-8')
    result = subprocess.run([sys.executable, '-m', 'fleettools.journal', '--journal',
                             str(tmp_path / 'journal'), '--tenant', 'tenant', 'append',
                             '--file', str(record)], cwd=ROOT, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 2
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('missing', ['approved_by', 'approved_at'])
def test_pure_approval_consumer_requires_display_fields(missing):
    row = {'subject': 's', 'stage': 'sandbox', 'approved_by': 'fixture',
           'approved_at': '2026-01-01T00:00:00Z'}
    row.pop(missing)
    with pytest.raises(JournalCorrupt):
        promotion_status([], [row], 's')


def test_valid_plain_object_roundtrip_unchanged(tmp_path):
    j = Journal(tmp_path, 'tenant')
    row = j.append(BASE | {'metrics': {'quality': 0.5, 'cost_units': 2}})
    assert j.read() == [row]
