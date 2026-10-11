"""Preserve declared UNC semantics and honest approval/roster attribution."""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from fleettools.policy import PolicyCompileError,PolicySourceError,compile_effective,drift_report,load_sources
from policy_fixtures import client_body,write_policy

NOW=datetime(2026,10,10,tzinfo=timezone.utc)


def exception():
    return {'id':'fixture-exception','capability':'backup','path':'/fixture',
            'approved_by':'reviewer','approved_at':'2026-01-01T00:00:00Z','expires_at':'2027-01-01T00:00:00Z'}


@pytest.mark.parametrize('slot',['protected','grant','exception'])
def test_declared_forward_slash_unc_protection_is_not_vacuous(tmp_path,slot):
    common={'protected_paths':[r'\\fixture\share']}
    body=client_body(caps={'backup':{'paths':[r'\\fixture\share\db']}})
    if slot=='protected':common['protected_paths']=['//fixture/share']
    elif slot=='grant':body['capabilities']['backup']['paths']=['//fixture/share/db']
    else:body['exceptions']=[exception()|{'path':'//fixture/share'}]
    write_policy(tmp_path,common=common,clients={'client-a':body})
    if slot=='exception':
        result=compile_effective(load_sources(tmp_path),NOW)['client-a']
        assert result.exceptions_active[0]['id']=='fixture-exception'
    else:
        with pytest.raises(PolicyCompileError):compile_effective(load_sources(tmp_path),NOW)


def test_explicit_backslash_unc_protection_still_reaches_descendant(tmp_path):
    write_policy(tmp_path,common={'protected_paths':[r'\\fixture\share']},
                 clients={'client-a':client_body(caps={'backup':{'paths':[r'\\fixture\share\db']}})})
    with pytest.raises(PolicyCompileError):compile_effective(load_sources(tmp_path),NOW)


def test_explicit_backslash_unc_narrowing_preserves_target(tmp_path):
    target=r'\\fixture\share\db'
    write_policy(tmp_path,common={'capabilities':{'backup':{'paths':[r'\\fixture\share']}}},
                 clients={'client-a':client_body(caps={'backup':{'paths':[target]}})})
    assert compile_effective(load_sources(tmp_path),NOW)['client-a'].capabilities['backup']['paths']==[target]


def test_still_declared_but_unused_exception_is_not_operator_removal(tmp_path):
    common={'protected_paths':['/fixture']}
    before_body=client_body(caps={'backup':{'paths':['/fixture']}},exceptions=[exception()])
    write_policy(tmp_path,common=common,clients={'client-a':before_body})
    old={k:v.as_dict() for k,v in compile_effective(load_sources(tmp_path),NOW).items()}
    write_policy(tmp_path,common=common,clients={'client-a':client_body(caps={'backup':False},exceptions=[exception()])})
    new={k:v.as_dict() for k,v in compile_effective(load_sources(tmp_path),NOW).items()}
    kinds={d['kind'] for d in drift_report(old,new,NOW)}
    assert 'exception_removed' not in kinds
    assert 'exception_no_longer_active' in kinds


@pytest.mark.parametrize('channel',['cli','env'])
def test_explicit_missing_roster_never_falls_back_to_cwd(tmp_path,channel):
    policy=write_policy(tmp_path/'policy',common={'capabilities':{'skill_deploy':None}},clients={'client-a':client_body()})
    (tmp_path/'agents.json').write_text(json.dumps({'agents':[{'name':'fixture','enabled':True,'identity':'enr-host-1','role':'worker'}]}),encoding='utf-8')
    script=Path(__file__).resolve().parents[1]/'fleettools/deploy.py'
    argv=[sys.executable,str(script),'--check-policy','--policy-dir',str(policy)]
    env=dict(os.environ);env.pop('FLEET_ROSTER',None)
    missing=str(tmp_path/'missing.json')
    if channel=='cli':argv+=['--roster',missing]
    else:env['FLEET_ROSTER']=missing
    result=subprocess.run(argv,cwd=tmp_path,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==1
    assert 'ROSTER REFUSED' in result.stdout+result.stderr
    assert 'ALLOW fixture' not in result.stdout
