"""YAML -> models, env interpolation, cross-file validation (04 §4, 05).

Two properties this module is responsible for:

* **Validation happens at load, not at run.** A malformed config fails before a single
  query is issued, with the offending file in the message.
* **A secret literal in a YAML file is a load-time error**, not a warning (13 §3).

Configuration precedence (04 §4):
    API request parameter > env var (DPHM_*) > projects/<name>/*.yaml > config/defaults.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from dphm.config import secrets_store
from dphm.config.models import (
    CheckDef,
    ColumnRules,
    LayerContract,
    Manifest,
    ProjectConfig,
    Transforms,
)
from dphm.util import secrets
from dphm.util.ids import stable_digest

M = TypeVar("M", bound=BaseModel)

_ENV_PREFIX = "DPHM_"


class ConfigError(Exception):
    """A config problem, always carrying the file it came from."""

    def __init__(self, path: Path | str, message: str) -> None:
        self.path = str(path)
        super().__init__(f"{self.path}: {message}")


@dataclass(frozen=True)
class LoadedProject:
    """Everything a project's config says, validated and cross-checked."""

    project: ProjectConfig
    column_rules: ColumnRules
    manifest: Manifest
    layers: LayerContract
    transforms: Transforms
    checks: tuple[CheckDef, ...]
    config_sha: str
    root: Path

    @property
    def name(self) -> str:
        return self.project.project


# ── raw YAML reading ──────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(path, "file not found")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(path, f"invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(path, f"expected a mapping at the top level, got {type(raw).__name__}")
    return raw


def _read_yaml_list(path: Path) -> list[Any]:
    if not path.exists():
        raise ConfigError(path, "file not found")
    try:
        raw = yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError as exc:
        raise ConfigError(path, f"invalid YAML: {exc}") from exc
    if not isinstance(raw, list):
        raise ConfigError(path, f"expected a list at the top level, got {type(raw).__name__}")
    return raw


# ── the secret-literal scan (13 §3) ───────────────────────────────────────────


def scan_for_secret_literals(data: Any, path: Path, *, trail: str = "") -> None:
    """Walk a parsed YAML tree and refuse anything that looks like a credential.

    Runs BEFORE interpolation, so it inspects what is written in the file rather than
    what the environment supplies.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            here = f"{trail}.{key}" if trail else str(key)
            if isinstance(value, str):
                reason = secrets.looks_like_secret(value, key=str(key))
                if reason:
                    raise ConfigError(
                        path,
                        f"secret literal at '{here}': the value {reason}. "
                        "Secrets come only from env vars backed by the platform secret "
                        "manager; write ${DPHM_...} instead (13 §3).",
                    )
            else:
                scan_for_secret_literals(value, path, trail=here)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            scan_for_secret_literals(item, path, trail=f"{trail}[{i}]")


# ── interpolation ─────────────────────────────────────────────────────────────


def interpolate(data: Any, *, where: str, mapping: dict[str, str] | None = None) -> Any:
    """Expand ${VAR} everywhere in a parsed tree. A missing var raises.

    `mapping` supplies values from the project's secret store; the environment still
    wins, so a deployed service using a real secret manager needs no config change.
    """
    if isinstance(data, dict):
        return {k: interpolate(v, where=where, mapping=mapping) for k, v in data.items()}
    if isinstance(data, list):
        return [interpolate(v, where=where, mapping=mapping) for v in data]
    if isinstance(data, str) and secrets.is_env_reference(data):
        return secrets.resolve(data, where=where, mapping=mapping)
    return data


def _env_overrides(section: str) -> dict[str, str]:
    """`DPHM_TARGET__WAREHOUSE=X` overrides `target.warehouse` (04 §4).

    Env vars sit above YAML in precedence, so an operator can retarget a deployment
    without editing a file that is under review.
    """
    prefix = f"{_ENV_PREFIX}{section.upper()}__"
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            out[key[len(prefix) :].lower()] = value
    return out


def _validate(model: type[M], data: dict[str, Any], path: Path) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            lines.append(f"  {loc}: {err['msg']}")
        raise ConfigError(path, "invalid configuration:\n" + "\n".join(lines)) from exc


# ── the public entrypoint ─────────────────────────────────────────────────────


def load_project(root: Path | str) -> LoadedProject:
    """Load and cross-validate one project directory.

    Raises ConfigError with the offending file named. Never returns a partially valid
    project: a config that cannot be trusted must not reach the query layer.
    """
    root = Path(root)
    if not root.is_dir():
        raise ConfigError(root, "project directory not found")

    # Credentials typed into the onboarding wizard live in a per-project store outside
    # the repo. The environment still overrides them.
    mapping = secrets_store.resolution_mapping(root.name)

    project = _load_one(root / "project.yaml", ProjectConfig, section="project", mapping=mapping)
    column_rules = _load_optional(root / "column_rules.yaml", ColumnRules, mapping)
    manifest = _load_optional(root / "manifest.yaml", Manifest, mapping)
    layers = _load_optional(root / "layers.yaml", LayerContract, mapping)
    transforms = _load_optional(root / "transforms.yaml", Transforms, mapping)
    checks = _load_checks(root / "checks", mapping)

    loaded = LoadedProject(
        project=project,
        column_rules=column_rules,
        manifest=manifest,
        layers=layers,
        transforms=transforms,
        checks=checks,
        config_sha=_config_sha(root),
        root=root,
    )
    validate_cross_file(loaded)
    return loaded


def _load_one(
    path: Path, model: type[M], *, section: str, mapping: dict[str, str] | None = None
) -> M:
    raw = _read_yaml(path)
    scan_for_secret_literals(raw, path)
    data = _interpolate_or_raise(raw, path, mapping)
    if not isinstance(data, dict):  # pragma: no cover - _read_yaml guarantees a mapping
        raise ConfigError(path, "expected a mapping at the top level")
    overrides = _env_overrides(section)
    if overrides:
        merged = dict(data.get(section, {})) if isinstance(data.get(section), dict) else {}
        merged.update(overrides)
        if merged:
            data = {**data, section: merged}
    return _validate(model, data, path)


def _interpolate_or_raise(data: Any, path: Path, mapping: dict[str, str] | None = None) -> Any:
    """Interpolate, converting a missing-variable failure into a ConfigError.

    Every load-time failure must name the file it came from, or the operator is left
    guessing which of six YAML files referenced the variable.
    """
    try:
        return interpolate(data, where=str(path), mapping=mapping)
    except secrets.SecretResolutionError as exc:
        raise ConfigError(path, str(exc)) from exc


def _load_optional(path: Path, model: type[M], mapping: dict[str, str] | None = None) -> M:
    """A missing optional file yields an empty model, not an error.

    An empty manifest is a legitimate pre-onboarding state; a malformed one is not.
    """
    if not path.exists():
        return model()
    raw = _read_yaml(path)
    scan_for_secret_literals(raw, path)
    data = _interpolate_or_raise(raw, path, mapping)
    if not isinstance(data, dict):  # pragma: no cover
        raise ConfigError(path, "expected a mapping at the top level")
    return _validate(model, data, path)


def _load_checks(checks_dir: Path, mapping: dict[str, str] | None = None) -> tuple[CheckDef, ...]:
    if not checks_dir.is_dir():
        return ()
    out: list[CheckDef] = []
    for path in sorted(checks_dir.rglob("*.yaml")):
        raw_list = _read_yaml_list(path)
        scan_for_secret_literals(raw_list, path)
        data = _interpolate_or_raise(raw_list, path, mapping)
        for item in data:
            if not isinstance(item, dict):
                raise ConfigError(path, f"expected a list of check mappings, got {type(item)}")
            out.append(_validate(CheckDef, item, path))
    ids = [c.id for c in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ConfigError(checks_dir, f"duplicate check ids across files: {dupes}")
    return tuple(out)


def _config_sha(root: Path) -> str:
    """Recorded on every run as RUNS.CONFIG_SHA, so a result is attributable to a config."""
    parts: dict[str, str] = {}
    for path in sorted(root.rglob("*.yaml")):
        parts[str(path.relative_to(root))] = path.read_text()
    return stable_digest(parts, length=16)


# ── cross-file validation ─────────────────────────────────────────────────────


def validate_cross_file(loaded: LoadedProject) -> None:
    """Checks that no single file can make on its own.

    These are the ones that catch a config which is individually valid and jointly
    incoherent — the shape of mistake that otherwise surfaces as a check that quietly
    compares nothing.
    """
    root = loaded.root
    manifest_names = {t.name.upper() for t in loaded.manifest.tables}

    # A check must point at a table the manifest declares, or nothing knows its grain,
    # its column rules, or whether a human confirmed anything about it.
    for check in loaded.checks:
        if check.table.upper() not in manifest_names:
            raise ConfigError(
                root / "checks",
                f"check '{check.id}' targets '{check.table}', which manifest.yaml does not "
                "declare. Without a manifest entry the check has no confirmed grain and no "
                "column rules.",
            )

    # A check that names a hop must name one that exists.
    hop_ids = {h.id for h in loaded.layers.hops}
    for check in loaded.checks:
        if check.hop_id and check.hop_id not in hop_ids:
            raise ConfigError(
                root / "checks",
                f"check '{check.id}' references hop '{check.hop_id}', which layers.yaml does "
                f"not define. Known hops: {sorted(hop_ids) or 'none'}",
            )

    # transforms_ref must resolve, or a declared divergence is silently not applied and
    # Lane B reports an intentional difference as a failure.
    transform_ids = {t.id for t in loaded.transforms.transforms}
    for hop in loaded.layers.hops:
        missing = sorted(set(hop.transforms_ref) - transform_ids)
        if missing:
            raise ConfigError(
                root / "layers.yaml",
                f"hop '{hop.id}' references transform(s) {missing} that transforms.yaml does "
                "not declare. An unresolved transform reference means a deliberate difference "
                "would be reported as a failure.",
            )

    # Lane B needs an RDS counterpart, and the source schema must be one we can read.
    source_schemas = {s.lower() for s in loaded.project.source.schemas}
    for table in loaded.manifest.tables:
        if table.lane_b != "unvalidated" and table.source_table:
            schema = table.source_table.split(".")[0].lower() if "." in table.source_table else ""
            if schema and schema not in source_schemas:
                raise ConfigError(
                    root / "manifest.yaml",
                    f"table '{table.name}' compares against '{table.source_table}', but schema "
                    f"'{schema}' is not in source.schemas {sorted(source_schemas)} — the RDS "
                    "role cannot read it.",
                )

    # An scd2_of hop's gold table must actually be declared scd2, or the SCD suite is
    # generated against a table nobody classified as a dimension.
    for hop in loaded.layers.hops:
        if hop.relation == "scd2_of":
            gold_table = loaded.manifest.get(hop.target)
            if gold_table is None:
                raise ConfigError(
                    root / "layers.yaml",
                    f"hop '{hop.id}' has relation=scd2_of but its target '{hop.target}' is not "
                    "in manifest.yaml, so the SCD suite has no business key to build from.",
                )
            if gold_table.table_type != "scd2":
                raise ConfigError(
                    root / "manifest.yaml",
                    f"hop '{hop.id}' has relation=scd2_of but '{hop.target}' is declared "
                    f"table_type={gold_table.table_type}. The 11-check suite needs "
                    "table_type=scd2.",
                )


def unresolved_confirmations(loaded: LoadedProject) -> list[str]:
    """Human confirmations still outstanding.

    Not an error — it is the honest state of a project mid-onboarding, and it is what the
    Review Queue and the coverage panel render. Returned rather than raised because a
    project with unconfirmed grain should still start, just with those checks inactive.
    """
    out: list[str] = []
    for table in loaded.manifest.tables:
        if not table.activatable:
            out.append(f"{table.name}: grain unconfirmed (B3/R4 — checks cannot activate)")
        if table.scd2 and not table.scd2.check7_generatable:
            out.append(
                f"{table.name}: no stage_table, so SCD check 7 "
                "(tracked_change_created_version) cannot be generated — reported as "
                "unvalidated, not omitted"
            )
    for hop in loaded.layers.hops:
        if not hop.contract_confirmed:
            what = "dedup key/pick rule" if hop.relation == "dedup_of" else "mart group_by"
            out.append(f"hop {hop.id}: {what} unconfirmed (07B §6.3)")
        if hop.unverified_measures:
            out.append(
                f"hop {hop.id}: non-additive measures cannot be recomputed and are reported "
                f"unverified: {', '.join(hop.unverified_measures)}"
            )
    return out
