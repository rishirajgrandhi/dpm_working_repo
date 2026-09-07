"""Every fixture statement must parse in its target dialect.

14 §3: "Each snapshot is also parsed by sqlglot in the Snowflake dialect, so a template
can never merge in a state that does not parse."

The same rule applied to the fixtures. Without this, a typo in the Snowflake seed is only
discovered by someone with warehouse credentials, which makes the fixture unreviewable by
anyone else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot

from dphm.util.sql import split_statements, strip_comments

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DDL = Path(__file__).resolve().parents[2] / "src" / "dphm" / "state" / "ddl.sql"


def _statements(sql: str) -> list[str]:
    """The same splitter the migration runner uses, so the two cannot disagree."""
    return split_statements(sql)


@pytest.mark.parametrize(
    ("filename", "dialect"),
    [
        ("postgres_seed.sql", "postgres"),
        ("snowflake_seed.sql", "snowflake"),
    ],
)
def test_fixture_sql_parses(filename: str, dialect: str) -> None:
    statements = _statements((FIXTURES / filename).read_text())
    assert statements, f"{filename} produced no statements"
    for index, statement in enumerate(statements):
        try:
            parsed = sqlglot.parse_one(statement, dialect=dialect)
        except Exception as exc:  # pragma: no cover - the failure message is the point
            pytest.fail(
                f"{filename} statement {index + 1} does not parse as {dialect}: {exc}\n"
                f"{statement[:400]}"
            )
        assert parsed is not None


def test_state_ddl_parses_as_snowflake() -> None:
    """The state store's own DDL is applied at startup; it must never be unparseable."""
    for index, statement in enumerate(_statements(DDL.read_text())):
        try:
            sqlglot.parse_one(statement, dialect="snowflake")
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"ddl.sql statement {index + 1} does not parse: {exc}")


def test_state_ddl_declares_no_foreign_keys() -> None:
    """08 §2: Snowflake does not enforce them, and declaring unenforced constraints is
    exactly the kind of false confidence this product is against."""
    # Strip comments first: the DDL's own comments *explain* why there are no foreign
    # keys, so a naive text search finds the explanation and fails.
    sql = strip_comments(DDL.read_text()).lower()
    assert "foreign key" not in sql
    assert "references" not in sql


def test_result_columns_are_not_nullable() -> None:
    """Risk R1 at the schema level: if the engine cannot say what it compared, the result
    is an error, not a pass."""
    text = DDL.read_text()
    for column in ("COLUMNS_COMPARED", "COLUMNS_EXCLUDED"):
        line = next(line for line in text.splitlines() if column in line)
        assert "not null" in line.lower(), f"{column} must be NOT NULL"


def test_snowflake_fixture_dedup_is_readable_as_a_contract() -> None:
    """07B §6.1: the transform SQL usually *contains* the dedup contract.

    The fixture's L2 must therefore express its dedup as a real QUALIFY ROW_NUMBER() with
    a two-term ordering, so `repo/dedup_extract.py` has something genuine to read back
    out in M2B — and so the agent's inferred contract can be compared against a
    hand-written one.
    """
    text = (FIXTURES / "snowflake_seed.sql").read_text()
    assert "qualify row_number() over (" in text.lower()
    assert "partition by CUSTOMER_ID" in text
    # The tiebreaker is what makes the ordering total.
    assert "SOURCE_UPDATED_AT desc nulls last, SOURCE_ROW_ID desc nulls last" in text


def test_snowflake_fixture_carries_its_deliberate_defects() -> None:
    """A fixture with no defects cannot demonstrate that any check works."""
    text = (FIXTURES / "snowflake_seed.sql").read_text()
    # customer 4's two rows must share a SOURCE_UPDATED_AT, or L2.6 has no tie to find.
    assert text.count("'2026-08-07 12:00:00'::timestamp_ntz") >= 2
    # customer 2 must change a tracked column, or SCD check 7 has nothing to assert.
    assert "'SMB', 'DE'" in text and "'ENTERPRISE', 'DE'" in text
    # a gold object with no silver counterpart, so coverage reports it as unvalidated
    assert "MART_CHURN" in text
    # non-additive measures, so the coverage report has something to declare unverified
    assert "AVG_BASKET" in text and "UNIQUE_BUYERS" in text


def test_snowflake_fixture_never_targets_a_production_database() -> None:
    """It drops and recreates everything it touches."""
    # Comments first: the fixture's own comments discuss CREATE DATABASE and the real
    # database name, so a raw text search finds the explanation rather than the code.
    sql = strip_comments((FIXTURES / "snowflake_seed.sql").read_text())
    # Every object is fully qualified into DPHM_TEST rather than relying on `use
    # database`, so warehouse/safety.py can verify each write target (it refuses
    # unqualified ones, since session context is invisible to a statement-level guard).
    assert "DPHM_TEST." in sql
    # It must NOT create the database: the dev role has no CREATE DATABASE on the
    # account, and asking for that privilege would widen the blast radius for no reason.
    assert "create database" not in sql.lower()
    assert "ANALYTICS" not in sql, "the fixture must not reference the real database"
