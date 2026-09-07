"""Warehouse connections, and the writer-role allowlist.

04 rule 6: no module outside `state/` and `parity/tier2_land.py` may use `DPHM_WRITER`.
That rule is enforced twice — by the Snowflake grant itself, and here, at runtime, by
inspecting the caller's module. The grant is the real defence; this is the second line
and, more usefully, a clear error message instead of a permission failure from Snowflake
three layers down.

Every query carries QUERY_TAG = 'dphm:{run_id}:{check_id}' so credits are attributable
per check (12 §5, 13 §4).
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from dphm.util.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from dphm.config.models import ProjectConfig, SourceSpec, TargetSpec

log = get_logger(__name__)

Role = Literal["reader", "writer", "admin"]

# Modules permitted to request a DPHM_WRITER connection. Anything else raises.
_WRITER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "dphm.state.migrate",
        "dphm.state.repo",
        "dphm.state.incidents",
        "dphm.state.signoff",
        "dphm.state.permissions",
        "dphm.state.checkpointer",
        "dphm.state.sweeper",
        "dphm.parity.tier2_land",
        # Gate 3 clones tables into DPHM_SCRATCH and mutates the clone (10).
        "dphm.engine.mutation",
    }
)


class WriterAccessDeniedError(PermissionError):
    """A module that is not on the allowlist asked for a writer connection."""


class WarehouseConnectionError(RuntimeError):
    """We could not connect, with the reason preserved for the diagnostics panel."""


class Cursor(Protocol):
    """The subset of DB-API we use. Keeps the drivers behind a seam."""

    def execute(self, operation: str, parameters: Any = ..., /) -> Any: ...
    def fetchall(self) -> Sequence[Any]: ...
    def fetchone(self) -> Any: ...
    @property
    def description(self) -> Any: ...


# Frames that sit between this module and its real caller. `@contextmanager` inserts a
# contextlib frame on every `with connector.connect(...)`, so without skipping these the
# walk reports "contextlib" and refuses every legitimate writer — which is exactly what
# happened the first time the migration runner was pointed at a real warehouse.
_TRANSPARENT_MODULES = frozenset({"contextlib", "functools", "typing", __name__})


def _calling_module() -> str:
    """The first frame outside this module and its wrappers."""
    frame = inspect.currentframe()
    try:
        while frame is not None:
            module = str(frame.f_globals.get("__name__", ""))
            if module and module not in _TRANSPARENT_MODULES:
                return module
            frame = frame.f_back
        return "<unknown>"
    finally:
        del frame


def assert_writer_allowed(caller: str | None = None) -> str:
    """Raise unless the calling module may hold a writer connection."""
    module = caller or _calling_module()
    root = module.rsplit(".", 1)[0] if module.startswith("dphm.") else module
    if module not in _WRITER_ALLOWLIST and root not in _WRITER_ALLOWLIST:
        raise WriterAccessDeniedError(
            f"module '{module}' requested a DPHM_WRITER connection. Only state/ and "
            f"parity/tier2_land.py may write, and only to DPHM_STATE and DPHM_SCRATCH "
            f"(04 rule 6, 13 §1). Checks execute as DPHM_READER."
        )
    return module


@dataclass(frozen=True)
class SnowflakeConnector:
    """Opens Snowflake connections. Reader by default; writer or admin only on request.

    Every statement issued through `guarded_execute` passes `warehouse/safety.py` first,
    which refuses writes outside the objects the tool owns — regardless of what the role
    would permit. Holding ACCOUNTADMIN does not exempt a statement: the role decides what
    Snowflake *would* allow, the guard decides what we are *willing to ask for*.
    """

    spec: TargetSpec
    admin_role: str | None = None
    # Objects this connector may write to or destroy. Defaults to the tool's own two
    # schemas; the dev tooling widens it to DPHM_TEST explicitly.
    writable_roots: frozenset[str] = frozenset()

    def _role_name(self, role: Role) -> str:
        if role == "admin":
            # Never from config: elevated access is passed in at the call site so it
            # cannot become a deployment's quiet default.
            return self.admin_role or "ACCOUNTADMIN"
        return self.spec.role_writer if role == "writer" else self.spec.role_reader

    def _connect_kwargs(self, role: Role) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "account": self.spec.account,
            "user": self.spec.user,
            "role": self._role_name(role),
            "warehouse": self.spec.warehouse,
            "database": self.spec.database,
            "client_session_keep_alive": False,
            "login_timeout": 30,
            "network_timeout": self.spec.query_timeout_s,
        }
        if self.spec.authenticator == "snowflake_jwt":
            if not self.spec.private_key_path:  # pragma: no cover - model validator covers it
                raise WarehouseConnectionError("snowflake_jwt requires private_key_path")
            kwargs["private_key_file"] = str(Path(self.spec.private_key_path))
        else:
            kwargs["authenticator"] = self.spec.authenticator
        return kwargs

    @contextmanager
    def connect(
        self,
        *,
        role: Role = "reader",
        query_tag: str | None = None,
    ) -> Iterator[Any]:
        """Open a connection. `role="writer"` is allowlist-checked before anything else."""
        if role == "writer":
            caller = assert_writer_allowed()
            log.debug("writer_connection_granted", module=caller)
        elif role == "admin":
            caller = assert_writer_allowed()
            # Loudly, and at warning level: an elevated session should be visible in the
            # log of any run that opened one.
            log.warning(
                "admin_connection_granted",
                module=caller,
                role=self._role_name(role),
                note="elevated session; statements are still guarded by warehouse/safety",
            )

        try:
            import snowflake.connector as sf
        except ImportError as exc:  # pragma: no cover - driver present in the image
            raise WarehouseConnectionError(
                "snowflake-connector-python is not installed in this environment"
            ) from exc

        conn = None
        try:
            conn = sf.connect(**self._connect_kwargs(role))
        except Exception as exc:
            # Log only host/database/role/user — never anything from the connection
            # layer's payload (13 §3).
            log.warning(
                "snowflake_connect_failed",
                account=self.spec.account,
                database=self.spec.database,
                role=role,
                error=type(exc).__name__,
            )
            raise WarehouseConnectionError(f"Snowflake connection failed: {exc}") from exc

        try:
            with conn.cursor() as cur:
                timeout_s = int(self.spec.query_timeout_s)
                cur.execute(f"alter session set statement_timeout_in_seconds = {timeout_s}")
                if query_tag:
                    cur.execute("alter session set query_tag = %s", (query_tag,))
            yield conn
        finally:
            conn.close()


@dataclass(frozen=True)
class PostgresConnector:
    """Opens read-only RDS connections.

    Every connection sets a statement timeout and a read-only transaction. RDS is a
    production OLTP box: the tool must not be *capable* of hurting it (02 §1).
    """

    spec: SourceSpec

    @contextmanager
    def connect(self) -> Iterator[Any]:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise WarehouseConnectionError("psycopg is not installed in this environment") from exc

        conn = None
        try:
            conn = psycopg.connect(
                host=self.spec.host,
                port=self.spec.port,
                dbname=self.spec.database,
                user=self.spec.user,
                password=self.spec.password,
                connect_timeout=15,
                # Belt and braces: the role is read-only, and so is the session.
                options=f"-c statement_timeout={int(self.spec.statement_timeout_s * 1000)} "
                f"-c default_transaction_read_only=on",
            )
        except Exception as exc:
            log.warning(
                "postgres_connect_failed",
                host=self.spec.host,
                database=self.spec.database,
                error=type(exc).__name__,
            )
            raise WarehouseConnectionError(f"RDS connection failed: {exc}") from exc

        try:
            conn.read_only = True
            yield conn
        finally:
            conn.close()


def query_tag(run_id: str, check_id: str | None = None) -> str:
    """`dphm:{run_id}:{check_id}` — 12 §5."""
    return f"dphm:{run_id}:{check_id}" if check_id else f"dphm:{run_id}"


def connectors(cfg: ProjectConfig) -> tuple[SnowflakeConnector, PostgresConnector]:
    return SnowflakeConnector(cfg.target), PostgresConnector(cfg.source)


def guarded_execute(
    cursor: Any,
    sql: str,
    params: Any = None,
    *,
    writable_roots: Iterable[str] | None = None,
    allow_account_level: bool = False,
    context: str = "",
) -> Any:
    """Run one statement, after the safety guard has approved it.

    Prefer this over `cursor.execute` for anything that might write. The guard is a
    function anyone can forget to call; routing execution through it is what makes the
    protection structural rather than remembered.
    """
    from dphm.warehouse.safety import DEFAULT_WRITABLE_ROOTS, guard_statement

    guard_statement(
        sql,
        writable_roots=writable_roots if writable_roots is not None else DEFAULT_WRITABLE_ROOTS,
        allow_account_level=allow_account_level,
        context=context,
    )
    return cursor.execute(sql, params) if params is not None else cursor.execute(sql)
