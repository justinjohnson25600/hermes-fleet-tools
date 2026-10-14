"""Malformed execution flags and unreadable roster input never certify."""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from policy_fixtures import client_body,write_policy

ROOT=Path(__file__).resolve().parents[1]


def gate(tmp_path,raw):
    roster=tmp_path/'agents.json'
    roster.write_bytes(raw)
    policy=write_policy(tmp_path/'policy',common={'capabilities':{'skill_deploy':None}},
                        clients={'client-a':client_body()})
    result=subprocess.run([sys.executable,'-m','fleettools.deploy','--check-policy',
                           '--policy-dir',str(policy),'--roster',str(roster)],cwd=ROOT,
                          capture_output=True,text=True,timeout=30)
    return result


@pytest.mark.parametrize('bad',['false','no',1,[],{},None])
def test_enabled_is_boolean_not_truthiness(tmp_path,bad):
    result=gate(tmp_path,json.dumps({'agents':[{'name':'fixture','identity':'enr-host-1',
                  'role':'worker','enabled':bad}]}).encode('utf-8'))
    assert result.returncode==1
    assert 'ROSTER REFUSED' in result.stdout+result.stderr
    assert 'enabled' in result.stdout+result.stderr
    assert 'ALLOW fixture' not in result.stdout
    assert 'Traceback' not in result.stderr


def test_roster_invalid_utf8_is_contextual_refusal(tmp_path):
    result=gate(tmp_path,b'\xff')
    assert result.returncode==1
    assert 'ROSTER REFUSED' in result.stdout+result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.skipif(os.name=='nt',reason='POSIX mode-bit refusal; native Windows uses ACLs')
def test_roster_permission_error_is_contextual_refusal(tmp_path):
    # Real read failure, not a mocked loader exception.
    roster=tmp_path/'agents.json';roster.write_text('{}',encoding='utf-8')
    policy=write_policy(tmp_path/'policy',clients={'client-a':client_body()})
    roster.chmod(0)
    try:
        result=subprocess.run([sys.executable,'-m','fleettools.deploy','--check-policy',
                               '--policy-dir',str(policy),'--roster',str(roster)],cwd=ROOT,
                              capture_output=True,text=True,timeout=30)
    finally:roster.chmod(0o600)
    assert result.returncode==1
    assert 'ROSTER REFUSED' in result.stdout+result.stderr
    assert 'Traceback' not in result.stderr
