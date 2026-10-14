"""Exception schema: `path` is REQUIRED (and must be a modelable absolute
policy path), and `approved_by` must be a non-empty human-readable string.

A load-bearing exception that does not name the path it exempts cannot be
matched to a protected grant — the "no in-window exception" refusal cites it
by id, but the record itself is incomplete, so the compile must refuse the
file. And an approver of `True`/`1` is a typed hole, not a name: a non-empty
STRING is required.

    python3 -m pytest tests/test_policy_exception_path.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.policy import PolicySourceError, load_sources  # noqa: E402
from policy_fixtures import client_body, write_policy  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def exc(eid="exc-1", capability="backup", path="/var/telemetry",
        approved_by="approver-a", approved_at="2026-10-01T00:00:00+00:00",
        expires_at="2026-12-01T00:00:00+00:00"):
    body = {"id": eid, "capability": capability, "approved_by": approved_by,
            "approved_at": approved_at, "expires_at": expires_at}
    if path is not None:
        body["path"] = path
    return body


def expect_refusal(tmp_path, mutate, fragment, require_id=True):
    write_policy(tmp_path,
                 clients={"client-a": client_body(exceptions=[exc(**mutate)])})
    with pytest.raises(PolicySourceError) as e:
        load_sources(tmp_path)
    msg = str(e.value)
    assert fragment in msg, msg
    # A blank path is refused by the typed check, whose message names the
    # file and field; the id suffix rides only on the missing-field branch.
    if require_id:
        assert "exc-1" in msg, msg


def test_exception_without_path_is_a_source_error(tmp_path):
    expect_refusal(tmp_path, {"path": None}, "path")


def test_exception_with_blank_path_is_a_source_error(tmp_path):
    expect_refusal(tmp_path, {"path": "  "}, "path", require_id=False)


@pytest.mark.parametrize("bad", [True, False, 1, 0, [], ["a"], {}])
def test_non_string_approved_by_is_a_source_error(tmp_path, bad):
    expect_refusal(tmp_path, {"approved_by": bad}, "approved_by")


def test_blank_approved_by_is_a_source_error(tmp_path):
    expect_refusal(tmp_path, {"approved_by": "   "}, "approved_by")


def test_a_well_formed_exception_still_compiles(tmp_path):
    from fleettools.policy import compile_effective
    write_policy(tmp_path, common={"protected_paths": ["/var/telemetry"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/telemetry"]}},
                     exceptions=[exc()])})
    eff = compile_effective(load_sources(tmp_path), now=NOW)
    assert eff["client-a"].exceptions_active[0]["id"] == "exc-1"
