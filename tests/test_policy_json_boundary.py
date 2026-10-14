"""Foreign JSON and actual snapshot publication failures never leak tracebacks."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools.policy import PolicySourceError, load_snapshot, load_sources, write_snapshot
from policy_fixtures import client_body, write_policy

ROOT = Path(__file__).resolve().parents[1]


def policy_fixture(tmp_path):
    return write_policy(tmp_path / 'policy', clients={'client-a': client_body()})


def cli(policy, *args):
    return subprocess.run([sys.executable, '-m', 'fleettools.policy', '--policy-dir',
                           str(policy), *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=30)


@pytest.mark.parametrize('token', ['NaN', 'Infinity', '-Infinity', '1e999', '-1e999'])
@pytest.mark.parametrize('location', ['protected_paths', 'capabilities', 'exceptions_active'])
@pytest.mark.parametrize('consumer', ['api', 'drift'])
def test_nonfinite_snapshot_json_is_refused_before_hashing(tmp_path, token, location, consumer):
    policy = policy_fixture(tmp_path)
    snapshot = tmp_path / 'foreign.json'
    raw = write_snapshot(policy, snapshot)
    stored = raw['policies']['client-a']
    if location == 'capabilities':
        stored[location] = {'foreign': {'nested': ['TOKEN_PLACEHOLDER']}}
    else:
        stored[location] = ['TOKEN_PLACEHOLDER']
    snapshot.write_text(json.dumps(raw).replace('"TOKEN_PLACEHOLDER"', token),
                        encoding='utf-8')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_snapshot(snapshot)
        assert snapshot.name in str(exc.value)
    else:
        result = cli(policy, 'drift', '--against', str(snapshot))
        assert result.returncode == 1
        assert snapshot.name in result.stdout + result.stderr
        assert 'Traceback' not in result.stdout + result.stderr


@pytest.mark.parametrize('slot', ['key', 'value'])
@pytest.mark.parametrize('consumer', ['api', 'drift'])
def test_surrogate_snapshot_json_is_contextual_refusal(tmp_path, slot, consumer):
    policy = policy_fixture(tmp_path)
    snapshot = tmp_path / 'surrogate.json'
    raw = write_snapshot(policy, snapshot)
    raw['policies']['client-a']['capabilities'] = {
        ('\ud800' if slot == 'key' else 'foreign'): {'nested': ['\ud800' if slot == 'value' else 'valid']}}
    snapshot.write_text(json.dumps(raw), encoding='utf-8')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_snapshot(snapshot)
        assert snapshot.name in str(exc.value)
    else:
        result = cli(policy, 'drift', '--against', str(snapshot))
        assert result.returncode == 1
        assert snapshot.name in result.stdout + result.stderr
        assert 'Traceback' not in result.stdout + result.stderr


@pytest.mark.parametrize('consumer', ['api', 'compile'])
def test_surrogate_policy_capability_refused_at_reader(tmp_path, consumer):
    policy = policy_fixture(tmp_path)
    (policy / 'common.json').write_text(json.dumps({'capabilities': {'\ud800': None}}),
                                      encoding='utf-8')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_sources(policy)
        assert 'common.json' in str(exc.value)
    else:
        result = cli(policy, 'compile')
        assert result.returncode == 1
        assert 'common.json' in result.stdout + result.stderr
        assert 'Traceback' not in result.stdout + result.stderr


@pytest.mark.parametrize('fault', ['parent_is_file', 'destination_is_directory'])
@pytest.mark.parametrize('consumer', ['api', 'snapshot'])
def test_snapshot_publication_failure_is_contextual_refusal(tmp_path, fault, consumer):
    policy = policy_fixture(tmp_path)
    if fault == 'parent_is_file':
        parent = tmp_path / 'not-a-directory'
        parent.write_bytes(b'preserved')
        out = parent / 'snapshot.json'
    else:
        out = tmp_path / 'snapshot.json'
        out.mkdir()
        (out / 'marker').write_bytes(b'preserved')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            write_snapshot(policy, out)
        assert 'snapshot' in str(exc.value).lower()
    else:
        result = cli(policy, 'snapshot', '--out', str(out))
        assert result.returncode == 1
        assert 'snapshot' in result.stdout + result.stderr
        assert 'Traceback' not in result.stdout + result.stderr
    if fault == 'parent_is_file':
        assert parent.read_bytes() == b'preserved'
    else:
        assert (out / 'marker').read_bytes() == b'preserved'
    assert not list(tmp_path.glob('snapshot.json*.tmp'))


@pytest.mark.parametrize('field,value', [
    ('protected_paths', [7]), ('protected_paths', [{}]),
    ('capabilities', {'foreign': 7}), ('capabilities', {'foreign': 'bad'}),
    ('capabilities', {'foreign': None}),
    ('capabilities', {'foreign': {'granted': 'false', 'paths': None}}),
    ('capabilities', {'foreign': {'granted': True, 'paths': [7]}}),
    ('exceptions_active', [7]), ('exceptions_active', [None]),
    ('exceptions_active', [{'id': [], 'expires_at': '2026-10-15T00:00:00Z'}]),
    ('exceptions_inactive', [{'id': []}]),
])
@pytest.mark.parametrize('consumer', ['api', 'drift'])
def test_snapshot_nested_shape_refused_before_drift(tmp_path, field, value, consumer):
    policy = policy_fixture(tmp_path)
    snapshot = tmp_path / 'wrong-shape.json'
    raw = write_snapshot(policy, snapshot)
    raw['policies']['client-a'][field] = value
    snapshot.write_text(json.dumps(raw), encoding='utf-8')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_snapshot(snapshot)
        assert field in str(exc.value)
    else:
        result = cli(policy, 'drift', '--against', str(snapshot))
        assert result.returncode == 1
        assert field in result.stdout + result.stderr
        assert 'Traceback' not in result.stdout + result.stderr


def test_finite_unicode_snapshot_drift_remains_report_only(tmp_path):
    policy = policy_fixture(tmp_path)
    snapshot = tmp_path / 'unicode.json'
    raw = write_snapshot(policy, snapshot)
    raw['policies']['client-a']['capabilities']['技能-é-😀'] = {
        'granted': True, 'paths': None}
    snapshot.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    assert '技能-é-😀' in load_snapshot(snapshot)['policies']['client-a']['capabilities']
    result = cli(policy, 'drift', '--against', str(snapshot), '--json')
    assert result.returncode == 0, result.stdout + result.stderr
    assert any(row['kind'] == 'snapshot_hash_mismatch' for row in json.loads(result.stdout))


@pytest.mark.parametrize('path', ['//srv./share', '//srv /share', '//NUL/share',
                                   r'\\srv.\share', r'\\srv \share', r'\\NUL\share'])
def test_unc_anchor_validation_does_not_depend_on_separator(path):
    from fleettools.policy import _path_model
    assert _path_model(path) is None
