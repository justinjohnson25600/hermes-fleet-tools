"""New exception approvals require explicit instants; historical drift stays readable."""
from datetime import datetime, timezone

import pytest

from fleettools.policy import PolicySourceError, compile_effective, drift_report, load_sources
from policy_fixtures import client_body, write_policy

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def exception():
    return {'id': 'fixture-exception', 'capability': 'backup', 'path': '/var/secrets',
            'approved_by': 'fixture-approver', 'approved_at': '2026-10-01T00:00:00Z',
            'expires_at': '2026-10-15T00:00:00Z'}


@pytest.mark.parametrize('field', ['approved_at', 'expires_at'])
@pytest.mark.parametrize('stamp', ['2026-10-10T11:00:00', '2026-10-10'])
@pytest.mark.parametrize('consumer', ['file', 'direct'])
def test_source_exception_requires_explicit_timezone(tmp_path, field, stamp, consumer):
    approval = exception() | {field: stamp}
    body = client_body(caps={'backup': {'paths': ['/var/secrets']}}, exceptions=[approval])
    common = {'protected_paths': ['/var/secrets']}
    with pytest.raises(PolicySourceError) as exc:
        if consumer == 'file':
            load_sources(write_policy(tmp_path, common=common, clients={'client-a': body}))
        else:
            compile_effective({'common': common, 'roles': {'worker': {}},
                               'clients': {'client-a': body}}, NOW)
    assert field in str(exc.value)
    assert 'timestamp' in str(exc.value) or 'timezone' in str(exc.value)


@pytest.mark.parametrize('approved,expires', [
    ('2026-10-10T00:00:00Z', '2026-10-11T00:00:00Z'),
    ('2026-10-10T14:00:00+14:00', '2026-10-11T14:00:00+14:00'),
    ('2026-10-09T10:00:00-14:00', '2026-10-10T10:00:00-14:00'),
])
def test_offset_spelling_preserves_same_approval_window(tmp_path, approved, expires):
    approval = exception() | {'approved_at': approved, 'expires_at': expires}
    policy = write_policy(tmp_path, common={'protected_paths': ['/var/secrets']},
                          clients={'client-a': client_body(caps={'backup': {'paths': ['/var/secrets']}},
                                                           exceptions=[approval])})
    sources = load_sources(policy)
    assert compile_effective(sources, NOW)['client-a'].exceptions_active[0]['id'] == approval['id']
    expiry = datetime(2026, 10, 11, tzinfo=timezone.utc)
    from fleettools.policy import PolicyCompileError
    with pytest.raises(PolicyCompileError):
        compile_effective(sources, expiry)


def test_historical_naive_exception_drift_keeps_utc_compatibility(tmp_path):
    sources = load_sources(write_policy(tmp_path, clients={'client-a': client_body()}))
    current = {name: item.as_dict() for name, item in compile_effective(sources, NOW).items()}
    old = {name: dict(item) for name, item in current.items()}
    old['client-a']['exceptions_active'] = [exception() | {'expires_at': '2026-10-10T11:00:00'}]
    rows = drift_report(old, current, now=NOW)
    assert any(row['kind'] == 'exception_expired' for row in rows)
