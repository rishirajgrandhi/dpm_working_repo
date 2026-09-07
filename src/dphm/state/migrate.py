"""Forward-only numbered migrations (08 §2).

Migrations run at service startup, and **the service refuses to serve if they fail**
(08 §2, 12 §8). A half-migrated state store is worse than a stopped service, because
every query against it produces a plausible-looking wrong answer.

`CHECKSUM` means an *edited* migration is refused rather than silently re-applied. A
migration whose content changed after it ran is a history that no longer describes the
schema it produced.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dphm import __version__
from dphm.util.ids import stable_digest
from dphm.util.logging import get_logger
from dphm.util.sql import split_statements

if TYPE_CHECKING:
    from dphm.warehouse.connection import SnowflakeConnector

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASE_DDL = Path(__file__).parent / "ddl.sql"


class MigrationError(RuntimeError):
    """A migration failed, or the ledger disagrees with the files on disk."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    sql: str

    @property
    def checksum(self) -> str:
        return stable_digest(self.sql, length=32)


def discover(directory: Path | None = None) -> list[Migration]:
    """Numbered `NNN_name.sql` files, in order. The base DDL is version 0."""
    out = [Migration(version=0, filename="ddl.sql", sql=BASE_DDL.read_text())]
    directory = directory or MIGRATIONS_DIR
    if directory.is_dir():
        for path in sorted(directory.glob("*.sql")):
            stem = path.name.split("_", 1)[0]
            if not stem.isdigit():
                raise MigrationError(
                    f"{path.name}: migration files must be named NNN_description.sql"
                )
            out.append(Migration(version=int(stem), filename=path.name, sql=path.read_text()))
    versions = [m.version for m in out]
    dupes = sorted({v for v in versions if versions.count(v) > 1})
    if dupes:
        raise MigrationError(f"duplicate migration versions: {dupes}")
    return out


def _split_statements(sql: str) -> list[str]:
    """Snowflake's driver executes one statement per call.

    Delegates to the comment-aware splitter: ddl.sql contains inline comments with
    semicolons in them, and a naive split on ";" cuts those statements in half.
    """
    return split_statements(sql)


def _assert_state_schema_exists(conn: Any, schema: str) -> None:
    """Fail with a precise, actionable error rather than a permission error mid-DDL.

    The writer role deliberately cannot create schemas, so a missing state schema is a
    provisioning gap, not something the migration should paper over by escalating.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "select count(*) from information_schema.schemata where schema_name = %s",
            (schema.upper(),),
        )
        row = cur.fetchone()
        if not row or int(row[0]) == 0:
            raise MigrationError(
                f"schema {schema} does not exist. The writer role holds ALL on "
                f"{schema} and DPHM_SCRATCH only — it has no CREATE SCHEMA on the "
                "database, by design (02 §1). Provision the two schemas once as an "
                "administrator: scripts/provision_dev_roles.py (dev) or the block in "
                "docs/PROVISIONING.md §1.5 (production)."
            )
    finally:
        cur.close()


def applied_versions(conn: object) -> dict[int, str]:
    """version -> checksum, from the ledger. Empty when the schema does not exist yet."""
    cur = conn.cursor()  # type: ignore[attr-defined]
    try:
        cur.execute("select VERSION, CHECKSUM from DPHM_STATE.SCHEMA_MIGRATIONS order by VERSION")
        return {int(row[0]): str(row[1]) for row in cur.fetchall()}
    except Exception:
        # The state schema does not exist yet. That is the expected first-run state, not
        # an error — version 0 is about to create it.
        return {}
    finally:
        cur.close()


def current_level(connector: SnowflakeConnector) -> int:
    """The highest applied version, or -1 if the state store is not initialised.

    Surfaced in the Settings diagnostics panel (12 §2.10).
    """
    with connector.connect(role="reader") as conn:
        applied = applied_versions(conn)
    return max(applied) if applied else -1


def migrate(connector: SnowflakeConnector, *, directory: Path | None = None) -> list[int]:
    """Apply every unapplied migration in order. Returns the versions applied."""
    migrations = discover(directory)

    with connector.connect(role="writer") as conn:
        _assert_state_schema_exists(conn, connector.spec.state_schema)
        applied = applied_versions(conn)

        # Refuse an edited migration before applying anything new.
        for m in migrations:
            recorded = applied.get(m.version)
            if recorded is not None and recorded != m.checksum:
                raise MigrationError(
                    f"migration {m.version} ({m.filename}) has changed since it was applied "
                    f"(recorded {recorded[:12]}…, on disk {m.checksum[:12]}…). Migrations are "
                    "forward-only: add a new one rather than editing history."
                )

        pending = [m for m in migrations if m.version not in applied]
        if not pending:
            log.info("migrations_up_to_date", level=max(applied) if applied else -1)
            return []

        done: list[int] = []
        for m in pending:
            started = time.monotonic()
            cur = conn.cursor()
            try:
                for statement in _split_statements(m.sql):
                    cur.execute(statement)
                duration_ms = int((time.monotonic() - started) * 1000)
                cur.execute(
                    """
                    insert into DPHM_STATE.SCHEMA_MIGRATIONS
                        (VERSION, FILENAME, CHECKSUM, APPLIED_AT, APPLIED_BY, DURATION_MS)
                    select %s, %s, %s, current_timestamp()::timestamp_ntz, %s, %s
                    """,
                    (m.version, m.filename, m.checksum, f"dphm/{__version__}", duration_ms),
                )
            except Exception as exc:
                raise MigrationError(
                    f"migration {m.version} ({m.filename}) failed: {exc}. The service must "
                    "not serve on a partially migrated state store."
                ) from exc
            finally:
                cur.close()
            log.info("migration_applied", version=m.version, filename=m.filename)
            done.append(m.version)
        return done


def main() -> int:
    """Maintenance entrypoint (`dphm-migrate`). Not a user surface (12 §8)."""
    from dphm.config.loader import load_project
    from dphm.warehouse.connection import SnowflakeConnector

    if len(sys.argv) < 2:
        sys.stderr.write("usage: dphm-migrate <project-dir>\n")
        return 2
    loaded = load_project(sys.argv[1])
    connector = SnowflakeConnector(loaded.project.target)
    try:
        applied = migrate(connector)
    except MigrationError as exc:
        sys.stderr.write(f"migration failed: {exc}\n")
        return 1
    sys.stdout.write(
        f"applied {len(applied)} migration(s): {applied}\n" if applied else "already up to date\n"
    )
    return 0
