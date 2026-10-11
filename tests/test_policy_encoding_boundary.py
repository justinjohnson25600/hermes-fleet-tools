"""Policy source and snapshot decoding failures remain domain refusals."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools.policy import PolicySourceError, load_snapshot, load_sources
from policy_fixtures import client_body, write_policy

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    return subprocess.run([sys.executable, '-m', *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('relative', ['common.json', 'roles/worker.json',
                                      'clients/client-a.json'])
@pytest.mark.parametrize('consumer', ['api', 'compile', 'gate'])
def test_invalid_utf8_policy_is_contextual_refusal(tmp_path, relative, consumer):
    policy = write_policy(tmp_path / 'policy',
                          common={'capabilities': {'skill_deploy': None}},
                          clients={'client-a': client_body()})
    (policy / relative).write_bytes(b'\xff')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_sources(policy)
        assert Path(relative).name in str(exc.value)
        return
    if consumer == 'compile':
        result = run_cli('fleettools.policy', '--policy-dir', str(policy), 'compile')
    else:
        roster = tmp_path / 'agents.json'
        roster.write_text(json.dumps({'agents': [{'name': 'fixture',
                           'identity': 'enr-host-1', 'role': 'worker',
                           'enabled': True}]}), encoding='utf-8')
        result = run_cli('fleettools.deploy', '--check-policy', '--policy-dir',
                         str(policy), '--roster', str(roster))
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert Path(relative).name in output
    assert 'Traceback' not in output
    assert 'ALLOW fixture' not in output


@pytest.mark.parametrize('consumer', ['api', 'drift'])
def test_invalid_utf8_snapshot_is_contextual_refusal(tmp_path, consumer):
    snapshot = tmp_path / 'broken-snapshot.json'
    snapshot.write_bytes(b'\xff')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_snapshot(snapshot)
        assert snapshot.name in str(exc.value)
        return
    policy = write_policy(tmp_path / 'policy',
                          clients={'client-a': client_body()})
    result = run_cli('fleettools.policy', '--policy-dir', str(policy), 'drift',
                     '--against', str(snapshot))
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert snapshot.name in output
    assert 'Traceback' not in output


def test_valid_utf8_policy_and_snapshot_remain_readable(tmp_path):
    policy = write_policy(tmp_path / 'policy', clients={'client-a': client_body()})
    (policy / 'common.json').write_text(
        json.dumps({'capabilities': {'技能-é-😀': None}}, ensure_ascii=False),
        encoding='utf-8')
    load_sources(policy)
    snapshot = tmp_path / 'snapshot.json'
    result = run_cli('fleettools.policy', '--policy-dir', str(policy), 'snapshot',
                     '--out', str(snapshot))
    assert result.returncode == 0, result.stdout + result.stderr
    assert '技能-é-😀' in load_snapshot(snapshot)['policies']['client-a']['capabilities']
