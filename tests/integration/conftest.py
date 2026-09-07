"""A real PostgreSQL server for the integration tests.

14 §4 asks for a fixture `orders_prod` database carrying one instance of every
canonicalization case. It suggests testcontainers; this uses `pgserver`, which bundles a
real PostgreSQL binary and needs no Docker daemon — the same database, one fewer
prerequisite, and it works on a laptop and in CI unchanged.

These are real assertions against a real engine. Nothing here is mocked: a catalog reader
that passes against a mock and fails against PostgreSQL has tested nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    """Boot one server for the whole session and seed it once."""
    pgserver = pytest.importorskip(
        "pgserver", reason="pgserver provides the embedded PostgreSQL used by these tests"
    )
    data_dir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(data_dir)
    server.psql("create database orders_prod")
    seed = (FIXTURES / "postgres_seed.sql").read_text()
    server.psql(seed, dbname="orders_prod") if _accepts_dbname(server) else None
    if not _accepts_dbname(server):
        # Older pgserver: connect through psycopg instead.
        import psycopg

        with psycopg.connect(server.get_uri(database="orders_prod")) as conn:
            conn.execute(seed)
            conn.commit()
    yield server
    server.cleanup()


def _accepts_dbname(server: Any) -> bool:
    import inspect

    try:
        return "dbname" in inspect.signature(server.psql).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False


@pytest.fixture(scope="session")
def pg_dsn(pg_server: Any) -> str:
    return str(pg_server.get_uri(database="orders_prod"))


@pytest.fixture
def pg_source_spec(pg_server: Any):
    """A SourceSpec pointed at the embedded server.

    pgserver listens on a unix socket, so `host` is a directory path. psycopg accepts
    that, which is why the connector takes host/port rather than a DSN string.
    """
    from urllib.parse import parse_qs, urlparse

    from dphm.config.models import SourceSpec

    parsed = urlparse(str(pg_server.get_uri(database="orders_prod")))
    socket_dir = parse_qs(parsed.query).get("host", [""])[0]
    return SourceSpec.model_validate(
        {
            "kind": "postgres",
            "host": socket_dir or (parsed.hostname or "localhost"),
            "port": parsed.port or 5432,
            "database": "orders_prod",
            "user": parsed.username or "postgres",
            "password": parsed.password or "",
            "schemas": ["public"],
            "read_only": True,
            "statement_timeout_s": 30,
        }
    )
