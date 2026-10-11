#!/usr/bin/env python3
"""Stage B: the effective business-client policy compiler. DETERMINISTIC, READ-ONLY.

Merges per-tenant policy layers — common -> role -> client — into one canonical
effective policy per client, plus a content hash and a report-only drift
comparison against a stored snapshot. Nothing here applies a policy, mutates a
client, or touches any agent: the compiler decides and reports; humans act.

Layer semantics (per key, so drift is always explainable):

* ``capabilities`` fold in layer order under narrowing-only rules: a grant
  set to ``false`` (revoked) is FINAL — a lower layer cannot revive it; a
  path-constrained grant can only be narrowed downward by naming paths at
  or under the upper layer's paths (naming an unrelated path, an ancestor
  of them, or unconstraining a constrained grant is an error);
* ``protected_paths`` only ever GROW across layers (aliased spellings of an
  already-protected path do not add a second entry);
* ``role``, ``placement`` and ``exceptions`` are client-layer-only and are
  taken from the client file, never merged across layers;
* a grant naming (or covering, or covering-part-of) a protected path is
  legal ONLY while an explicit approved in-window exception covers the
  reached path set. At compile time the window is evaluated against
  ``now``: expired/not-yet-in-effect exceptions fail CLOSED (the compile
  errors) and every such failure is reported, per client;
* a grant with an EMPTY path list is refused at load: it would display as
  ``granted`` while authorising nothing. Revoke with ``false`` instead; an
  unconstrained grant (``null``) means everything EXCEPT protected paths.

Path semantics: policy paths are compared with host-independent LEXICAL
component semantics — PurePosixPath for single-leading-slash absolute strings,
PureWindowsPath for drive/UNC/backslash strings — never a filesystem, so
the same policy compiles identically on every OS. Overlap is checked in
BOTH directions (a descendant grant inside a protected subtree AND a broad
ancestor grant covering one both require an exception); POSIX comparison
is case-sensitive, Windows comparison follows PureWindowsPath (case- and
separator-insensitive). The original strings are preserved verbatim in
records and snapshots. Strings that cannot be modeled without inventing
semantics are refused: relative paths, ``.``/``..`` segments, glob
metacharacters, control characters.
Both ``//server/share`` and backslash UNC spellings intentionally select the
Windows namespace; ``//var/secrets`` names server ``var``, share ``secrets``,
not POSIX ``/var/secrets``. Use exactly one leading slash for POSIX paths.

Placement: each client names its host by stable enrolled identity, supplied
as data in the client file. Identity is never guessed here — not from
hostname, COMPUTERNAME or BIOS serial, all of which collide across imaged
machines (see deploy.py's identity notes). Enrolled ids are OPAQUE and
CASE-SENSITIVE: they are validated for shape (non-blank, no padding or
embedded whitespace, no control characters) and never case-folded or
normalized. Two clients may share a physical host only when each carries a
distinct explicit ``vm_identity``; anything else is an error rather than a
silent colocate.

Input layout (all JSON, all validated fail-closed — unknown keys are errors)::

    common.json                        shared layer (required)
    roles/<role>.json                  per-role layer (REQUIRED whenever a
                                       client names that role, even if the
                                       layer is an empty object)
    clients/<client>.json              per-client layer (required, >= 1)

Commands::

    python3 -m fleettools.policy --policy-dir DIR compile [--json]
    python3 -m fleettools.policy --policy-dir DIR snapshot --out FILE
    python3 -m fleettools.policy --policy-dir DIR drift --against FILE

``snapshot`` is the only command that writes, and only to the explicit
``--out`` path. ``drift`` prints human lines (or ``--json``) and never writes.
Exit codes: 0 ok, 1 policy/compile/drift error, 2 usage.
Drift findings (including snapshot hash anomalies) are non-gating report
output: exit 0 means the report ran, not that policy or snapshot is clean.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Optional

TOP_KEYS = {"role", "placement", "capabilities", "exceptions", "protected_paths"}
COMMON_KEYS = TOP_KEYS - {"role", "placement", "exceptions"}
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CLIENTS_DIR, ROLES_DIR, COMMON_FILE = "clients", "roles", "common.json"

# Strings that could hide glob semantics or smuggle control characters are
# refused rather than compared literally: a literal compare of "a*b" against
# "/a*b" would silently turn an intended wildcard into an exact leaf, and a
# newline inside an enrolled id would launder a second line of config into
# one field. Opaque means opaque.
_UNSAFE_CHARS_RX = re.compile(r"[\x00-\x1f\x7f]")
_GLOB_CHARS = set("*?[]")


class PolicySourceError(Exception):
    """A policy file is missing, unreadable, or violates its schema."""


class PolicyCompileError(Exception):
    """The layers are individually valid but cannot be merged fail-closed."""


# ---------------------------------------------------------------------------
# Path model: host-independent lexical component semantics.
#
# A policy string is matched by PATH MODEL — PurePosixPath for '/'-absolute
# strings, PureWindowsPath for drive/UNC/backslash strings — never by a
# filesystem: the same policy must compile identically on every OS, and a
# .resolve() here would silently bind meaning to whichever host happens to
# run the compiler. PureWindowsPath is case-insensitive and treats '/' and
# '\\' as equal, which is the documented, deterministic semantics of the
# paths it models; POSIX comparison stays case-sensitive. The original
# spelling is always preserved; the model is used only to decide overlap.
#
# The overlap (a.is_relative_to(b) or b.is_relative_to(a)) covers both escape
# directions: a grant of "/var/secrets/db" reaches into protected
# "/var/secrets", and a broad grant of "/var" covers it. Both are
# load-bearing on the same protected path and both require an exception.
# ---------------------------------------------------------------------------

def _windows_path_model(path: PureWindowsPath):
    if not path.is_absolute():
        return None
    components = path.parts[1:]
    if path.drive.startswith('\\\\'):
        components = tuple(path.drive[2:].split('\\')) + components
    for part in components:
        if part.endswith((' ', '.')) or any(c in part for c in ':<>|"'):
            return None
    for part in components:
        if re.fullmatch(r'(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])', part.split('.', 1)[0].rstrip(' '), re.IGNORECASE):
            return None
    return ('windows', path)


def _path_model(raw: str):
    """Parse a policy path string into a comparable model, or None if the
    string cannot be modeled without inventing semantics."""
    if not isinstance(raw, str) or not raw or _UNSAFE_CHARS_RX.search(raw):
        return None
    if any(c in _GLOB_CHARS for c in raw):
        return None
    s = raw.strip()
    if s != raw:
        return None
    if s.startswith("/") and not s.startswith("//"):
        if any(seg in (".", "..") for seg in s.split("/")):
            # Checked on the RAW segments: PurePosixPath silently normalizes
            # '.' away in .parts, so '/a/./b' would otherwise pass as '/a/b'.
            return None
        p = PurePosixPath(s)
        if not p.is_absolute():
            return None
        return ("posix", p)
    if s.startswith("/") or s.startswith("\\\\") or (len(s) >= 2 and
                                                     s[1] == ":"):
        if any(seg in (".", "..") for seg in s.replace("\\", "/").split("/")):
            return None
        p = PureWindowsPath(raw)
        return _windows_path_model(p)
    if "\\" in s:
        if any(seg in (".", "..") for seg in s.split("\\")):
            return None
        p = PureWindowsPath(raw)
        return _windows_path_model(p)
    return None


def _paths_overlap(a, b) -> bool:
    (ka, pa), (kb, pb) = a, b
    if ka != kb:
        return False
    return pa.is_relative_to(pb) or pb.is_relative_to(pa)


def _path_within(child, ancestor) -> bool:
    (kc, pc), (ka, pa) = child, ancestor
    if kc != ka:
        return False
    return pc.is_relative_to(pa)


def _describe_path(raw: str) -> str:
    return repr(raw)


def _validated_paths(where: str, key: str, values, problems: list) -> Optional[list]:
    """Validate a list of path strings; returns the VERBATIM strings."""
    if not isinstance(values, list):
        problems.append(f"{where}.{key}: must be a list of path strings")
        return None
    bad = []
    for p in values:
        if not isinstance(p, str) or _path_model(p) is None:
            bad.append(p)
    if bad:
        problems.append(
            f"{where}.{key}: not modelable policy path(s) "
            f"{', '.join(_describe_path(p) if isinstance(p, str) else type(p).__name__ for p in bad)} "
            f"— absolute POSIX ('/var/x') or Windows drive/UNC paths only; "
            f"relative paths, '.', '..', globs, padding/control characters "
            f"are refused rather than guessed at")
        return None
    return list(values)


def _identity_problems(where: str, key: str, value, problems: list) -> bool:
    """Shape-check an opaque enrolled id (host_identity / vm_identity).

    Non-blank string, no surrounding padding, no embedded whitespace, no
    control characters — and deliberately NOTHING else: enrolled ids are
    opaque and case-sensitive, never case-folded or normalized here.
    """
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where}.{key}: must be a non-empty string")
        return False
    if value != value.strip() or any(c.isspace() for c in value) or \
            _UNSAFE_CHARS_RX.search(value):
        problems.append(f"{where}.{key}: {value!r} must not contain "
                        f"padding, whitespace, or control characters")
        return False
    return True


def _check_placement(where: str, placement, problems: list) -> None:
    """Full client-placement shape validation at LOAD time, so every bad
    file is named in one pass instead of surfacing as a compile-time
    ValueError traceback."""
    if not isinstance(placement, dict) or not placement:
        problems.append(f"{where}.placement: must be a non-empty object with "
                        f"a 'host_identity'")
        return
    unknown = sorted(set(placement) - {"host_identity", "vm_identity"})
    if unknown:
        problems.append(f"{where}.placement: unknown key(s) "
                        f"{', '.join(unknown)} (allowed: host_identity, "
                        f"vm_identity)")
    _identity_problems(where, "placement.host_identity",
                       placement.get("host_identity"), problems)
    vm = placement.get("vm_identity")
    if vm is not None:
        _identity_problems(where, "placement.vm_identity", vm, problems)


# ---------------------------------------------------------------------------
# Typed effective-policy record: fields ARE the schema, validated at build.
# ---------------------------------------------------------------------------

@dataclass
class EffectivePolicy:
    """One client's merged policy — the only shape consumers ever see."""

    client: str
    role: str
    placement: dict
    capabilities: dict
    protected_paths: list
    exceptions_active: list = field(default_factory=list)
    exceptions_inactive: list = field(default_factory=list)

    def __post_init__(self):
        if not NAME_RX.match(self.client or ""):
            raise ValueError(f"client name {self.client!r} is not a valid id")
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("role must be a non-empty string")
        # Placement shape has ONE validator, shared with the loader and the
        # direct-sources pass, so the record's last-line guard can never
        # drift from what files are held to.
        problems: list = []
        _check_placement(f"client {self.client!r}", self.placement, problems)
        if problems:
            raise ValueError("; ".join(problems))
        if not isinstance(self.protected_paths, list) or \
                any(not isinstance(p, str) or not p for p in self.protected_paths):
            raise ValueError("protected_paths must be a list of non-empty strings")
        if not isinstance(self.capabilities, dict):
            raise ValueError("capabilities must be an object")

    def as_dict(self) -> dict:
        return {
            "client": self.client, "role": self.role,
            "placement": dict(self.placement),
            "capabilities": copy.deepcopy(self.capabilities),
            "protected_paths": list(self.protected_paths),
            "exceptions_active": copy.deepcopy(self.exceptions_active),
            "exceptions_inactive": copy.deepcopy(self.exceptions_inactive),
        }


# ---------------------------------------------------------------------------
# Loading: strict, fail-closed, every problem reported together.
# ---------------------------------------------------------------------------

def _validate_json_values(raw: Any, source: str) -> None:
    # json.loads accepts non-finite constants, overflowed exponents and escaped
    # lone surrogates; none can enter our canonical UTF-8 hash representation.
    try:
        canonical_json(raw).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PolicySourceError(
            f"{source}: invalid JSON value (non-finite number, unencodable string or unsupported nesting)") from exc


def _load_json_text(text: str, source: str) -> Any:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicySourceError(
            f"{source}: invalid JSON at line {exc.lineno}") from exc
    except (ValueError, RecursionError) as exc:
        raise PolicySourceError(
            f"{source}: JSON exceeds supported parser limits ({type(exc).__name__})") from exc
    _validate_json_values(raw, source)
    return raw


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PolicySourceError(
            f"{_rel(path)}: unreadable or not valid UTF-8 ({exc})") from exc
    return _load_json_text(text, _rel(path))


def _rel(path: Path, root: Optional[Path] = None) -> str:
    if root is not None:
        try:
            return str(path.relative_to(root))
        except ValueError:
            pass
    return path.name


def _check_layer(layer: Any, allowed: set, where: str,
                 problems: list, client_layer: bool = False) -> Optional[dict]:
    """Validate ONE layer; appends problems to `problems`, returns the layer
    when IT is well-formed (prior problems elsewhere do not disqualify it —
    every file gets validated and reported in the same run)."""
    local: list = []
    if not isinstance(layer, dict):
        problems.append(f"{where}: must be a JSON object")
        return None
    unknown = sorted(set(layer) - allowed)
    if unknown:
        problems.append(f"{where}: unknown key(s) {', '.join(unknown)} "
                        f"(allowed: {', '.join(sorted(allowed))})")
        local.append("unknown-keys")
    caps = layer.get("capabilities")
    if caps is not None and (not isinstance(caps, dict) or any(
            not isinstance(k, str) or not k
            for k in caps)):
        problems.append(f"{where}.capabilities: must map capability names to "
                        f"false, null, or {{\"paths\": [...]}}")
        local.append("capabilities")
    for name, v in ((caps or {}).items() if isinstance(caps, dict) else []):
        if v is None or v is False:
            continue
        if isinstance(v, dict) and set(v) == {"paths"}:
            # An EMPTY path list is refused at the source: as data it would
            # render `granted` while authorising nothing, and in the fold it
            # could silently narrow an upper layer's real paths to nothing.
            # `false` is the explicit revoke; `null` the explicit unconstrained.
            if not isinstance(v["paths"], list) or not v["paths"]:
                problems.append(f"{where}.capabilities[{name!r}]: an empty "
                                f"path list is refused — it would display as "
                                f"granted while authorising nothing; revoke "
                                f"with false or grant null (unconstrained)")
                local.append("capabilities")
                continue
            if _validated_paths(f"{where}.capabilities[{name!r}]", "paths",
                                v["paths"], problems) is None:
                local.append("capabilities")
            continue
        problems.append(f"{where}.capabilities[{name!r}]: grant must be "
                        f"false, null, or {{\"paths\": [modelable paths]}}")
        local.append("capabilities")
    pp = layer.get("protected_paths")
    if pp is not None and _validated_paths(f"{where}", "protected_paths",
                                           pp, problems) is None:
        local.append("protected_paths")
    if not client_layer:
        for key in ("role", "placement", "exceptions"):
            if key in layer:
                problems.append(f"{where}: '{key}' belongs only in a client "
                                f"layer, never in {where}")
                local.append(key)
    return None if local else layer


def _check_exceptions(where: str, layer: dict, problems: list) -> None:
    seen_ids = set()
    for i, e in enumerate(layer.get("exceptions") or []):
        label = f"{where}.exceptions[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{label}: must be an object")
            continue
        if isinstance(e.get('id'), str):
            if e['id'] in seen_ids:
                problems.append(f"{label}: duplicate exception id {e['id']!r}")
            seen_ids.add(e['id'])
        # `path` is REQUIRED: the compile-time window check must name the
        # exact protected path an exception covers, and a pathless record
        # cannot be matched to a grant it exempts.
        required = ("id", "capability", "path", "approved_by",
                    "approved_at", "expires_at")
        unknown = sorted(set(e) - set(required))
        if unknown:
            problems.append(f"{label}: unknown key(s) {', '.join(unknown)}")
        missing = [k for k in required if not e.get(k)]
        if missing:
            # The id is included in the missing-list message when present.
            problems.append(f"{label}{('(id=' + str(e.get('id')) + ')') if e.get('id') else ''}: "
                            f"missing {', '.join(missing)}")
            continue
        eid = f"(id={e['id']})"
        approved = _parse_ts(e["approved_at"], require_timezone=True)
        expires = _parse_ts(e["expires_at"], require_timezone=True)
        if approved is None:
            problems.append(f"{label}{eid}.approved_at: not an "
                            f"ISO-8601 timestamp with an explicit timezone")
        if expires is None:
            problems.append(f"{label}{eid}.expires_at: not an "
                            f"ISO-8601 timestamp with an explicit timezone")
        if approved and expires and expires <= approved:
            problems.append(f"{label}{eid}: expiry window is empty "
                            f"(expires_at <= approved_at)")
        if not isinstance(e["id"], str) or not NAME_RX.match(e["id"]):
            problems.append(f"{label}.id: must match {NAME_RX.pattern}")
        if not isinstance(e["capability"], str) or not e["capability"]:
            problems.append(f"{label}.capability: must be a non-empty string")
        if not isinstance(e["path"], str) or not e["path"].strip() or \
                _path_model(e["path"]) is None:
            problems.append(f"{label}.path: must be a modelable absolute "
                            f"policy path (relative, '.', '..', globs and "
                            f"padding/control characters are refused)")
        # The approver is a NAME, not a truthy value: True/1 are the same
        # typed hole as an empty string and are refused the same way.
        if not isinstance(e["approved_by"], str) or not e["approved_by"].strip():
            problems.append(f"{label}{eid}.approved_by: must be a "
                            f"non-empty string")


def _check_client_layer(name: str, layer: dict, roles: dict,
                        problems: list, where: str) -> None:
    """Client-layer-only validation: placement shape, role (typed, and
    present in `roles`), exceptions. Shared by the directory loader and
    compile_effective's direct-sources validation, so a caller constructing
    layers in code gets the same typed, contextual refusals as files do."""
    if "placement" not in layer:
        problems.append(f"{where}: missing 'placement' — a client's host "
                        f"identity is data and is never guessed")
    else:
        _check_placement(where, layer["placement"], problems)
    if "role" not in layer:
        problems.append(f"{where}: missing 'role'")
    else:
        role = layer["role"]
        if not isinstance(role, str) or not role:
            problems.append(f"{where}.role: must be a non-empty string")
        elif not NAME_RX.match(role):
            problems.append(f"{where}.role: {role!r} must match "
                            f"{NAME_RX.pattern} — it names the file "
                            f"roles/{role}.json")
        elif role not in roles:
            problems.append(f"{where}: role '{role}' has no "
                            f"roles/{role}.json — a role named by a client "
                            f"must have a layer file, even an empty one")
    excs = layer.get("exceptions") or []
    if not isinstance(excs, list):
        problems.append(f"{where}.exceptions: must be a list")
    else:
        _check_exceptions(where, layer, problems)


def load_sources(policy_dir) -> dict:
    """Read and validate every layer file, or raise PolicySourceError.

    All problems are collected and reported together: an operator fixing
    twelve client files should not discover them one run at a time.
    """
    root = Path(policy_dir)
    problems: list[str] = []
    if not root.is_dir():
        raise PolicySourceError(f"policy dir not found: {root}")

    common_path = root / COMMON_FILE
    if not common_path.is_file():
        raise PolicySourceError(f"{COMMON_FILE} is missing under {root} — the "
                                f"common layer is required")
    common = _check_layer(_read_json(common_path), COMMON_KEYS,
                          COMMON_FILE, problems) or {}

    roles: dict[str, dict] = {}
    roles_dir = root / ROLES_DIR
    if not roles_dir.is_dir():
        problems.append(f"{ROLES_DIR}/ is missing — every role a client "
                        f"names must have a layer file, even an empty one")
    else:
        for path in sorted(roles_dir.glob("*.json")):
            if not NAME_RX.match(path.stem):
                problems.append(f"{path.name}: role file name must match "
                                f"{NAME_RX.pattern}")
                continue
            layer = _check_layer(_read_json(path), TOP_KEYS,
                                 f"{ROLES_DIR}/{path.name}", problems)
            if layer is not None:
                roles[path.stem] = layer

    clients: dict[str, dict] = {}
    clients_dir = root / CLIENTS_DIR
    if not clients_dir.is_dir():
        problems.append(f"{CLIENTS_DIR}/ is missing — at least one client "
                        f"layer is required")
    else:
        for path in sorted(clients_dir.glob("*.json")):
            if not NAME_RX.match(path.stem):
                problems.append(f"{path.name}: client file name must match "
                                f"{NAME_RX.pattern}")
                continue
            layer = _check_layer(_read_json(path), TOP_KEYS,
                                 f"{CLIENTS_DIR}/{path.name}", problems,
                                 client_layer=True)
            if layer is None:
                continue
            clients[path.stem] = layer
            _check_client_layer(path.stem, layer, roles, problems,
                                f"{CLIENTS_DIR}/{path.name}")
        if not clients:
            problems.append(f"{CLIENTS_DIR}/ contains no client layers")

    if problems:
        raise PolicySourceError("\n".join(f"  - {p}" for p in problems))
    return {"common": common, "roles": roles, "clients": clients}


# ---------------------------------------------------------------------------
# The fold: per-key layer semantics — narrowing-only capabilities, growing
# protected_paths. role/placement/exceptions never merge across layers.
# ---------------------------------------------------------------------------

def _grow_protected(base: list, layer: dict) -> list:
    out = list(base)
    for p in (layer.get("protected_paths") or []):
        if not any(_path_within(_path_model(p), _path_model(q))
                   for q in out):
            out.append(p)
        # Only redundant subsets/aliases may be omitted; an ancestor
        # expands protection and must survive the fold.
    return out


def _parse_ts(value, *, require_timezone: bool = False) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        if require_timezone:
            return None
        # Historical snapshots retain their original UTC-by-definition meaning.
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _exception_window(e: dict, now: datetime) -> Optional[str]:
    """None when in effect, else the reason it is not."""
    approved, expires = _parse_ts(e["approved_at"]), _parse_ts(e["expires_at"])
    if approved and now < approved:
        return "not_yet_in_effect"
    if expires and now >= expires:
        return "expired"
    return None


def _apply_capabilities(eff: EffectivePolicy, layer: dict) -> None:
    """Fold one layer's capability grants under the per-key semantics.

    Narrowing-only: a constrained grant may be narrowed DOWNWARD by naming
    paths at or under the upper layer's paths. A set of paths that merely
    INTERSECTS the upper set is a widen (the covered-but-new remainder
    escapes the upper layer's restriction) and fails.
    """
    for name, grant in (layer.get("capabilities") or {}).items():
        if grant is False:
            eff.capabilities[name] = {"granted": False, "paths": None,
                                      "note": None}
            continue
        cur = eff.capabilities.get(name)
        if cur is not None and cur.get("granted") is False:
            raise PolicyCompileError(
                f"capability '{name}' is revoked for client "
                f"'{eff.client}'; a lower layer cannot revive it")
        if grant is None:
            if cur is not None and cur.get("paths") is not None:
                raise PolicyCompileError(
                    f"client '{eff.client}' would widen capability '{name}': "
                    f"an upper layer constrains it to "
                    f"{cur['paths']}; null (unconstrained) is a widen")
            eff.capabilities[name] = {"granted": True, "paths": None,
                                      "note": None}
            continue
        paths = list(grant["paths"])
        models = [_path_model(p) for p in paths]
        if cur is None or cur.get("paths") is None:
            # A first grant, or a legitimate REFINEMENT of an unconstrained
            # upper grant (narrowing null to a concrete set is the feature,
            # never an error).
            eff.capabilities[name] = {"granted": True, "paths": paths,
                                      "note": None}
            continue
        upper = [_path_model(p) for p in cur["paths"]]
        covered = {i for i, m in enumerate(models)
                   if any(_path_within(m, u) for u in upper)}
        if len(covered) != len(paths):
            extra = [p for i, p in enumerate(paths) if i not in covered]
            raise PolicyCompileError(
                f"client '{eff.client}' would widen capability '{name}' "
                f"with path(s) {extra} not contained by the upper layer's "
                f"{cur['paths']} — an intersecting-but-new path escapes the "
                f"upper restriction and is a widen, not a narrow")
        # Aliased spellings of the same modeled path collapse so the record
        # cannot grow duplicates; the UPPER layer's spelling is kept (the
        # lower layer narrows INTO the upper's set, so the upper's spelling
        # is the established one).
        kept: list = []
        for i, m in enumerate(models):
            path = next((p for p, u in zip(cur["paths"], upper) if u == m), paths[i])
            if not any(m == _path_model(k) for k in kept):
                kept.append(path)
        eff.capabilities[name] = {"granted": True, "paths": kept,
                                  "note": None}


def _check_protected_grants(eff: EffectivePolicy, exceptions: list,
                            now: datetime, problems: list) -> None:
    """Every protected path a grant REACHES needs an in-window exception.

    Reach is bidirectional containment: a descendant grant inside the
    protected subtree, an exact grant of it, AND a broad ancestor grant
    covering it are all load-bearing on the protected path and fail without
    a covering exception. Overlap is decided on the path model, so
    '/var/secrets/db', 'C:/Secrets/db' and 'c:\\secrets\\db' reach
    '/var/secrets' and 'C:/Secrets' respectively; an unrelated sibling
    ('/var/secrets-other') reaches nothing.

    The reached REGION needs an in-window exception whose own path CONTAINS
    the whole region (the deeper of the grant path and the protected path):
    an exception scoped to a leaf does not bless a broader grant that
    reaches more of the protected subtree, and a broad exception does not
    bless a grant whose other paths reach unprotected territory — those
    other paths are simply checked on their own.
    """
    for cap, grant in eff.capabilities.items():
        if not grant.get("granted"):
            continue
        named = grant.get("paths")
        if named is None:
            # An unconstrained grant means everything EXCEPT protected
            # paths; nothing is reached, nothing to check.
            continue
        for p in named:
            pm = _path_model(p)
            for q in eff.protected_paths:
                qm = _path_model(q)
                if not _paths_overlap(pm, qm):
                    continue
                reached = pm if _path_within(pm, qm) else qm
                covers = [e for e in exceptions
                          if e.get("capability") == cap
                          and _exception_window(e, now) is None
                          and _path_within(reached,
                                           _path_model(e["path"]))]
                if covers:
                    for e in covers:
                        if not any(a.get("id") == e.get("id")
                                   for a in eff.exceptions_active):
                            eff.exceptions_active.append(dict(e))
                    continue
                reasons = []
                for e in exceptions:
                    if e.get("capability") != cap or not _path_within(
                            reached, _path_model(e["path"])):
                        continue
                    status = _exception_window(e, now)
                    if status:
                        reasons.append(f"{e['id']} ({status})")
                problems.append(
                    f"client '{eff.client}': capability '{cap}' grant "
                    f"'{p}' reaches protected path '{q}' with no in-window "
                    f"exception covering it"
                    + (f" — out of window: {', '.join(reasons)}"
                       if reasons else ""))

    for e in exceptions:
        if any(a.get("id") == e.get("id") for a in eff.exceptions_active):
            continue
        status = _exception_window(e, now) or "unused"
        # Every declared exception is recorded: expired/not-yet-in-effect as
        # such, and an in-window one no granted path reaches as "unused" —
        # nothing vanishes from the compiled record or its snapshot.
        eff.exceptions_inactive.append(
            {"id": e["id"], "capability": e["capability"], "status": status,
             "expires_at": e["expires_at"]})


def compile_one(name: str, sources: dict, now: datetime) -> EffectivePolicy:
    """Fold common -> role -> client for one client into its typed record."""
    client_layer = sources["clients"][name]
    role = client_layer["role"]
    if role not in sources["roles"]:
        raise PolicyCompileError(
            f"client '{name}' has role '{role}' but roles/{role}.json "
            f"does not exist")
    # Per-key fold in layer order. role/placement/exceptions are
    # client-layer-only and never merge across layers; capabilities narrow
    # downward and protected_paths only grow, both decided on the path model.
    eff = EffectivePolicy(
        client=name, role=role,
        placement=copy.deepcopy(client_layer["placement"]),
        capabilities={}, protected_paths=[])
    for layer in (sources["common"], sources["roles"][role], client_layer):
        eff.protected_paths = _grow_protected(eff.protected_paths, layer)
        _apply_capabilities(eff, layer)
    problems: list[str] = []
    _check_protected_grants(eff, client_layer.get("exceptions") or [], now,
                            problems)
    if problems:
        raise PolicyCompileError("\n".join(f"  - {p}" for p in problems))
    return eff


def _validate_sources(sources: dict) -> None:
    """compile_effective's fail-closed validation of DIRECT sources.

    A caller constructing layers in code (rather than through load_sources)
    gets the same typed, contextual refusals — unknown/missing keys, path
    model, placement shape — instead of raw errors deep in the fold.
    """
    problems: list[str] = []
    if not isinstance(sources, dict):
        raise PolicySourceError("sources must be a dict with 'common', "
                                "'roles' and 'clients'")
    _validate_json_values(sources, "sources")
    common = sources.get("common")
    if not isinstance(common, dict):
        problems.append("common: must be a dict layer")
    else:
        _check_layer(common, COMMON_KEYS, "common", problems)
    roles = sources.get("roles")
    if not isinstance(roles, dict):
        problems.append("roles: must be a dict of role name -> layer")
        roles = {}
    else:
        for rname, rlayer in roles.items():
            if not isinstance(rname, str) or not NAME_RX.match(rname):
                problems.append(f"roles[{rname!r}]: role name must match "
                                f"{NAME_RX.pattern}")
            if not isinstance(rlayer, dict):
                problems.append(f"roles[{rname!r}]: must be a dict layer")
                continue
            _check_layer(rlayer, TOP_KEYS, f"roles[{rname!r}]", problems)
    clients = sources.get("clients")
    if not isinstance(clients, dict) or not clients:
        problems.append("clients: must be a non-empty dict of client name "
                        "-> layer")
    else:
        for cname, clayer in clients.items():
            if not isinstance(cname, str) or not NAME_RX.match(cname):
                problems.append(f"clients[{cname!r}]: client name must match "
                                f"{NAME_RX.pattern}")
            if not isinstance(clayer, dict):
                problems.append(f"clients[{cname!r}]: must be a dict layer")
                continue
            _check_layer(clayer, TOP_KEYS, f"clients[{cname!r}]", problems,
                         client_layer=True)
            _check_client_layer(cname, clayer, roles, problems,
                                f"clients[{cname!r}]")
    if problems:
        raise PolicySourceError("\n".join(f"  - {p}" for p in problems))


def compile_effective(sources: dict, now: datetime) -> dict:
    """Compile every client; report EVERY failure, never just the first."""
    _validate_sources(sources)
    results: dict[str, EffectivePolicy] = {}
    failures: list[str] = []
    for name in sorted(sources["clients"]):
        try:
            results[name] = compile_one(name, sources, now)
        except PolicyCompileError as exc:
            failures.append(f"client '{name}':\n{exc}")
    if failures:
        raise PolicyCompileError(
            "compilation failed:\n" + "\n".join(failures))

    # Colocation: one physical host may carry many clients ONLY when every
    # client on it declares its own isolated VM identity. Enforced globally,
    # after per-client compilation, because it is a property of the set.
    by_host: dict[str, list] = {}
    for eff in results.values():
        by_host.setdefault(eff.placement["host_identity"], []).append(eff)
    for host, group in sorted(by_host.items()):
        if len(group) == 1:
            continue
        vms = {e.placement.get("vm_identity") for e in group}
        if any(vm is None for vm in vms):
            names = ", ".join(sorted(e.client for e in group))
            bare = sorted(e.client for e in group
                          if e.placement.get("vm_identity") is None)
            raise PolicyCompileError(
                f"  - host '{host}' would host multiple clients ({names}) "
                f"without explicit isolated VM identities (missing on: "
                f"{', '.join(bare)}); colocation requires each client to "
                f"declare its own vm_identity")
        seen: dict[str, str] = {}
        for e in sorted(group, key=lambda x: x.client):
            vm = e.placement["vm_identity"]
            if vm in seen:
                raise PolicyCompileError(
                    f"  - host '{host}': clients '{seen[vm]}' and "
                    f"'{e.client}' declare the same vm_identity '{vm}'")
            seen[vm] = e.client
    return results


# ---------------------------------------------------------------------------
# Canonical form, hash, drift.
# ---------------------------------------------------------------------------

def canonical_json(obj) -> str:
    """The one serialisation hashes and snapshots are built on: sorted keys,
    no whitespace, floats via repr so the bytes are stable across runs."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def policy_hash(policy: dict) -> str:
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def compile_all(policy_dir, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return compile_effective(load_sources(policy_dir), now=now)


def _uncovered_protected_paths(paths: list, covering: list) -> list:
    models = [_path_model(p) for p in covering]
    uncovered = []
    for path in paths:
        model = _path_model(path)
        if path in covering or (model is not None and any(
                other is not None and _path_within(model, other) for other in models)):
            continue
        uncovered.append(path)
    return sorted(set(uncovered))


def drift_report(old: dict, new: dict, now: Optional[datetime] = None) -> list:
    """Human-auditable differences between two compiled policy maps.

    Report-only by contract: callers print or store the result; nothing here
    writes, applies, or reverts anything. Old maps come from snapshots, so
    their integrity is checked first by the caller — a snapshot whose stored
    hash no longer matches its content is reported, not silently compared.

    An exception present in `old` but gone from `new` is `exception_expired`
    ONLY when its window has actually closed against `now`; a still-valid
    exception the operator deleted is `exception_removed`. Labelling the
    removal by `expires_at` truthiness would report a false cause.
    """
    now = now or datetime.now(timezone.utc)
    entries: list = []
    for client in sorted(set(old) - set(new)):
        entries.append({"client": client, "kind": "client_removed",
                        "detail": "no client layer compiles for it now"})
    for client in sorted(set(new) - set(old)):
        entries.append({"client": client, "kind": "client_added",
                        "detail": "new effective policy"})
    for client in sorted(set(old) & set(new)):
        o, n = old[client], new[client]
        if policy_hash(o) != policy_hash(n):
            entries.append({"client": client, "kind": "hash_changed",
                            "detail": f"{policy_hash(o)[:12]} -> "
                                      f"{policy_hash(n)[:12]}"})
        removed = _uncovered_protected_paths(o.get('protected_paths') or [], n.get('protected_paths') or [])
        added = _uncovered_protected_paths(n.get('protected_paths') or [], o.get('protected_paths') or [])
        if removed:
            entries.append({"client": client, "kind": "protected_path_removed",
                            "detail": ", ".join(removed)})
        if added:
            entries.append({"client": client, "kind": "protected_path_added",
                            "detail": ", ".join(added)})
        if o.get("role") != n.get("role"):
            entries.append({"client": client, "kind": "role_changed",
                            "detail": f"{o.get('role')} -> {n.get('role')}"})
        if o.get("placement") != n.get("placement"):
            entries.append({"client": client, "kind": "placement_changed",
                            "detail": f"{o.get('placement')} -> "
                                      f"{n.get('placement')}"})
        oc, nc = o.get("capabilities") or {}, n.get("capabilities") or {}
        for cap in sorted(set(oc) | set(nc)):
            was, now_ = oc.get(cap), nc.get(cap)
            if (was and was.get("granted")) and not (now_ and now_.get("granted")):
                entries.append({"client": client, "kind": "capability_revoked",
                                "detail": cap})
            elif was != now_ and (now_ and now_.get("granted")) and \
                    not (was and was.get("granted")):
                entries.append({"client": client, "kind": "capability_granted",
                                "detail": cap})
            elif (was and now_ and was.get("granted") and now_.get("granted")
                  and was.get("paths") != now_.get("paths")):
                entries.append({"client": client,
                                "kind": "capability_paths_changed",
                                "detail": f"{cap}: {was.get('paths')} -> "
                                          f"{now_.get('paths')}"})
        oe = {e.get("id"): e for e in (o.get("exceptions_active") or [])}
        ne = {e.get("id"): e for e in (n.get("exceptions_active") or [])}
        inactive_ids = {e.get('id') for e in (n.get('exceptions_inactive') or [])}
        for eid in sorted(set(oe) - set(ne)):
            gone = oe[eid]
            expires = _parse_ts(gone.get("expires_at"))
            kind = "exception_expired" if (expires is not None and
                                           now >= expires) \
                else ('exception_no_longer_active' if eid in inactive_ids else 'exception_removed')
            entries.append({"client": client, "kind": kind, "detail": eid})
        for eid in sorted(set(ne) - set(oe)):
            entries.append({"client": client, "kind": "exception_added",
                            "detail": eid})
    return entries


# ---------------------------------------------------------------------------
# Snapshot I/O (the only writing path in this module).
# ---------------------------------------------------------------------------

SNAPSHOT_FORMAT = 1


def write_snapshot(policy_dir, out_path, now: Optional[datetime] = None) -> dict:
    compiled = compile_all(policy_dir, now)
    policies = {name: eff.as_dict() for name, eff in compiled.items()}
    snap = {
        "tool": "fleettools.policy",
        "format": SNAPSHOT_FORMAT,
        "compiled_at": (now or datetime.now(timezone.utc)).isoformat(),
        "hashes": {name: policy_hash(p) for name, p in policies.items()},
        "policies": policies,
    }
    path = Path(out_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name,
                                   suffix=".tmp")
    except OSError as exc:
        raise PolicySourceError(
            f"snapshot {path.name}: publication refused ({exc})") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(canonical_json(snap))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise PolicySourceError(
                f"snapshot {path.name}: publication refused ({exc})") from exc
        raise
    return snap


_SNAPSHOT_POLICY_KEYS = ("client", "role", "placement", "capabilities",
                         "protected_paths", "exceptions_active",
                         "exceptions_inactive")


def _check_stored_grants(client: str, capabilities: dict) -> None:
    for cap, grant in capabilities.items():
        label = f"snapshot policies[{client!r}].capabilities[{cap!r}]"
        if not isinstance(grant, dict) or not isinstance(grant.get("granted"), bool):
            raise PolicySourceError(f"{label}: must be an object with boolean granted")
        paths = grant.get("paths")
        if "paths" not in grant or (paths is not None and (
                not isinstance(paths, list) or any(not isinstance(p, str) for p in paths))):
            raise PolicySourceError(f"{label}.paths: must be null or a list of strings")


def _check_stored_policy(client: str, policy) -> None:
    """A stored policy must be a dict with the canonical record shape: the
    snapshot is the tamper-detector's input, so a structurally broken one is
    refused outright instead of silently compared (or crashed on)."""
    if not isinstance(policy, dict):
        raise PolicySourceError(
            f"snapshot is malformed: policies[{client!r}] must be an object, "
            f"got {type(policy).__name__}")
    missing = [k for k in _SNAPSHOT_POLICY_KEYS if k not in policy]
    if missing:
        raise PolicySourceError(
            f"snapshot is malformed: policies[{client!r}] is missing "
            f"{', '.join(missing)}")
    if not isinstance(policy["placement"], dict) or \
            not isinstance(policy["capabilities"], dict) or \
            not isinstance(policy["protected_paths"], list) or \
            not isinstance(policy["exceptions_active"], list) or \
            not isinstance(policy["exceptions_inactive"], list):
        raise PolicySourceError(
            f"snapshot is malformed: policies[{client!r}] has a field of "
            f"the wrong type (placement/capabilities must be objects, "
            f"protected_paths/exceptions_* must be lists)")
    if any(not isinstance(path, str) for path in policy["protected_paths"]):
        raise PolicySourceError(
            f"snapshot policies[{client!r}].protected_paths: must contain strings")
    _check_stored_grants(client, policy["capabilities"])
    for field in ("exceptions_active", "exceptions_inactive"):
        for row in policy[field]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise PolicySourceError(
                    f"snapshot policies[{client!r}].{field}: each entry requires a string id")


def load_snapshot(path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PolicySourceError(
            f"snapshot {Path(path).name}: unreadable or not valid UTF-8 ({exc})") from exc
    raw = _load_json_text(text, f"snapshot {Path(path).name}")
    if not isinstance(raw, dict) or not isinstance(raw.get("policies"), dict):
        raise PolicySourceError("snapshot is malformed: 'policies' must be "
                                "an object of client -> policy")
    hashes = raw.get("hashes")
    if not isinstance(hashes, dict) or any(
            not isinstance(v, str) for v in hashes.values()):
        raise PolicySourceError("snapshot is malformed: 'hashes' must be an "
                                "object of client -> hash string")
    for client, policy in raw["policies"].items():
        _check_stored_policy(client, policy)
    return raw


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="fleettools.policy",
        description="Compile effective business-client policies (read-only, "
                    "deterministic; snapshot is the only writing command).")
    ap.add_argument("--policy-dir", required=True)
    ap.add_argument("--now", help="ISO timestamp the compile window is "
                                  "evaluated against (default: now)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_compile = sub.add_parser("compile", help="compile and print, write nothing")
    p_compile.add_argument("--json", action="store_true")
    p_snap = sub.add_parser("snapshot", help="compile and store a snapshot")
    p_snap.add_argument("--out", required=True)
    p_drift = sub.add_parser("drift", help="compare against a stored snapshot")
    p_drift.add_argument("--against", required=True)
    p_drift.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError:
            print(f"fleettools.policy: --now is not an ISO-8601 timestamp: "
                  f"{args.now}", file=sys.stderr)
            return 2
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

    try:
        if args.cmd == "compile":
            compiled = compile_all(args.policy_dir, now)
            policies = {n: e.as_dict() for n, e in compiled.items()}
            if args.json:
                print(json.dumps({"hashes": {n: policy_hash(p) for n, p in
                                             policies.items()},
                                  "policies": policies}, indent=2))
            else:
                for n, p in policies.items():
                    print(f"{n}: role={p['role']} "
                          f"host={p['placement']['host_identity']} "
                          f"hash={policy_hash(p)[:12]}")
                    for cap, g in sorted(p["capabilities"].items()):
                        state = ("granted" if g["granted"] else "revoked")
                        paths = (f" paths={g['paths']}"
                                 if g["paths"] is not None else "")
                        print(f"    {cap}: {state}{paths}")
                    for e in p["exceptions_active"]:
                        print(f"    exception {e['id']} active until "
                              f"{e['expires_at']}")
                    for e in p["exceptions_inactive"]:
                        print(f"    exception {e['id']} inactive "
                              f"({e['status']})")
            return 0
        if args.cmd == "snapshot":
            snap = write_snapshot(args.policy_dir, args.out, now)
            print(f"snapshot written: {args.out} "
                  f"({len(snap['policies'])} client(s))")
            return 0
        # drift
        snap = load_snapshot(args.against)
        compiled = compile_all(args.policy_dir, now)
        policies = {n: e.as_dict() for n, e in compiled.items()}
        entries = drift_report(snap["policies"], policies, now=now)
        # Integrity first, over EVERY stored entry: a hash entry that is
        # missing or no longer matches its stored policy is drift the
        # operator must see — also for clients that no longer compile, or
        # the snapshot's tamper detector would silently stop covering them.
        for client in sorted(snap["policies"]):
            stored_hash = snap["hashes"].get(client)
            if not isinstance(stored_hash, str) or not stored_hash:
                entries.append(
                    {"client": client, "kind": "snapshot_hash_missing",
                     "detail": "no recorded hash for this stored policy — "
                               "its integrity cannot be verified"})
                continue
            if policy_hash(snap["policies"][client]) != stored_hash:
                entries.append(
                    {"client": client, "kind": "snapshot_hash_mismatch",
                     "detail": "stored policy no longer matches its "
                               "recorded hash — the snapshot was edited "
                               "after it was written"})
        # hash strings with no policy behind them are equally broken data.
        for client in sorted(set(snap["hashes"]) - set(snap["policies"])):
            entries.append({"client": client, "kind": "snapshot_orphan_hash",
                            "detail": "a hash is recorded for this client "
                                      "but no policy is stored"})
        entries.sort(key=lambda e: (e["client"], e["kind"]))
        if args.json:
            print(json.dumps(entries, indent=2))
        elif not entries:
            print(f"no drift: {len(policies)} client policy/policies match "
                  f"the snapshot")
        else:
            for e in entries:
                print(f"{e['client']}: {e['kind']} — {e['detail']}")
            print(f"\n{len(entries)} drift entr(y/ies) vs "
                  f"{args.against} (report only — nothing was changed)")
        return 0
    except (PolicySourceError, PolicyCompileError) as exc:
        print(f"fleettools.policy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
