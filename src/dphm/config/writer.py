"""Write project config from the onboarding wizard.

The specification is explicit: "nobody hand-edits these files in v1 — the onboarding
wizard collects project.yaml." This module is that. The YAML stays the durable, reviewable
artifact; a form fills it in.

Credentials never reach the YAML. They go to the per-project secret store, and the file
holds `${VAR}` references — so the load-time scanner that refuses credential literals
keeps working exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 - used at runtime, not only in annotations
from typing import Any

import yaml

from dphm.config import secrets_store
from dphm.util.logging import get_logger

log = get_logger(__name__)

_VALID_PROJECT = re.compile(r"^[a-z0-9_\-]+$")


class ProjectWriteError(RuntimeError):
    """The project could not be written."""


@dataclass
class SnowflakeInput:
    """What the wizard collects for Snowflake."""

    account: str
    user: str
    private_key_path: str
    database: str
    schemas: list[str]
    warehouse: str = "DPHM_WH"
    role_reader: str = "DPHM_READER"
    role_writer: str = "DPHM_WRITER"
    state_schema: str = "DPHM_STATE"
    scratch_schema: str = "DPHM_SCRATCH"


@dataclass
class PostgresInput:
    """What the wizard collects for the source database. Optional."""

    host: str
    database: str
    user: str
    password: str
    schemas: list[str] = field(default_factory=lambda: ["public"])
    port: int = 5432


@dataclass
class ProjectInput:
    name: str
    team: str
    slack_channel: str
    shadow_channel: str
    snowflake: SnowflakeInput
    postgres: PostgresInput | None = None
    repo_url: str = ""
    repo_branch: str = "main"
    batch_strategy: str = "hook_only"
    batch_control_table: str | None = None


def _env_key(project: str, suffix: str) -> str:
    return f"DPHM_{project.upper().replace('-', '_')}_{suffix}"


def write_project(root: Path, spec: ProjectInput, *, overwrite: bool = False) -> Path:
    """Write `projects/<name>/` and the project's secrets. Returns the directory."""
    if not _VALID_PROJECT.match(spec.name):
        raise ProjectWriteError(
            f"invalid project name {spec.name!r}: lower-case letters, digits, _ and - only"
        )
    directory = root / spec.name
    if directory.exists() and not overwrite:
        raise ProjectWriteError(
            f"project {spec.name!r} already exists at {directory}. Pass overwrite to replace it."
        )
    directory.mkdir(parents=True, exist_ok=True)

    # ── secrets out of the YAML and into the store ──
    sf_key = _env_key(spec.name, "SF_ACCOUNT")
    sf_user_key = _env_key(spec.name, "SF_USER")
    sf_path_key = _env_key(spec.name, "SF_KEY_PATH")
    values = {
        sf_key: spec.snowflake.account,
        sf_user_key: spec.snowflake.user,
        sf_path_key: spec.snowflake.private_key_path,
    }
    source_block: dict[str, Any]
    if spec.postgres:
        pg_host = _env_key(spec.name, "RDS_HOST")
        pg_user = _env_key(spec.name, "RDS_USER")
        pg_pass = _env_key(spec.name, "RDS_PASSWORD")
        values |= {
            pg_host: spec.postgres.host,
            pg_user: spec.postgres.user,
            pg_pass: spec.postgres.password,
        }
        source_block = {
            "kind": "postgres",
            "host": f"${{{pg_host}}}",
            "port": spec.postgres.port,
            "database": spec.postgres.database,
            "user": f"${{{pg_user}}}",
            "password": f"${{{pg_pass}}}",
            "schemas": spec.postgres.schemas,
            "read_only": True,
            "statement_timeout_s": 120,
            "max_rows_per_query": 5_000_000,
        }
    else:
        # No source yet. A placeholder keeps the config valid and the diagnostics panel
        # reports the source as unreachable, which is the honest state — rather than
        # pretending Lane B is available.
        placeholder = _env_key(spec.name, "RDS_HOST")
        values[placeholder] = ""
        source_block = {
            "kind": "postgres",
            "host": f"${{{placeholder}}}",
            "port": 5432,
            "database": "not_configured",
            "user": f"${{{_env_key(spec.name, 'RDS_USER')}}}",
            "password": f"${{{_env_key(spec.name, 'RDS_PASSWORD')}}}",
            "schemas": ["public"],
            "read_only": True,
            "statement_timeout_s": 120,
            "max_rows_per_query": 5_000_000,
        }
        values.setdefault(_env_key(spec.name, "RDS_USER"), "")
        values.setdefault(_env_key(spec.name, "RDS_PASSWORD"), "")

    secrets_store.write(spec.name, values)

    batch: dict[str, Any] = {"strategy": spec.batch_strategy, "lookback_hours": 48}
    if spec.batch_strategy == "control_table" and spec.batch_control_table:
        batch |= {
            "control_table": spec.batch_control_table,
            "batch_id_column": "BATCH_ID",
            "status_column": "STATUS",
            "completed_value": "SUCCESS",
            "completed_at_column": "COMPLETED_AT",
        }

    project_yaml: dict[str, Any] = {
        "project": spec.name,
        "owner": {
            "team": spec.team,
            "slack_channel": spec.slack_channel,
            "shadow_channel": spec.shadow_channel,
            "jira_project": None,
            # Always false on creation. Going live is a deliberate later act, after a
            # shadow period — never something a wizard turns on.
            "go_live": False,
        },
        "source": source_block,
        "target": {
            "kind": "snowflake",
            "account": f"${{{sf_key}}}",
            "user": f"${{{sf_user_key}}}",
            "authenticator": "snowflake_jwt",
            "private_key_path": f"${{{sf_path_key}}}",
            "role_reader": spec.snowflake.role_reader,
            "role_writer": spec.snowflake.role_writer,
            "warehouse": spec.snowflake.warehouse,
            "database": spec.snowflake.database,
            "schemas": spec.snowflake.schemas,
            "state_schema": spec.snowflake.state_schema,
            "scratch_schema": spec.snowflake.scratch_schema,
            "query_timeout_s": 300,
        },
        "repo": {
            "url": spec.repo_url or "https://example.invalid/not-configured.git",
            "branch": spec.repo_branch,
            "auth_env": _env_key(spec.name, "GIT_TOKEN"),
            "paths": {"transforms": [], "ddl": [], "orchestration": []},
            "codeowners": ".github/CODEOWNERS",
        },
        "batch": batch,
    }

    _dump(
        directory / "project.yaml",
        project_yaml,
        header=(
            "Written by the onboarding wizard. Credentials are NOT here — they live in the\n"
            "per-project secret store and appear below only as ${VAR} references, so the\n"
            "load-time scanner that refuses credential literals still applies."
        ),
    )
    _dump(
        directory / "column_rules.yaml",
        _default_column_rules(),
        header=(
            "The single most important file for a useful first run. Audit columns included by\n"
            "mistake make every row look different."
        ),
    )
    _dump(
        directory / "manifest.yaml",
        {"tables": []},
        header=(
            "Tables to monitor. Filled in by the discovery step; `grain_confirmed_by` must be\n"
            "set by a person before any check on a table can activate."
        ),
    )
    _dump(
        directory / "layers.yaml",
        {"layers": {}, "hops": []},
        header=(
            "Medallion hops. A hop with no relation generates no checks and one coverage row\n"
            "saying so."
        ),
    )
    _dump(
        directory / "transforms.yaml",
        {"transforms": []},
        header=(
            "Deliberate source/target differences. Each is bound to a commit and expires when\n"
            "that code changes."
        ),
    )
    (directory / "checks").mkdir(exist_ok=True)

    log.info("project_written", project=spec.name, path=str(directory))
    return directory


def _dump(path: Path, data: dict[str, Any], *, header: str = "") -> None:
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, width=100)
    prefix = "".join(f"# {line}\n" for line in header.splitlines()) if header else ""
    path.write_text(prefix + body)


def _default_column_rules() -> dict[str, Any]:
    from dphm.config import defaults

    return {
        "classification": {
            "key": list(defaults.DEFAULT_KEY_PATTERNS),
            "audit": list(defaults.DEFAULT_AUDIT_PATTERNS),
            "derived": list(defaults.DEFAULT_DERIVED_PATTERNS),
        },
        "overrides": {},
    }


def delete_project(root: Path, name: str) -> bool:
    """Remove a project's config and secrets. Nothing in the warehouse is touched."""
    import shutil

    directory = root / name
    removed = False
    if directory.is_dir():
        shutil.rmtree(directory)
        removed = True
    secrets_store.delete(name)
    if removed:
        log.info("project_deleted", project=name)
    return removed
