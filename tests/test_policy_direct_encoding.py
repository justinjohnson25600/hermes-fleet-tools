"""In-memory policy layers obey the file reader's canonical UTF-8 boundary."""
from datetime import datetime, timezone

import pytest

from fleettools.policy import PolicySourceError, compile_effective
from policy_fixtures import client_body

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize('layer', ['common', 'role', 'client'])
@pytest.mark.parametrize('slot', ['path', 'capability'])
def test_direct_layers_refuse_unencodable_strings(layer, slot):
    bad = {'capabilities': {'backup': {'paths': ['/var/\ud800']}}} if slot == 'path' else {
        'capabilities': {'\ud800': None}}
    sources = {'common': {}, 'roles': {'worker': {}},
               'clients': {'client-a': client_body()}}
    if layer == 'common':
        sources['common'] = bad
    elif layer == 'role':
        sources['roles']['worker'] = bad
    else:
        sources['clients']['client-a'].update(bad)
    with pytest.raises(PolicySourceError):
        compile_effective(sources, NOW)


def test_direct_unicode_capability_remains_valid():
    sources = {'common': {'capabilities': {'技能-é-😀': None}},
               'roles': {'worker': {}}, 'clients': {'client-a': client_body()}}
    result = compile_effective(sources, NOW)['client-a'].as_dict()
    assert result['capabilities']['技能-é-😀']['granted'] is True
