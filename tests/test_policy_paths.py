"""Path semantics: host-independent lexical component containment.

A grant reaching a protected path — by naming the path exactly, a
descendant of it, or an ancestor COVERING it — is load-bearing on the
protection and requires an in-window exception covering the whole reached
region. Comparison is by PATH MODEL (PurePosixPath for '/'-absolute,
PureWindowsPath for drive/UNC), never the local filesystem, so the same
policy compiles identically on every OS. Siblings ('/var/secrets-other')
are unrelated and stay usable.

    python3 -m pytest tests/test_policy_paths.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleettools.policy import (                    # noqa: E402
    PolicyCompileError,
    _apply_capabilities,
    _path_model,
    compile_effective,
    load_sources,
)
from policy_fixtures import client_body, write_policy  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def compile_dir(root, now=NOW):
    return compile_effective(load_sources(root), now=now)


# ---------------------------------------------------------------------------
# The path model itself: what is modelable, what is refused.
# ---------------------------------------------------------------------------

def test_posix_and_windows_models():
    assert _path_model("/var/secrets") == ("posix", _path_model(
        "/var/secrets")[1])
    assert _path_model("C:/Secrets")[0] == "windows"
    assert _path_model("c:\\secrets\\db")[0] == "windows"
    assert _path_model("//srv/share/x")[0] == "windows"


@pytest.mark.parametrize("bad", [
    "var/secrets",          # relative
    "./secrets", "../x", "/a/./b", "/a/../b",
    "/a/*/b", "/a/b?", "a[b]",          # globs
    " /var/secrets", "/var/secrets ",   # padding
    "a\nb", "/a\tb",                    # control characters
    "",                                 # empty
])
def test_unmodelable_paths_are_refused(bad):
    assert _path_model(bad) is None


def test_mixed_families_do_not_overlap():
    assert _path_model("/var/secrets")[0] != _path_model("C:/secrets")[0]
    # Different families never overlap: comparison is per-family only.
    from fleettools.policy import _paths_overlap
    assert not _paths_overlap(_path_model("/var/secrets"),
                              _path_model("C:/secrets"))


# ---------------------------------------------------------------------------
# POSIX: descendant, ancestor-covering, trailing slash, sibling, casing.
# ---------------------------------------------------------------------------

def test_posix_descendant_grant_inside_protected_is_refused(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/secrets/db"]}})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    msg = str(e.value)
    assert "/var/secrets/db" in msg and "/var/secrets" in msg


def test_posix_broad_ancestor_grant_covering_protected_is_refused(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var"]}})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "/var" in str(e.value)


def test_posix_trailing_slash_alias_reaches_protection(tmp_path):
    write_policy(tmp_path,
                 common={"protected_paths": ["/var/secrets/"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/secrets/db"]}})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "reaches protected path" in str(e.value)


def test_posix_sibling_of_protected_stays_usable(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/secrets-other"]}})})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.capabilities["backup"]["granted"] is True
    assert eff.capabilities["backup"]["paths"] == ["/var/secrets-other"]


def test_posix_comparison_is_case_sensitive(tmp_path):
    # POSIX paths are compared case-sensitively: /VAR/SECRETS/db is a
    # DIFFERENT path from /var/secrets/db, reaches nothing the protection
    # covers, and therefore compiles — no case-fold pretending the estate
    # protected it. (A case-aliasing story belongs to the Windows family,
    # which PureWindowsPath defines; see the tests below.)
    write_policy(tmp_path, common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/VAR/SECRETS/db"]}})})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.capabilities["backup"]["paths"] == ["/VAR/SECRETS/db"]


def test_unprotected_grant_of_exact_protected_alias_reaches(tmp_path):
    write_policy(tmp_path,
                 common={"protected_paths": ["/var/secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["/var/secrets/"]}})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "/var/secrets/" in str(e.value)


# ---------------------------------------------------------------------------
# Windows: drive/UNC case and separator aliases reach the protection.
# ---------------------------------------------------------------------------

def test_windows_drive_case_and_separator_alias_reaches(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["C:/Secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["c:\\secrets\\db"]}})})
    with pytest.raises(PolicyCompileError) as case:
        compile_dir(tmp_path)
    assert "reaches protected path" in str(case.value)


def test_windows_unc_case_and_separator_alias_reaches(tmp_path):
    write_policy(tmp_path,
                 common={"protected_paths": ["//srv/Share"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["\\\\SRV\\share\\db"]}})})
    with pytest.raises(PolicyCompileError) as e:
        compile_dir(tmp_path)
    assert "reaches protected path" in str(e.value)


def test_windows_sibling_stays_usable(tmp_path):
    write_policy(tmp_path, common={"protected_paths": ["C:/Secrets"]},
                 clients={"client-a": client_body(
                     caps={"backup": {"paths": ["C:/Secrets-other"]}})})
    eff = compile_dir(tmp_path)["client-a"]
    assert eff.capabilities["backup"]["paths"] == ["C:/Secrets-other"]


# ---------------------------------------------------------------------------
# Narrowing: containment narrows, intersection is a widen.
# ---------------------------------------------------------------------------

def _eff(caps):
    from fleettools.policy import EffectivePolicy
    return EffectivePolicy(client="c", role="r",
                           placement={"host_identity": "h"},
                           capabilities=caps, protected_paths=[])


def test_narrowing_to_descendant_is_legal():
    _apply_capabilities(
        _eff({"backup": {"granted": True,
                         "paths": ["/var/backups"], "note": None}}),
        {"capabilities": {"backup": {"paths": ["/var/backups/app"]}}})


def test_narrowing_to_sibling_is_a_widen():
    with pytest.raises(PolicyCompileError):
        _apply_capabilities(
            _eff({"backup": {"granted": True,
                             "paths": ["/var/backups"], "note": None}}),
            {"capabilities": {"backup": {"paths": ["/var/backups2"]}}})


def test_narrowing_to_ancestor_is_a_widen():
    with pytest.raises(PolicyCompileError):
        _apply_capabilities(
            _eff({"backup": {"granted": True,
                             "paths": ["/var/backups/app"], "note": None}}),
            {"capabilities": {"backup": {"paths": ["/var/backups"]}}})


def test_narrowing_alias_collapse_keeps_upper_spelling():
    eff = _eff({"backup": {"granted": True,
                           "paths": ["/var/backups/"], "note": None}})
    _apply_capabilities(eff, {"capabilities": {"backup":
                                               {"paths": ["/var/backups"]}}})
    assert eff.capabilities["backup"]["paths"] == ["/var/backups/"]


def test_unconstrained_to_concrete_set_remains_a_legal_refinement():
    eff = _eff({"backup": {"granted": True, "paths": None, "note": None}})
    _apply_capabilities(eff, {"capabilities": {"backup":
                                               {"paths": ["/var/backups"]}}})
    assert eff.capabilities["backup"]["paths"] == ["/var/backups"]
    assert eff.capabilities["backup"]["granted"] is True
