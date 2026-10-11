"""Protection grows, capabilities actually narrow, and layer schemas close."""
from datetime import datetime, timezone
import pytest
from fleettools.policy import PolicyCompileError, PolicySourceError, compile_effective, load_sources
from policy_fixtures import client_body, write_policy

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize('layer', ['role', 'client'])
@pytest.mark.parametrize('base,ancestor,grant', [
    ('/var/secrets/db', '/var/secrets', '/var/secrets/other'),
    ('C:/Secrets/db', 'c:/secrets', 'C:/Secrets/other'),
])
def test_lower_layer_ancestor_protection_cannot_disappear(tmp_path, layer, base, ancestor, grant):
    body=client_body(caps={'backup':{'paths':[grant]}})
    kwargs={}
    if layer=='client':
        body['protected_paths']=[ancestor]
    else:
        kwargs['roles']={'worker':{'protected_paths':[ancestor]}}
    write_policy(tmp_path,common={'protected_paths':[base]},clients={'client-a':body},**kwargs)
    with pytest.raises(PolicyCompileError):
        compile_effective(load_sources(tmp_path),NOW)


@pytest.mark.parametrize('base,narrow', [('/data','/data/client'),('C:/Data','c:/data/client')])
def test_constrained_lower_grant_really_narrows_effective_coverage(tmp_path, base, narrow):
    write_policy(tmp_path,common={'capabilities':{'backup':{'paths':[base]}}},
                 clients={'client-a':client_body(caps={'backup':{'paths':[narrow]}})})
    result=compile_effective(load_sources(tmp_path),NOW)['client-a']
    assert result.capabilities['backup']['paths']==[narrow]


@pytest.mark.parametrize('path', [r'\\fixture-server\NUL\x',r'\\fixture-server\con.txt\x'])
def test_reserved_unc_anchor_component_refused(tmp_path,path):
    write_policy(tmp_path,clients={'client-a':client_body(caps={'backup':{'paths':[path]}})})
    with pytest.raises(PolicySourceError):
        load_sources(tmp_path)


def test_role_does_not_silently_accept_another_role_selector(tmp_path):
    write_policy(tmp_path,roles={'worker':{'role':'other-role'}},
                 clients={'client-a':client_body()})
    with pytest.raises(PolicySourceError):
        load_sources(tmp_path)


def test_unknown_exception_key_refused(tmp_path):
    e={'id':'fixture-exception','capability':'backup','path':'/fixture',
       'approved_by':'fixture-reviewer','approved_at':'2026-01-01T00:00:00Z',
       'expires_at':'2027-01-01T00:00:00Z'}
    e['unrecognised']='ignored approval metadata'
    write_policy(tmp_path,clients={'client-a':client_body(exceptions=[e])})
    with pytest.raises(PolicySourceError):
        load_sources(tmp_path)
