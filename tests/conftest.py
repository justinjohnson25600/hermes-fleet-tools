"""Keep main() off the host: the deploy tests must never read or fetch a real skills repo."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_skills_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_SKILLS_REPO", str(tmp_path / "no-skills-repo"))
