"""The doctor panel (12 §2.10, 15 M0).

M0's exit criterion: signing in and opening Settings shows every connection healthy —
RDS, Snowflake roles and grants, state migration level, git, LLM — **or a precise error**.

"Or a precise error" is the load-bearing half. A diagnostics panel that reports a generic
failure is a panel someone has to debug with a terminal, which defeats the point of the
product having no CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from dphm import __version__
from dphm.api.auth import Viewer
from dphm.api.deps import get_project
from dphm.catalog.postgres import PostgresCatalog
from dphm.catalog.snowflake import SnowflakeCatalog
from dphm.state import migrate, permissions
from dphm.util.logging import get_logger
from dphm.warehouse.connection import PostgresConnector, SnowflakeConnector

if TYPE_CHECKING:
    from collections.abc import Callable

    from dphm.config.loader import LoadedProject
    from dphm.config.models import ProjectConfig

log = get_logger(__name__)

router = APIRouter(tags=["diagnostics"])

CheckState = Literal["ok", "degraded", "error", "skipped"]


class DiagnosticResult(BaseModel):
    name: str
    state: CheckState
    detail: str
    # What breaks if this one is red — so the reader knows whether to act now.
    consequence: str | None = None
    duration_ms: int = 0


class DiagnosticsResponse(BaseModel):
    project: str
    tool_version: str
    healthy: bool
    results: list[DiagnosticResult] = Field(default_factory=list)
    # Human confirmations still outstanding. Not an error — it is the honest state of a
    # project mid-onboarding, and the Coverage panel renders the same list (12 §2.1).
    pending_confirmations: list[str] = Field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(r.state == "error" for r in self.results)


def _timed(fn: Callable[[], str], name: str, consequence: str) -> DiagnosticResult:
    started = time.monotonic()
    try:
        detail = fn()
    except Exception as exc:
        return DiagnosticResult(
            name=name,
            state="error",
            detail=f"{type(exc).__name__}: {exc}",
            consequence=consequence,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return DiagnosticResult(
        name=name,
        state="ok",
        detail=detail,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def run_diagnostics(loaded: LoadedProject, *, deep: bool = False) -> DiagnosticsResponse:
    """Every connection the tool needs, each reported independently.

    Independently matters in two ways. Lane A is unaffected by RDS being unreachable
    (03 §8), so a red RDS row must not present as "the tool is down" — and because the
    checks are genuinely independent, they run CONCURRENTLY. Sequentially this endpoint
    took 59 seconds against a real warehouse, which is not a panel anyone would load.

    `deep=False` skips the full role-hierarchy grant walk, which alone accounted for 44
    of those seconds (884 effective grants over three SHOW GRANTS round trips). The
    Settings screen requests it explicitly via `?deep=true` when someone wants to audit
    permissions, rather than paying for it on every page view.
    """
    from concurrent.futures import ThreadPoolExecutor

    from dphm.config.loader import unresolved_confirmations

    cfg = loaded.project
    sf = SnowflakeConnector(cfg.target)
    pg = PostgresConnector(cfg.source)
    tasks: list[tuple[str, Callable[[], DiagnosticResult]]] = []

    def _task(
        name: str, fn: Callable[[], str], consequence: str
    ) -> tuple[str, Callable[[], DiagnosticResult]]:
        return name, lambda: _timed(fn, name, consequence)

    # ── Snowflake: the one that must work. Lane A is 100% Snowflake. ──
    tasks.append(
        _task(
            "snowflake.connection",
            SnowflakeCatalog(sf).ping,
            "Nothing works. Lane A runs entirely in Snowflake and all state lives there.",
        )
    )

    # ── Grants, on both engines. A grant change must not look like data loss (R7). ──
    writable = frozenset({cfg.target.state_schema.upper(), cfg.target.scratch_schema.upper()})

    def _sf_grants() -> str:
        fp, violations = permissions.fingerprint_snowflake(
            sf, role=cfg.target.role_reader, writable_schemas=writable
        )
        if violations:
            # Grouped: a broad role yields dozens of near-identical privileges, and an
            # unreadable error is a error nobody acts on.
            raise PermissionError(permissions.summarize(violations))
        return f"{fp.summary} hash={fp.grants_hash[:12]}… (effective, incl. inherited)"

    if deep:
        tasks.append(
            _task(
                "snowflake.grants",
                _sf_grants,
                "Checks may be INCONCLUSIVE, or the reader role can write where it should not.",
            )
        )

    # ── State store migration level ──
    def _migrations() -> str:
        level = migrate.current_level(sf)
        expected = max(m.version for m in migrate.discover())
        if level < 0:
            raise RuntimeError(
                f"{cfg.target.state_schema} is not initialised. Run dphm-migrate; the "
                "service refuses to serve on an unmigrated state store (08 §2)."
            )
        if level < expected:
            raise RuntimeError(
                f"state store is at migration {level}, code expects {expected}. "
                "Migrations run at startup and must not be skipped."
            )
        return f"{cfg.target.state_schema} at migration level {level}"

    tasks.append(
        _task("state.migrations", _migrations, "No run can be recorded and no result persisted.")
    )

    # ── RDS: Lane B only. Lane A never touches it (02 §1). ──
    tasks.append(
        _task(
            "rds.connection",
            PostgresCatalog(pg).ping,
            "Lane B (source -> bronze parity) is blocked. Lane A is unaffected.",
        )
    )

    def _pg_grants() -> str:
        fp, violations = permissions.fingerprint_postgres(pg, user=cfg.source.user)
        if violations:
            raise PermissionError(permissions.summarize(violations))
        return f"{fp.summary} — read-only confirmed, hash={fp.grants_hash[:12]}…"

    tasks.append(
        _task(
            "rds.grants",
            _pg_grants,
            "The tool might be capable of writing to a production OLTP database.",
        )
    )

    # ── git: sign-offs, ownership, staleness all depend on it (assumption A3) ──
    tasks.append(_task("repo.git", lambda: _check_git(loaded), _GIT_CONSEQUENCE))

    # Bounded pool: enough for the checks we have, few enough that a project with many
    # connections cannot open an unbounded number of sessions at once.
    order = [name for name, _ in tasks]
    with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
        completed = dict(zip(order, pool.map(lambda item: item[1](), tasks), strict=True))
    results = [completed[name] for name in order]

    # ── LLM reachability. Degraded, never fatal (03 §8). No connection, so no thread. ──
    results.append(_check_llm(cfg))

    healthy = all(r.state in {"ok", "skipped"} for r in results)
    return DiagnosticsResponse(
        project=cfg.project,
        tool_version=__version__,
        healthy=healthy,
        results=results,
        pending_confirmations=unresolved_confirmations(loaded),
    )


_GIT_CONSEQUENCE = (
    "Commit-bound sign-offs, ownership routing, and staleness detection are all lost; "
    "authoring degrades to schema-only (assumption A3)."
)


def _check_git(loaded: LoadedProject) -> str:
    repo = loaded.project.repo
    if not os.environ.get(repo.auth_env):
        raise RuntimeError(
            f"{repo.auth_env} is not set, so the pipeline repo cannot be cloned (Q5)."
        )
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not installed in this image")
    # A fixed argv list, no shell. Reachability only — no clone, no fetch of refs we do
    # not need, and nothing written to disk.
    proc = subprocess.run(  # noqa: S603 - fixed argv, not user input
        [git, "ls-remote", "--heads", repo.url, repo.branch],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {proc.stderr.strip()[:300]}")
    if not proc.stdout.strip():
        raise RuntimeError(f"branch '{repo.branch}' not found at {repo.url}")
    sha = proc.stdout.split()[0][:12]
    return f"{repo.url} branch={repo.branch} head={sha}"


def _check_llm(cfg: ProjectConfig) -> DiagnosticResult:
    """Reachability only — no completion is requested, so this costs nothing.

    Reported `degraded` rather than `error` when unavailable: checks still execute and
    report, incidents just arrive with raw evidence and no narrative (03 §8).
    """
    if cfg.llm.provider != "anthropic":
        return DiagnosticResult(
            name="llm.reachable",
            state="skipped",
            detail=f"provider={cfg.llm.provider}; residency routing is a one-file change (13 §5)",
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return DiagnosticResult(
            name="llm.reachable",
            state="degraded",
            detail="ANTHROPIC_API_KEY is not set",
            consequence=(
                "Runs still execute and report. Incidents ship with raw evidence and "
                "category=UNCLASSIFIED; authoring falls back to templates (03 §8)."
            ),
        )
    return DiagnosticResult(
        name="llm.reachable",
        state="ok",
        detail=f"anthropic key present; model={cfg.llm.model} escalated={cfg.llm.model_escalated}",
    )


@router.get("/projects/{project}/diagnostics", response_model=DiagnosticsResponse)
async def get_diagnostics(project: str, _user: Viewer, deep: bool = False) -> DiagnosticsResponse:
    """The Settings health panel. Viewer is enough — it reveals no data, only reachability.

    `deep=true` adds the full role-hierarchy grant audit, which is slow enough (tens of
    seconds against a real account) that it must be asked for rather than assumed.
    """
    loaded = get_project(project)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown project '{project}'"
        )
    # Blocking driver calls, so keep them off the event loop.
    return await run_in_threadpool(run_diagnostics, loaded, deep=deep)
