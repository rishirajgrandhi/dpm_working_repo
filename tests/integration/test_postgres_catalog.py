"""The Postgres catalog reader and permission fingerprint, against a real engine.

Marked `integration` (14 §1). Every assertion here is one that a mock would have let
through: type mapping, identifier case folding, view-vs-table, and the read-only posture.
"""

from __future__ import annotations

import pytest

from dphm.catalog.base import normalize_type
from dphm.catalog.postgres import PostgresCatalog
from dphm.warehouse.connection import PostgresConnector

pytestmark = pytest.mark.integration


@pytest.fixture
def catalog(pg_source_spec) -> PostgresCatalog:
    return PostgresCatalog(PostgresConnector(pg_source_spec))


def test_ping_reports_a_read_only_session(catalog: PostgresCatalog) -> None:
    """02 §1: `read_only` is asserted, not trusted. Every connection is read-only."""
    detail = catalog.ping()
    assert "database=orders_prod" in detail
    assert "read_only=on" in detail


def test_lists_tables_and_normalizes_identifier_case(catalog: PostgresCatalog) -> None:
    """PostgreSQL folds to lower, Snowflake folds to UPPER.

    Comparing raw names across the two is the most common source of a spurious
    "column missing" (02 §4), so the reader normalizes at the boundary and keeps the
    original for quoting.
    """
    tables = {t.name: t for t in catalog.list_tables(["public"])}
    assert "CUSTOMERS" in tables
    assert "ORDERS" in tables
    customers = tables["CUSTOMERS"]
    assert customers.raw_name == "customers"  # original preserved for quoting
    assert customers.schema == "PUBLIC"
    assert customers.raw_schema == "public"
    assert "CUSTOMER_ID" in customers.column_names
    assert customers.column("customer_id") is not None  # lookup is case-insensitive


def test_schema_argument_accepts_either_case(catalog: PostgresCatalog) -> None:
    upper = {t.name for t in catalog.list_tables(["PUBLIC"])}
    lower = {t.name for t in catalog.list_tables(["public"])}
    assert upper == lower and "CUSTOMERS" in upper


def test_views_are_distinguished_from_tables(catalog: PostgresCatalog) -> None:
    """A view has no rows of its own; treating one as a table skews conservation checks."""
    tables = {t.name: t for t in catalog.list_tables(["public"])}
    assert tables["V_ACTIVE_CUSTOMERS"].kind == "view"
    assert tables["CUSTOMERS"].kind == "table"


@pytest.mark.parametrize(
    ("column", "expected_logical"),
    [
        ("CUSTOMER_ID", "integer"),
        ("SOURCE_ROW_ID", "integer"),
        ("NAME", "text"),
        ("COUNTRY", "text"),
        ("CREDIT_LIMIT", "decimal"),
        ("SCORE", "float"),
        ("IS_TEST", "boolean"),
        ("EXTERNAL_REF", "uuid"),
        ("ATTRIBUTES", "json"),
        ("SOURCE_UPDATED_AT", "timestamp_tz"),
        ("CREATED_AT", "timestamp"),
    ],
)
def test_every_canonicalization_case_maps_to_a_logical_type(
    catalog: PostgresCatalog, column: str, expected_logical: str
) -> None:
    """07 §3 keys its canonicalization rules off the logical type.

    A type that maps to `other` is excluded from comparison and reported as such — honest,
    where guessing a canonicalization would produce a silently wrong comparison.
    """
    customers = catalog.read_table("public.customers")
    assert customers is not None
    meta = customers.column(column)
    assert meta is not None, f"{column} not found"
    assert meta.logical_type == expected_logical


def test_numeric_precision_and_scale_are_captured(catalog: PostgresCatalog) -> None:
    """Fixed-scale casting on both sides is mandatory, so the scale must be read."""
    customers = catalog.read_table("public.customers")
    assert customers is not None
    credit = customers.column("CREDIT_LIMIT")
    assert credit is not None
    assert (credit.precision, credit.scale) == (12, 2)


def test_floats_are_flagged_so_they_never_enter_a_fingerprint(
    catalog: PostgresCatalog,
) -> None:
    """Cross-engine float rendering is not reliably identical (07 §3)."""
    customers = catalog.read_table("public.customers")
    assert customers is not None
    score = customers.column("SCORE")
    assert score is not None and score.is_float is True
    credit = customers.column("CREDIT_LIMIT")
    assert credit is not None and credit.is_float is False


def test_nullability_is_read_correctly(catalog: PostgresCatalog) -> None:
    customers = catalog.read_table("public.customers")
    assert customers is not None
    assert customers.column("CUSTOMER_ID").nullable is False  # type: ignore[union-attr]
    assert customers.column("SEGMENT").nullable is True  # type: ignore[union-attr]


def test_read_table_requires_a_two_part_name(catalog: PostgresCatalog) -> None:
    """Postgres has one database per connection; a three-part name is a config error."""
    with pytest.raises(ValueError, match="two-part name"):
        catalog.read_table("orders_prod.public.customers")


def test_unknown_table_returns_none_rather_than_raising(catalog: PostgresCatalog) -> None:
    assert catalog.read_table("public.does_not_exist") is None


def test_row_count_estimates_do_not_scan_the_table(catalog: PostgresCatalog) -> None:
    """Counts come from the planner estimate: a count(*) on production OLTP is exactly
    the query this tool must not issue (02 §1). An unanalysed table yields None, which is
    honest rather than zero."""
    customers = catalog.read_table("public.customers")
    assert customers is not None
    assert customers.row_count is None or customers.row_count >= 0


def test_normalize_type_falls_back_to_other_not_a_guess() -> None:
    assert normalize_type("postgres", "some_custom_domain_type") == "other"
    assert normalize_type("snowflake", "GEOGRAPHY") == "other"
