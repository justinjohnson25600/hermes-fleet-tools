"""Parent regressions for complete admission/error semantics, not child snapshots."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fleettools import journal

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX permission bits; Windows uses real ACLs')
def test_cli_constructor_permission_error_is_domain_refusal(tmp_path):
    root = tmp_path / 'blocked-root'
    root.mkdir()
    root.chmod(0)
    try:
        result = subprocess.run([sys.executable, '-m', 'fleettools.journal', '--journal',
                                  str(root), '--tenant', 'tenant', 'read'], cwd=ROOT,
                                 capture_output=True, text=True, timeout=30)
    finally:
        root.chmod(0o700)
    assert result.returncode == 2
    assert 'Traceback' not in result.stderr
    assert 'blocked-root' in result.stderr


def test_cli_operation_time_path_failure_is_refused(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'retired-root'
    original = journal.Journal.read

    def retire_then_read(self, *args, **kwargs):
        # Real filesystem fault after construction/admission, not a fake exception.
        if root.exists():
            shutil.rmtree(root)
        root.write_bytes(b'preserved')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(journal.Journal, 'read', retire_then_read)
    result = journal.main(['--journal', str(root), '--tenant', 'tenant', 'read'])
    assert result == 2
    assert 'Traceback' not in capsys.readouterr().err
    assert root.read_bytes() == b'preserved'


@pytest.mark.parametrize('bad', [None, 7, [], {'subject': [], 'stage': 'sandbox'},
                                  {'subject': 's', 'stage': []}])
def test_direct_approval_identity_shape_is_refused(bad):
    with pytest.raises(journal.JournalCorrupt):
        journal.promotion_status([], [bad], 's')


def test_nonaliasing_volume_race_refuses_without_leaving_alias_directory(tmp_path):
    # Probe actual volume semantics; no sys.platform mocking.
    probe = tmp_path / 'probe'
    probe.mkdir()
    (probe / 'caf\u00e9').mkdir()
    try:
        (probe / 'cafe\u0301').mkdir()
    except FileExistsError:
        pytest.skip('Volume aliases NFC/NFD; race covered by aliasing-volume test')
    root = tmp_path / 'journal'
    first = journal.Journal(root, 'cafe\u0301')
    other = journal.Journal(root, 'caf\u00e9')
    row = first.append({'kind': 'incident', 'subject': 's', 'reported_by': 'fixture',
                        'summary': 'fixture'})
    with pytest.raises(ValueError):
        other.read()
    assert not other.dir.exists()
    assert first.read() == [row]


def test_api_acquisition_case_alias_remains_refused_after_constructors(tmp_path):
    first = journal.Journal(tmp_path, 'tenant-a')
    other = journal.Journal(tmp_path, 'TENANT-A')
    row = first.append({'kind': 'incident', 'subject': 's', 'reported_by': 'fixture',
                        'summary': 'fixture'})
    with pytest.raises(ValueError):
        other.read()
    assert first.read() == [row]
