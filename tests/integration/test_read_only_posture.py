"""Prove the tool cannot write to RDS, against a real engine.

"This tool reports on data. It never changes it" is the product's first principle
(01 §5). Everywhere else it is a claim in a document. Here it is a test: we ask a real
PostgreSQL server to accept a write on our connection and assert that it refuses.

The real defence in production is the GRANT — the role has no INSERT/UPDATE/DELETE. The
session-level read-only transaction tested here is the second line, and it is the one
that still holds if someone provisions the role too generously.
"""

from __future__ import annotations

import psycopg
import pytest

from dphm.state import permissions
from dphm.warehouse.connection import PostgresConnector

pytestmark = pytest.mark.integration


@pytest.fixture
def connector(pg_source_spec) -> PostgresConnector:
    return PostgresConnector(pg_source_spec)


@pytest.mark.parametrize(
    ("statement", "what"),
    [
        (
            "insert into public.customers (customer_id, source_row_id, name) values (99, 99, 'x')",
            "INSERT",
        ),
        ("update public.customers set name = 'changed' where customer_id = 1", "UPDATE"),
        ("delete from public.customers where customer_id = 1", "DELETE"),
        ("truncate public.customers", "TRUNCATE"),
        ("create table public.sneaky (id int)", "CREATE TABLE"),
        ("create temp table public.sneaky_tmp (id int)", "CREATE TEMP TABLE"),
        ("drop table public.orders", "DROP TABLE"),
        ("alter table public.customers add column injected int", "ALTER TABLE"),
    ],
)
def test_every_write_is_refused(connector: PostgresConnector, statement: str, what: str) -> None:
    """Not "we don't issue writes" — the connection is incapable of accepting one."""
    with connector.connect() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            cur.execute(statement)
    assert what  # names the case in the test id


def test_reads_still_work(connector: PostgresConnector) -> None:
    """The read-only posture must not be achieved by breaking the useful half."""
    with connector.connect() as conn:
        cur = conn.cursor()
        cur.execute("select count(*) from public.customers")
        row = cur.fetchone()
        # 10 rows for 7 distinct customers: the fixture carries the duplicates and the
        # full-ordering tie that the L2 dedup contract exists to catch.
        assert row is not None and row[0] == 10
        cur.execute("select count(distinct customer_id) from public.customers")
        distinct = cur.fetchone()
        assert distinct is not None and distinct[0] == 7


def test_the_data_is_unchanged_after_every_refused_write(
    connector: PostgresConnector,
) -> None:
    """Belt and braces: confirm nothing leaked through."""
    with connector.connect() as conn:
        cur = conn.cursor()
        cur.execute("select name from public.customers where customer_id = 1")
        row = cur.fetchone()
        assert row is not None and row[0] == "Acme Ltd"
        cur.execute(
            "select count(*) from information_schema.tables "
            "where table_schema = 'public' and table_name = 'sneaky'"
        )
        count = cur.fetchone()
        assert count is not None and count[0] == 0


def test_statement_timeout_is_set_on_the_session(connector: PostgresConnector) -> None:
    """RDS is a production OLTP box. Every query carries a timeout (02 §1)."""
    with connector.connect() as conn:
        cur = conn.cursor()
        cur.execute("show statement_timeout")
        row = cur.fetchone()
        assert row is not None and row[0] == "30s"


def test_permission_fingerprint_is_stable_and_detects_change(
    connector: PostgresConnector, pg_source_spec
) -> None:
    """13 §1: a revoked grant looks exactly like a table full of missing rows.

    Without fingerprinting, the tool's most alarming possible alert is also its most
    likely false one.
    """
    first, violations = permissions.fingerprint_postgres(connector, user=pg_source_spec.user)
    second, _ = permissions.fingerprint_postgres(connector, user=pg_source_spec.user)

    assert first.grants_hash == second.grants_hash, "the same grants must hash identically"
    assert permissions.has_changed(first, second.grants_hash) is False
    assert permissions.has_changed(first, "a-different-hash") is True
    # A first sighting is not a change — there is nothing to compare against yet.
    assert permissions.has_changed(first, None) is False
    assert violations == [] or all(v.engine == "postgres" for v in violations)


def test_a_violation_explains_itself_in_product_terms(
    connector: PostgresConnector,
) -> None:
    """The message has to be readable by whoever provisioned the role."""
    violation = permissions.PermissionViolation(
        engine="postgres", role_name="dphm_reader", privilege="INSERT", on_object="public.customers"
    )
    text = str(violation)
    assert "INSERT" in text
    assert "never" in text and "changes it" in text
