"""Per-project secret storage.

The specification says secrets come only from env vars backed by a platform secret
manager. That is right for a deployed service, and it is also a chicken-and-egg problem
for an onboarding wizard: someone has to type the credentials in before anything can
read them from anywhere.

So this is the seam. Credentials typed into the wizard land in a per-project file OUTSIDE
the repo at mode 600, and `project.yaml` references them as `${VAR}` exactly as before.
Nothing about the config format changes, and swapping this module for AWS Secrets Manager
or Vault is a single-file change — which is the whole point of putting it behind a seam
rather than reading `os.environ` directly everywhere.

What this is NOT: encryption at rest. A file readable only by the service account is the
honest limit of what a local store can offer, and it is recorded here rather than
implied.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from dphm.util.logging import get_logger

log = get_logger(__name__)

_VALID_NAME = re.compile(r"^[a-z0-9_\-]+$")
_LINE = re.compile(r"^(?P<key>[A-Z_][A-Z0-9_]*)=(?P<value>.*)$")


class SecretStoreError(RuntimeError):
    """The store could not be read or written."""


def store_root() -> Path:
    return Path(os.environ.get("DPHM_SECRET_DIR", str(Path.home() / ".dphm" / "secrets")))


def _path_for(project: str) -> Path:
    if not _VALID_NAME.match(project):
        raise SecretStoreError(
            f"invalid project name {project!r}: lower-case letters, digits, _ and - only. "
            "The name becomes a filename, so it is validated rather than sanitised."
        )
    return store_root() / f"{project}.env"


def write(project: str, values: dict[str, str]) -> Path:
    """Persist a project's secrets at mode 600, creating the directory at 700."""
    path = _path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(stat.S_IRWXU)

    lines = [
        "# dphm secrets. Written by the onboarding wizard.",
        "# Mode 600, outside the repo, and never committed.",
        f"# project: {project}",
        "",
    ]
    for key in sorted(values):
        value = values[key]
        if "\n" in value:
            raise SecretStoreError(f"secret {key} contains a newline and cannot be stored")
        lines.append(f"{key}={value}")

    # Create with restrictive permissions BEFORE writing, so the content is never
    # briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    log.info("secrets_written", project=project, keys=sorted(values), path=str(path))
    return path


def read(project: str) -> dict[str, str]:
    """A project's secrets, or {} if none are stored. A wrong mode is a warning."""
    path = _path_for(project)
    if not path.exists():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning("secret_file_too_permissive", path=str(path), mode=oct(mode))
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match:
            out[match.group("key")] = match.group("value")
    return out


def delete(project: str) -> bool:
    path = _path_for(project)
    if path.exists():
        path.unlink()
        log.info("secrets_deleted", project=project)
        return True
    return False


def resolution_mapping(project: str) -> dict[str, str]:
    """Values for `${VAR}` interpolation: the project's secrets, then the environment.

    The environment wins, so a deployed service using a real secret manager overrides the
    local store without any config change.
    """
    merged = dict(read(project))
    merged.update(os.environ)
    return merged
