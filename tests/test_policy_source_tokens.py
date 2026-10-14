"""Source JSON token refusal is exercised through both compiler and actual gate."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools.policy import PolicySourceError, load_sources
from policy_fixtures import client_body, write_policy

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('token', ['NaN', '1e999', '"\\ud800"'])
@pytest.mark.parametrize('consumer', ['api', 'compile', 'gate'])
def test_noncanonical_source_tokens_are_refused_before_semantic_validation(tmp_path, token, consumer):
    policy = write_policy(tmp_path / 'policy', clients={'client-a': client_body()})
    (policy / 'common.json').write_text(
        '{"capabilities":{"backup":{"paths":[' + token + ']}}}', encoding='utf-8')
    if consumer == 'api':
        with pytest.raises(PolicySourceError) as exc:
            load_sources(policy)
        assert 'common.json' in str(exc.value)
        assert 'invalid JSON value' in str(exc.value)
        return
    if consumer == 'compile':
        args = [sys.executable, '-m', 'fleettools.policy', '--policy-dir', str(policy), 'compile']
    else:
        roster = tmp_path / 'agents.json'
        roster.write_text(json.dumps({'agents': [{'name': 'fixture', 'identity': 'enr-host-1',
                    'role': 'worker', 'enabled': True}]}), encoding='utf-8')
        args = [sys.executable, '-m', 'fleettools.deploy', '--check-policy',
                '--policy-dir', str(policy), '--roster', str(roster)]
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert 'common.json' in result.stdout + result.stderr
    assert 'invalid JSON value' in result.stdout + result.stderr
    assert 'Traceback' not in result.stdout + result.stderr
    assert 'ALLOW fixture' not in result.stdout
