"""Project registry — the loaded `projects/` directory (12 §8).

Config arrives as a mounted directory read from git, plus env vars. It is loaded once at
startup and validated then, so an invalid config fails the service rather than surfacing
as a confusing runtime error on the first scheduled run.
"""

from __future__ import annotations

import os
from pathlib import Path

from dphm.config.loader import ConfigError, LoadedProject, load_project
from dphm.util.logging import get_logger

log = get_logger(__name__)

_REGISTRY: dict[str, LoadedProject] = {}
_LOAD_ERRORS: dict[str, str] = {}
# Projects whose config is VALID but whose warehouse could not be reached at startup.
# Kept separate from _LOAD_ERRORS deliberately: a bad config and an unreachable warehouse
# need different fixes, and collapsing them sends the reader to the wrong place.
_UNREACHABLE: dict[str, str] = {}


def projects_root() -> Path:
    return Path(os.environ.get("DPHM_PROJECTS_DIR", "projects"))


def load_all(root: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """Load every project directory. Returns (loaded names, {name: error}).

    A single bad project does not stop the others from loading, but its error is kept and
    surfaced — a project that silently vanished from the dashboard is worse than one
    shown with a red banner.
    """
    root = root or projects_root()
    _REGISTRY.clear()
    _LOAD_ERRORS.clear()
    _UNREACHABLE.clear()
    if not root.is_dir():
        log.warning("projects_dir_missing", path=str(root))
        return [], {}

    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            loaded = load_project(child)
        except ConfigError as exc:
            _LOAD_ERRORS[child.name] = str(exc)
            log.error("project_config_invalid", project=child.name, error=str(exc))
            continue
        _REGISTRY[loaded.name] = loaded
        log.info(
            "project_loaded",
            project=loaded.name,
            tables=len(loaded.manifest.tables),
            hops=len(loaded.layers.hops),
            checks=len(loaded.checks),
            config_sha=loaded.config_sha,
        )
    return sorted(_REGISTRY), dict(_LOAD_ERRORS)


def get_project(name: str) -> LoadedProject | None:
    return _REGISTRY.get(name)


def all_projects() -> list[LoadedProject]:
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def load_errors() -> dict[str, str]:
    return dict(_LOAD_ERRORS)


def record_unreachable(name: str, error: str) -> None:
    _UNREACHABLE[name] = error


def unreachable() -> dict[str, str]:
    """Valid config, unreachable warehouse. Surfaced, never silently dropped."""
    return dict(_UNREACHABLE)
