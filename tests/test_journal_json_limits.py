"""Known JSON/parser and finite-metric boundaries also apply to the journal."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools.journal import Journal, JournalCorrupt, RecordError

ROOT = Path(__file__).resolve().parents[1]
BASE = {'kind': 'incident', 'subject': 's', 'reported_by': 'fixture', 'summary': 'fixture'}


def raw_parser_fault(kind):
    if kind == 'integer':
        limit = getattr(sys, 'get_int_max_str_digits', lambda: 0)()
        if not limit:
            pytest.skip('No enabled integer parser digit limit')
        value = '9' * (limit + 100)
    else:
        depth = sys.getrecursionlimit() + 1000
        value = '[' * depth + '0' + ']' * depth
    return ('{"payload":' + value + '}').encode('ascii')


@pytest.mark.parametrize('kind', ['integer', 'nesting'])
@pytest.mark.parametrize('store', ['data', 'approvals'])
def test_stored_parser_limits_are_corruption(tmp_path, kind, store):
    j = Journal(tmp_path, 'tenant')
    j.read()
    path = j.data_path if store == 'data' else j.approvals_path
    before = raw_parser_fault(kind) + b'\n'
    path.write_bytes(before)
    with pytest.raises(JournalCorrupt) as exc:
        j.read() if store == 'data' else j.read_approvals()
    assert path.name in str(exc.value)
    assert path.read_bytes() == before


@pytest.mark.parametrize('kind', ['integer', 'nesting'])
def test_cli_caller_parser_limits_are_usage_refusal(tmp_path, kind):
    record = tmp_path / 'record.json'
    record.write_bytes(raw_parser_fault(kind))
    result = subprocess.run([sys.executable, '-m', 'fleettools.journal', '--journal',
                             str(tmp_path / 'journal'), '--tenant', 'tenant', 'append',
                             '--file', str(record)], cwd=ROOT, capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 2
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('number', [float('nan'), float('inf'), float('-inf')])
@pytest.mark.parametrize('metric', ['quality', 'cost_units', 'latency_ms'])
def test_metric_must_be_finite_before_append(tmp_path, metric, number):
    j = Journal(tmp_path, 'tenant')
    first = j.append(dict(BASE))
    before = j.data_path.read_bytes()
    with pytest.raises(RecordError):
        j.append(BASE | {'metrics': {metric: number}})
    assert j.data_path.read_bytes() == before
    assert j.read() == [first]


def test_stored_nan_metric_is_corruption(tmp_path):
    j = Journal(tmp_path, 'tenant')
    row = j.append(dict(BASE))
    row['metrics'] = {'cost_units': float('nan')}
    before = (json.dumps(row) + '\n').encode('utf-8')
    j.data_path.write_bytes(before)
    with pytest.raises(JournalCorrupt):
        j.read()
    assert j.data_path.read_bytes() == before


def test_deep_caller_garbage_is_record_refusal(tmp_path):
    payload = 0
    for _ in range(sys.getrecursionlimit() + 100):
        payload = [payload]
    with pytest.raises(RecordError):
        Journal(tmp_path, 'tenant').append(BASE | {'summary': payload})


def test_valid_finite_metrics_and_unicode_remain_supported(tmp_path):
    j = Journal(tmp_path, 'tenant')
    row = j.append(BASE | {'summary': 'café 技能 😀',
                          'metrics': {'quality': 0.75, 'cost_units': 2, 'latency_ms': 100}})
    assert j.read() == [row]
