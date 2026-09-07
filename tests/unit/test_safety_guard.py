"""The destructive-statement guard.

While building the tool the operator holds elevated Snowflake access. At that point the
GRANT is no longer the thing protecting production — this is. So it gets tested harder
than most modules, including the cases where being wrong is unrecoverable.

The load-bearing property is that it **fails closed**: a destructive statement whose
target cannot be identified is refused, not permitted.
"""

from __future__ import annotations

import pytest

from dphm.warehouse.safety import (
    UnsafeStatementError,
    guard_script,
    guard_statement,
    is_destructive,
)

# ── reads are always fine ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "select * from ANALYTICS.GOLD.DIM_CUSTOMER",
        "select count(*) from PROD.PUBLIC.ORDERS where x = 1",
        "show grants to role DPHM_READER",
        "describe table ANALYTICS.GOLD.FCT_ORDER",
        "with x as (select 1) select * from x",
        "explain select * from ANALYTICS.GOLD.T",
    ],
)
def test_reads_pass(sql: str) -> None:
    guard_statement(sql)
    assert is_destructive(sql) is None


# ── the cases that matter: destroying someone else's data ───────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "drop table ANALYTICS.GOLD.DIM_CUSTOMER",
        "drop database ANALYTICS",
        "drop schema ANALYTICS.GOLD",
        "truncate table ANALYTICS.GOLD.FCT_ORDER",
        "delete from ANALYTICS.GOLD.FCT_ORDER where 1=1",
        "update ANALYTICS.GOLD.DIM_CUSTOMER set SEGMENT = 'X'",
        "insert into ANALYTICS.GOLD.T select 1",
        "merge into ANALYTICS.GOLD.T using S on T.id = S.id when matched then update set a = 1",
        "copy into ANALYTICS.GOLD.T from @stage",
        "alter table ANALYTICS.GOLD.T add column injected int",
        # The one that hides: "create" in the name, but it drops the existing object.
        "create or replace table ANALYTICS.GOLD.DIM_CUSTOMER (id number)",
        "create or replace view PROD.PUBLIC.V as select 1",
        "undrop table ANALYTICS.GOLD.T",
    ],
)
def test_destroying_production_is_refused(sql: str) -> None:
    with pytest.raises(UnsafeStatementError, match="outside"):
        guard_statement(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "drop table DPHM_TEST.BRONZE.CUSTOMERS",
        "create or replace table DPHM_TEST.SILVER.CUSTOMERS as select 1",
        "truncate table DPHM_TEST.GOLD.FCT_ORDER",
        "insert into DPHM_TEST.BRONZE.CUSTOMERS select 1",
        "delete from DPHM_STATE.CHECK_RESULTS where RUN_ID = 'x'",
        "create or replace table DPHM_SCRATCH.MUT_ABC clone DPHM_TEST.BRONZE.CUSTOMERS",
        "update DPHM_SCRATCH.MUT_ABC set NAME = 'mutated'",
    ],
)
def test_writing_to_our_own_objects_is_allowed(sql: str) -> None:
    guard_statement(sql)


def test_the_mutation_test_pattern_is_allowed() -> None:
    """Gate 3 clones a table into scratch and corrupts the clone. That must work."""
    guard_statement("create or replace table DPHM_SCRATCH.MUT_X clone DPHM_TEST.GOLD.DIM_CUSTOMER")
    guard_statement("update DPHM_SCRATCH.MUT_X set SEGMENT = 'ENTERPRISE' where CUSTOMER_ID = 2")
    guard_statement("drop table DPHM_SCRATCH.MUT_X")


def test_cloning_from_production_into_scratch_is_allowed_not_the_reverse() -> None:
    """Reading production to clone is fine. Replacing production is not."""
    guard_statement("create or replace table DPHM_SCRATCH.C clone ANALYTICS.GOLD.T")
    with pytest.raises(UnsafeStatementError):
        guard_statement("create or replace table ANALYTICS.GOLD.T clone DPHM_SCRATCH.C")


# ── fail closed ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "drop table CUSTOMERS",  # unqualified — which database?
        "truncate table ORDERS",
        "drop schema GOLD",
    ],
)
def test_an_unqualified_destructive_target_is_refused(sql: str) -> None:
    """The guard refuses what it cannot verify. An unparseable DROP is exactly the case
    where guessing is unrecoverable."""
    with pytest.raises(UnsafeStatementError, match=r"could not be identified|outside"):
        guard_statement(sql)


def test_a_destructive_statement_with_no_identifiable_target_is_refused() -> None:
    with pytest.raises(UnsafeStatementError, match="could not be identified"):
        guard_statement("delete from")


# ── account-level statements ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "alter account set statement_timeout_in_seconds = 60",
        "drop user SOMEONE_ELSE",
        "drop role ANALYST",
        "drop warehouse PROD_WH",
        "create user INTRUDER password = 'x'",
        "grant role accountadmin to user SOMEONE",
        "alter user ADMIN set password = 'x'",
        "grant ownership on database ANALYTICS to role X",
    ],
)
def test_account_level_statements_are_refused_by_default(sql: str) -> None:
    """Even with ACCOUNTADMIN held. The role says what Snowflake would permit; this says
    what we are willing to ask for."""
    with pytest.raises(UnsafeStatementError):
        guard_statement(sql)


def test_account_level_requires_an_explicit_per_call_opt_in() -> None:
    """Provisioning is legitimate — it just has to be a deliberate line of code."""
    guard_statement("create warehouse DPHM_WH warehouse_size = xsmall", allow_account_level=True)
    guard_statement(
        "grant usage on warehouse DPHM_WH to role DPHM_READER", allow_account_level=True
    )


def test_grants_are_refused_without_the_opt_in() -> None:
    with pytest.raises(UnsafeStatementError, match="account-level"):
        guard_statement("grant all on database ANALYTICS to role PUBLIC")


# ── scripts ──────────────────────────────────────────────────────────────────


def test_a_script_is_checked_entirely_before_anything_runs() -> None:
    """A script half-applied because statement nine was refused is worse than one that
    never ran at all."""
    script = """
        create or replace table DPHM_TEST.BRONZE.T (id number);
        insert into DPHM_TEST.BRONZE.T select 1;
        drop table ANALYTICS.GOLD.DIM_CUSTOMER;   -- the bad one, last
    """
    with pytest.raises(UnsafeStatementError, match="statement 3"):
        guard_script(script)


def test_the_real_fixture_script_passes_the_guard() -> None:
    """The seed script must be safe by construction, not by inspection."""
    from pathlib import Path

    fixture = (Path(__file__).resolve().parents[1] / "fixtures" / "snowflake_seed.sql").read_text()
    guard_script(fixture, context="snowflake_seed.sql")


def test_the_state_ddl_passes_the_guard() -> None:
    from pathlib import Path

    ddl = (Path(__file__).resolve().parents[2] / "src" / "dphm" / "state" / "ddl.sql").read_text()
    guard_script(ddl, writable_roots={"DPHM_STATE", "DPHM_SCRATCH"}, context="ddl.sql")


def test_comments_cannot_smuggle_a_statement_past_the_guard() -> None:
    """The splitter strips comments, so a semicolon inside one cannot hide a statement."""
    script = "select 1; -- harmless; drop table ANALYTICS.GOLD.T\nselect 2;"
    guard_script(script)


def test_case_and_whitespace_do_not_bypass_it() -> None:
    for sql in [
        "DROP   TABLE   ANALYTICS.GOLD.T",
        "\n\t drop table analytics.gold.t ",
        "DrOp TaBlE Analytics.Gold.T",
    ]:
        with pytest.raises(UnsafeStatementError):
            guard_statement(sql)
