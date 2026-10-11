"""Test helpers shared by the Stage B test files."""
from __future__ import annotations

import json


def write_policy(root, common=None, roles=None, clients=None, with_common=True,
                 auto_role=True):
    """Lay out a policy dir. A client role with no explicit layer gets an
    empty role file (auto_role), so each test states only what it exercises.
    auto_role=False leaves the named role's file ABSENT, for tests of the
    explicit required-role contract."""
    roles = dict(roles or {})
    (root / "roles").mkdir(parents=True, exist_ok=True)
    (root / "clients").mkdir(parents=True, exist_ok=True)
    if with_common:
        (root / "common.json").write_text(
            json.dumps(common if common is not None else {}), encoding="utf-8")
    if auto_role:
        for body in (clients or {}).values():
            role = body.get("role")
            if role and role not in roles:
                roles[role] = {}
    for name, body in roles.items():
        (root / "roles" / f"{name}.json").write_text(json.dumps(body),
                                                     encoding="utf-8")
    for name, body in (clients or {}).items():
        (root / "clients" / f"{name}.json").write_text(json.dumps(body),
                                                       encoding="utf-8")
    return root


def client_body(role="worker", host="enr-host-1", caps=None, exceptions=None,
                placement=None):
    body = {"role": role, "placement": placement or {"host_identity": host}}
    if caps is not None:
        body["capabilities"] = caps
    if exceptions is not None:
        body["exceptions"] = exceptions
    return body
