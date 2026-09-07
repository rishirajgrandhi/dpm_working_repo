"""FastAPI app — the only public interface (03 §2, 12 §3).

There is no user-facing CLI. Humans use the web app; machines call signed webhooks or
the internal scheduler.

Startup order matters and is deliberate: config is validated, then migrations run, and
**the service refuses to serve if migrations fail** (08 §2, 12 §8).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from dphm import __version__
from dphm.api import deps
from dphm.api.routes import diagnostics, projects, runs
from dphm.util.logging import configure, get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger(__name__)


class StateStoreNotReadyError(RuntimeError):
    """Migrations did not complete. The service must not serve."""


def _run_migrations_or_refuse() -> None:
    """08 §2: migrations run at startup and the service refuses to serve if they fail.

    `DPHM_SKIP_MIGRATIONS=1` exists for local UI work with no warehouse attached. It is
    refused in production for the same reason the dev-auth bypass is.
    """
    if os.environ.get("DPHM_SKIP_MIGRATIONS") == "1":
        if os.environ.get("DPHM_ENV", "").lower() in {"production", "prod"}:
            raise StateStoreNotReadyError(
                "DPHM_SKIP_MIGRATIONS is set in production. Refusing to serve on a state "
                "store of unknown level (08 §2)."
            )
        log.warning("migrations_skipped", reason="DPHM_SKIP_MIGRATIONS=1")
        return

    from dphm.state.migrate import MigrationError, migrate
    from dphm.warehouse.connection import SnowflakeConnector, WarehouseConnectionError

    for loaded in deps.all_projects():
        connector = SnowflakeConnector(loaded.project.target)
        try:
            applied = migrate(connector)
        except MigrationError as exc:
            # A migration that RAN and FAILED is the dangerous case: the state store may
            # be half-applied, and every query against it would then return a
            # plausible-looking wrong answer. Refuse to serve (08 §2).
            raise StateStoreNotReadyError(
                f"project '{loaded.name}': {exc}. The service will not serve on a "
                "partially migrated state store."
            ) from exc
        except WarehouseConnectionError as exc:
            # Could not connect at all — a different failure, and not one that leaves the
            # state store in an unknown condition. Record it, surface it in diagnostics,
            # and serve the other projects. One unreachable project must not take the
            # whole service down: otherwise a stale example config or a transient network
            # fault stops a healthy pipeline from being monitored.
            deps.record_unreachable(loaded.name, str(exc))
            log.error("project_warehouse_unreachable", project=loaded.name, error=str(exc)[:300])
            continue
        if applied:
            log.info("migrations_applied", project=loaded.name, versions=applied)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure(level=os.environ.get("DPHM_LOG_LEVEL", "INFO"))
    loaded, errors = deps.load_all()
    log.info("startup", version=__version__, projects=loaded, invalid=sorted(errors))
    _run_migrations_or_refuse()
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Data Pipeline Health Monitor",
        version=__version__,
        description=(
            "Reports on data. Never changes it. The React SPA is the only intended "
            "consumer, but the API is documented because orchestrators may want to "
            "trigger runs (12 §3)."
        ),
        lifespan=lifespan,
        # OpenAPI is the source of the frontend's generated types, so the two cannot
        # drift silently (04 rule 5, 14 §6b).
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
    )

    # httpOnly, SameSite=Lax session cookie. No local accounts (12 §6).
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("DPHM_SESSION_SECRET", "dev-only-not-for-production"),
        session_cookie="dphm_session",
        https_only=os.environ.get("DPHM_ENV", "").lower() in {"production", "prod"},
        same_site="lax",
    )

    app.include_router(diagnostics.router, prefix="/api/v1")
    app.include_router(projects.router, prefix="/api/v1")
    app.include_router(runs.router, prefix="/api/v1")

    @app.get("/api/v1/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        """Liveness only. Says nothing about the warehouse — that is /diagnostics."""
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
