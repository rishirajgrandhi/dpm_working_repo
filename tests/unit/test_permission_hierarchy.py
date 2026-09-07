"""Permission fingerprinting must capture INHERITED privileges, not just direct ones.

Found live on a real Snowflake account. `SHOW GRANTS TO ROLE X` lists only what was
granted to X directly. It does not list what X inherits through a role it holds USAGE on,
and **every Snowflake role implicitly inherits PUBLIC** — whose grants accumulate over an
account's lifetime and are nobody's deliberate decision.

The concrete case: PUBLIC held USAGE on SNOWFLAKE_LEARNING_ROLE, which held CREATE SCHEMA
on a database. So the configured role could create schemas in a database nobody had
granted it, and a direct-grants-only fingerprint reported that role as clean.

This matters more in production than in dev: the whole "checks execute as DPHM_READER and
cannot write" guarantee rests on this fingerprint being complete.
"""

from __future__ import annotations

from typing import Any

from dphm.state import permissions


class _FakeCursor:
    """Replays a role hierarchy: role -> the grants SHOW GRANTS returns for it."""

    def __init__(self, hierarchy: dict[str, list[tuple[str, str, str]]]) -> None:
        self.hierarchy = hierarchy
        self.queried: list[str] = []
        self._rows: list[tuple[str, str, str]] = []

    description = (("PRIVILEGE",), ("GRANTED_ON",), ("NAME",))

    def execute(self, sql: str) -> None:
        role = sql.rsplit(" ", 1)[-1].strip().upper()
        self.queried.append(role)
        self._rows = self.hierarchy.get(role, [])

    def fetchall(self) -> list[tuple[str, str, str]]:
        return self._rows

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


class _FakeConnector:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def connect(self, **_: Any):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield _FakeConn(self._cursor)

        return _cm()


WRITABLE = frozenset({"DPHM_STATE", "DPHM_SCRATCH"})


def test_the_hierarchy_is_walked_including_public() -> None:
    cursor = _FakeCursor(
        {
            "DPHM_READER": [("SELECT", "TABLE", "ANALYTICS.GOLD.DIM_CUSTOMER")],
            "PUBLIC": [("USAGE", "ROLE", "LEGACY_ETL_ROLE")],
            "LEGACY_ETL_ROLE": [("INSERT", "TABLE", "ANALYTICS.GOLD.DIM_CUSTOMER")],
        }
    )
    fingerprint, violations = permissions.fingerprint_snowflake(
        _FakeConnector(cursor), role="DPHM_READER", writable_schemas=WRITABLE
    )

    assert "PUBLIC" in cursor.queried, "PUBLIC must be walked even when nobody granted it"
    assert "LEGACY_ETL_ROLE" in cursor.queried, "roles reachable via USAGE must be walked"

    assert len(violations) == 1
    violation = violations[0]
    assert violation.privilege == "INSERT"
    assert violation.via_role == "LEGACY_ETL_ROLE"
    # The message must say the privilege is inherited, or the reader looks in the wrong
    # place for it.
    assert "inherited via LEGACY_ETL_ROLE" in str(violation)
    assert any(g["via_role"] == "LEGACY_ETL_ROLE" for g in fingerprint.grants)


def test_a_direct_grants_only_walk_would_have_missed_it() -> None:
    """The regression, stated as a test: the dangerous grant is not on the role itself."""
    cursor = _FakeCursor(
        {
            "DPHM_READER": [("SELECT", "TABLE", "ANALYTICS.GOLD.T")],
            "PUBLIC": [("USAGE", "ROLE", "SNOWFLAKE_LEARNING_ROLE")],
            "SNOWFLAKE_LEARNING_ROLE": [("CREATE SCHEMA", "DATABASE", "CUSTOMER_DB")],
        }
    )
    _, violations = permissions.fingerprint_snowflake(
        _FakeConnector(cursor), role="DPHM_READER", writable_schemas=WRITABLE
    )
    direct_only = [v for v in violations if v.via_role == "DPHM_READER"]
    assert direct_only == [], "the point is that nothing dangerous is granted directly"
    assert len(violations) == 1


def test_snowflake_owned_databases_are_not_reported_as_findings() -> None:
    """They ship on every account and hold no customer data — noise, not a finding."""
    cursor = _FakeCursor(
        {
            "DPHM_READER": [],
            "PUBLIC": [("USAGE", "ROLE", "SNOWFLAKE_LEARNING_ROLE")],
            "SNOWFLAKE_LEARNING_ROLE": [
                ("CREATE SCHEMA", "DATABASE", "SNOWFLAKE_LEARNING_DB"),
                ("MODIFY", "DATABASE", "SNOWFLAKE_SAMPLE_DATA"),
            ],
        }
    )
    _, violations = permissions.fingerprint_snowflake(
        _FakeConnector(cursor), role="DPHM_READER", writable_schemas=WRITABLE
    )
    assert violations == []


def test_a_real_database_named_like_a_snowflake_one_still_reports() -> None:
    """Named explicitly rather than prefix-matched, so this is not a bypass."""
    cursor = _FakeCursor(
        {
            "DPHM_READER": [("MODIFY", "DATABASE", "SNOWFLAKE_MIGRATION_STAGING")],
            "PUBLIC": [],
        }
    )
    _, violations = permissions.fingerprint_snowflake(
        _FakeConnector(cursor), role="DPHM_READER", writable_schemas=WRITABLE
    )
    assert len(violations) == 1
    assert violations[0].on_object == "SNOWFLAKE_MIGRATION_STAGING"


def test_writes_to_our_own_schemas_are_allowed() -> None:
    cursor = _FakeCursor(
        {
            "DPHM_WRITER": [
                ("MODIFY", "SCHEMA", "ANALYTICS.DPHM_STATE"),
                ("INSERT", "TABLE", "ANALYTICS.DPHM_SCRATCH.PARITY_X"),
            ],
            "PUBLIC": [],
        }
    )
    _, violations = permissions.fingerprint_snowflake(
        _FakeConnector(cursor), role="DPHM_WRITER", writable_schemas=WRITABLE
    )
    assert violations == []


def test_a_cycle_in_the_role_graph_terminates() -> None:
    """Role graphs can be cyclic. An infinite loop at startup is a hung service."""
    cursor = _FakeCursor(
        {
            "A": [("USAGE", "ROLE", "B")],
            "B": [("USAGE", "ROLE", "A")],
            "PUBLIC": [],
        }
    )
    fingerprint, _ = permissions.fingerprint_snowflake(
        _FakeConnector(cursor), role="A", writable_schemas=WRITABLE
    )
    assert cursor.queried.count("A") == 1
    assert fingerprint.grants_hash


def test_an_unreadable_role_is_recorded_not_skipped() -> None:
    """An unreadable grant is not an absent one — silence here would be a false clean."""

    class _Raising(_FakeCursor):
        def execute(self, sql: str) -> None:
            role = sql.rsplit(" ", 1)[-1].strip().upper()
            self.queried.append(role)
            if role == "OPAQUE_ROLE":
                raise RuntimeError("insufficient privileges to view grants")
            self._rows = self.hierarchy.get(role, [])

    cursor = _Raising({"R": [("USAGE", "ROLE", "OPAQUE_ROLE")], "PUBLIC": []})
    fingerprint, _ = permissions.fingerprint_snowflake(
        _FakeConnector(cursor), role="R", writable_schemas=WRITABLE
    )
    assert any(g["privilege"] == "<UNREADABLE>" for g in fingerprint.grants)


def test_an_unsafe_role_name_is_refused_rather_than_interpolated() -> None:
    """SHOW GRANTS takes no bind parameters, so the identifier is validated instead."""
    import pytest

    cursor = _FakeCursor({})
    with pytest.raises(ValueError, match="unsafe role name"):
        permissions.fingerprint_snowflake(
            _FakeConnector(cursor),
            role='DPHM"; drop database ANALYTICS; --',
            writable_schemas=WRITABLE,
        )
