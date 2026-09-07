"""Shared fixtures.

The env vars here are the ones projects/orders/project.yaml interpolates. They are fake
by construction — no test in this suite opens a network connection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROJECT = REPO_ROOT / "projects" / "orders"

_FAKE_ENV = {
    "DPHM_RDS_HOST": "rds.test.internal",
    "DPHM_RDS_USER": "dphm_reader",
    "DPHM_RDS_PASSWORD": "not-a-real-password",
    "DPHM_SF_ACCOUNT": "acme-test",
    "DPHM_SF_USER": "DPHM_SVC",
    "DPHM_SF_KEY_PATH": "/run/secrets/sf_key.p8",
    "DPHM_GIT_TOKEN": "not-a-real-token",
}


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in _FAKE_ENV.items():
        monkeypatch.setenv(key, value)
    # Never let a stray dev-auth or production flag leak between tests.
    for key in ("DPHM_DEV_AUTH", "DPHM_ENV", "DPHM_ROLE_MAP", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def example_project_dir() -> Path:
    return EXAMPLE_PROJECT


@pytest.fixture
def loaded_project():
    from dphm.config.loader import load_project

    return load_project(EXAMPLE_PROJECT)


@pytest.fixture
def write_project(tmp_path: Path):
    """Copy the example project so a test can mutate one file and assert the failure."""
    import shutil
    from itertools import count

    counter = count()

    def _write(**overrides: str) -> Path:
        # A fresh directory per call, so one test may build two variants and compare.
        dest = tmp_path / f"proj{next(counter)}"
        shutil.copytree(EXAMPLE_PROJECT, dest)
        for filename, content in overrides.items():
            (dest / filename).write_text(content)
        return dest

    return _write
