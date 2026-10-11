"""Windows paths with Win32 aliases are not safe lexical policy inputs."""
from datetime import datetime, timezone
import pytest
from fleettools.policy import PolicySourceError, compile_effective, load_sources
from policy_fixtures import client_body, write_policy


@pytest.mark.parametrize('path', [
    'C:/Secrets./db', 'C:/Secrets /db', 'C:/Secrets' + ':' + 'stream',
    'C:/Secrets/db' + ':' + 'stream', 'C:/Secrets/NUL', 'C:/Secrets/COM1.txt',
])
def test_windows_alias_or_device_path_is_not_certified(tmp_path, path):
    write_policy(tmp_path, common={'protected_paths': ['C:/Secrets']},
                 clients={'client-a': client_body(caps={'backup': {'paths': [path]}})})
    with pytest.raises(PolicySourceError):
        compile_effective(load_sources(tmp_path), now=datetime(2026, 10, 10, tzinfo=timezone.utc))
