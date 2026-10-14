"""CPython parser and canonicalizer limits produce domain refusals, not crashes."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools import deploy
from fleettools.policy import PolicySourceError, _validate_json_values, load_snapshot, load_sources
from policy_fixtures import client_body, write_policy

ROOT = Path(__file__).resolve().parents[1]


def raw_input(kind):
    if kind == 'integer':
        limit = getattr(sys, 'get_int_max_str_digits', lambda: 0)()
        if not limit:
            pytest.skip('Interpreter has no enabled integer digit limit')
        value = '9' * (limit + 100)
    else:
        depth = sys.getrecursionlimit() + 1000
        value = '[' * depth + '0' + ']' * depth
    return ('{"payload":' + value + '}').encode('ascii')


def cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, '-m', *args], cwd=cwd,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('kind', ['integer', 'nesting'])
@pytest.mark.parametrize('relative', ['common.json', 'roles/worker.json',
                                      'clients/client-a.json'])
@pytest.mark.parametrize('consumer', ['api', 'compile', 'gate'])
def test_policy_parser_limits_are_contextual_refusals(tmp_path, kind, relative, consumer):
    policy = write_policy(tmp_path / 'policy', clients={'client-a': client_body()})
    (policy / relative).write_bytes(raw_input(kind))
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_sources(policy)
        assert Path(relative).name in str(exc.value)
        return
    if consumer == 'compile':
        result = cli('fleettools.policy', '--policy-dir', str(policy), 'compile')
    else:
        roster = tmp_path / 'agents.json'
        roster.write_text(json.dumps({'agents': [{'name': 'fixture', 'enabled': True,
                       'identity': 'enr-host-1', 'role': 'worker'}]}), encoding='utf-8')
        result = cli('fleettools.deploy', '--check-policy', '--policy-dir',
                     str(policy), '--roster', str(roster))
    assert result.returncode == 1
    assert Path(relative).name in result.stdout + result.stderr
    assert 'Traceback' not in result.stdout + result.stderr
    assert 'ALLOW fixture' not in result.stdout


@pytest.mark.parametrize('kind', ['integer', 'nesting'])
@pytest.mark.parametrize('consumer', ['api', 'drift'])
def test_snapshot_parser_limits_are_contextual_refusals(tmp_path, kind, consumer):
    snapshot = tmp_path / 'parser-limit.json'
    snapshot.write_bytes(raw_input(kind))
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_snapshot(snapshot)
        assert snapshot.name in str(exc.value)
        return
    policy = write_policy(tmp_path / 'policy', clients={'client-a': client_body()})
    result = cli('fleettools.policy', '--policy-dir', str(policy), 'drift',
                 '--against', str(snapshot))
    assert result.returncode == 1
    assert snapshot.name in result.stdout + result.stderr
    assert 'Traceback' not in result.stdout + result.stderr


@pytest.mark.parametrize('kind', ['integer', 'nesting'])
@pytest.mark.parametrize('route', ['explicit', 'cwd'])
@pytest.mark.parametrize('consumer', ['api', 'gate'])
def test_roster_parser_limits_are_refused_on_both_resolution_paths(tmp_path, monkeypatch,
                                                                 kind, route, consumer):
    roster = tmp_path / 'agents.json'
    roster.write_bytes(raw_input(kind))
    monkeypatch.delenv('FLEET_ROSTER', raising=False)
    if consumer == 'api':
        monkeypatch.setattr(deploy, '_ROSTER_OVERRIDE', str(roster) if route == 'explicit' else None)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(deploy.PolicyGateInputError) as exc:
            deploy._load_roster()
        assert 'parser limits' in str(exc.value)
        return
    policy = write_policy(tmp_path / 'policy', clients={'client-a': client_body()})
    args = [sys.executable, str(ROOT / 'fleettools' / 'deploy.py'), '--check-policy',
            '--policy-dir', str(policy)]
    if route == 'explicit':
        args.extend(['--roster', str(roster)])
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert 'ROSTER REFUSED' in result.stdout + result.stderr
    assert 'parser limits' in result.stdout + result.stderr
    assert 'Traceback' not in result.stdout + result.stderr


def test_canonicalizer_recursion_is_a_domain_refusal():
    raw = 0
    for _ in range(sys.getrecursionlimit() + 100):
        raw = [raw]
    with pytest.raises(PolicySourceError) as exc:
        _validate_json_values(raw, 'fixture-source.json')
    assert 'fixture-source.json' in str(exc.value)
