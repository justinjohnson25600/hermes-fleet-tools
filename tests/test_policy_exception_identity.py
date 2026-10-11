"""Approval IDs identify one declared exception; drift compares coverage."""
from datetime import datetime, timezone
import pytest
from fleettools.policy import PolicySourceError, compile_effective, drift_report, load_sources
from policy_fixtures import client_body, write_policy

NOW=datetime(2026,10,10,tzinfo=timezone.utc)


def exception(path, capability='backup'):
    return {'id':'fixture-exception','capability':capability,'path':path,
            'approved_by':'fixture-reviewer','approved_at':'2026-01-01T00:00:00Z',
            'expires_at':'2027-01-01T00:00:00Z'}


@pytest.mark.parametrize('source',['files','direct'])
@pytest.mark.parametrize('second',[exception('/other'),exception('/fixture','skill_deploy')])
def test_duplicate_exception_identity_refused_before_compile(tmp_path,source,second):
    layer=client_body(exceptions=[exception('/fixture'),second])
    if source=='files':
        write_policy(tmp_path,clients={'client-a':layer})
        with pytest.raises(PolicySourceError,match='duplicate'):
            load_sources(tmp_path)
    else:
        with pytest.raises(PolicySourceError,match='duplicate'):
            compile_effective({'common':{},'roles':{'worker':{}},'clients':{'client-a':layer}},NOW)


@pytest.mark.parametrize('old,new',[
    ('C:/Secrets','c:\\secrets'),('/var/secrets','/var/secrets/'),
    ('/var/secrets/db','/var/secrets'),
])
def test_equivalent_or_expanded_protection_is_not_reported_removed(tmp_path,old,new):
    write_policy(tmp_path,common={'protected_paths':[old]},clients={'client-a':client_body()})
    before={k:v.as_dict() for k,v in compile_effective(load_sources(tmp_path),NOW).items()}
    write_policy(tmp_path,common={'protected_paths':[new]},clients={'client-a':client_body()})
    after={k:v.as_dict() for k,v in compile_effective(load_sources(tmp_path),NOW).items()}
    changes=drift_report(before,after,now=NOW)
    assert not any(c['kind']=='protected_path_removed' for c in changes)
    assert any(c['kind']=='hash_changed' for c in changes), 'verbatim spelling still participates in integrity'


def test_posix_case_changed_protection_is_a_real_delta(tmp_path):
    write_policy(tmp_path,common={'protected_paths':['/Secrets']},clients={'client-a':client_body()})
    before={k:v.as_dict() for k,v in compile_effective(load_sources(tmp_path),NOW).items()}
    write_policy(tmp_path,common={'protected_paths':['/secrets']},clients={'client-a':client_body()})
    after={k:v.as_dict() for k,v in compile_effective(load_sources(tmp_path),NOW).items()}
    kinds={c['kind'] for c in drift_report(before,after,now=NOW)}
    assert {'protected_path_removed','protected_path_added'} <= kinds
