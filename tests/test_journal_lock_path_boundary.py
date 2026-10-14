"""Conservative alias refusal and typed acquisition after real directory loss."""
import os

import pytest

from fleettools import journal


def test_disappearing_directory_before_lock_open_is_typed_and_recoverable(tmp_path, monkeypatch):
    store = journal.Journal(tmp_path / 'root', 'tenant')
    original = os.open
    retired = []

    def retire_then_open(path, *args, **kwargs):
        if path == str(store.lock_path) and not retired:
            store.dir.rmdir()
            retired.append(True)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patcher:
        patcher.setattr(journal.os, 'open', retire_then_open)
        with pytest.raises(journal.JournalPathRefused):
            store.read()
    assert retired
    # Recovery on the same object proves its in-process lock was released.
    assert store.read() == []


def test_sensitive_volume_preexisting_conflicts_refuse_both_exact_names(tmp_path):
    first, second = tmp_path / 'tenant-a', tmp_path / 'TENANT-A'
    first.mkdir()
    try:
        second.mkdir()
    except FileExistsError:
        pytest.skip('Actual volume folds case: cannot materialize both legacy entries')
    assert not first.samefile(second)
    for path in [first, second]:
        (path / 'marker').write_bytes(path.name.encode('utf-8'))
    for path in [first, second]:
        with pytest.raises(journal.JournalAliasRefused):
            journal.Journal(tmp_path, path.name).read()
    assert {path.name: (path / 'marker').read_bytes() for path in [first, second]} == {
        path.name: path.name.encode('utf-8') for path in [first, second]}
