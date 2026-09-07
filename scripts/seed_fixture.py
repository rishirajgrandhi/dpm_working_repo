#!/usr/bin/env python
"""Seed (or reseed) the Snowflake fixture pipeline in DPHM_TEST.

    source ~/.dphm/env.sh && python scripts/seed_fixture.py [--verify-only]

Idempotent: every statement is CREATE OR REPLACE, so rerunning gives a clean fixture.

It does NOT create the database — the dev role has no CREATE DATABASE on the account, by
design. `scripts/setup_dev_snowflake.sql` provisions that once, as ACCOUNTADMIN.

After seeding it verifies the fixture actually carries its deliberate defects. A fixture
that has quietly lost its defects still passes every check, which would make the whole
suite look green while testing nothing.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

SEED = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "snowflake_seed.sql"


def connect() -> Any:
    import snowflake.connector as sf

    missing = [
        k for k in ("DPHM_SF_ACCOUNT", "DPHM_SF_USER", "DPHM_SF_KEY_PATH") if not os.environ.get(k)
    ]
    if missing:
        sys.stderr.write(f"missing env vars: {missing}. Try: source ~/.dphm/env.sh\n")
        raise SystemExit(2)
    return sf.connect(
        account=os.environ["DPHM_SF_ACCOUNT"],
        user=os.environ["DPHM_SF_USER"],
        private_key_file=os.environ["DPHM_SF_KEY_PATH"],
        # Elevated deliberately: the fixture CREATEs in BRONZE/SILVER/GOLD, which the
        # reader role cannot do — and must not, or the dev environment would not mirror
        # production. Building the fixture is a provisioning act, not a monitoring one.
        role=os.environ.get("DPHM_SF_ADMIN_ROLE", "ACCOUNTADMIN"),
        warehouse=os.environ.get("DPHM_SF_WAREHOUSE", "DPHM_TEST_WH"),
        database=os.environ.get("DPHM_SF_DATABASE", "DPHM_TEST"),
    )


def seed(cur: Any) -> None:
    from dphm.util.sql import split_statements

    statements = split_statements(SEED.read_text())
    print(f"applying {len(statements)} statements from {SEED.name}")
    started = time.monotonic()
    for index, statement in enumerate(statements, 1):
        try:
            cur.execute(statement)
        except Exception as exc:
            head = " ".join(statement.split()[:8])
            sys.stderr.write(f"\nFAILED at statement {index}: {head}\n{exc}\n")
            raise SystemExit(1) from exc
    print(f"seeded in {time.monotonic() - started:.1f}s")


# (description, sql, expected) — each defect the fixture must carry.
CHECKS: list[tuple[str, str, Any]] = [
    ("bronze keeps duplicates", "select count(*) from BRONZE.CUSTOMERS", 10),
    ("bronze distinct keys", "select count(distinct CUSTOMER_ID) from BRONZE.CUSTOMERS", 7),
    ("silver is deduplicated", "select count(*) from SILVER.CUSTOMERS", 5),
    (
        "L2.1 no duplicate survived",
        "select count(*) from (select CUSTOMER_ID from SILVER.CUSTOMERS "
        "group by 1 having count(*) > 1)",
        0,
    ),
    (
        "L2.5 the RIGHT duplicate survived (customer 3)",
        "select SOURCE_ROW_ID from SILVER.CUSTOMERS where CUSTOMER_ID = 3",
        1005,
    ),
    (
        "L2.6 an unresolved tie EXISTS to be caught",
        "select count(*) from (select CUSTOMER_ID from BRONZE.CUSTOMERS "
        "where NAME is not null and NAME <> '' and IS_TEST = false "
        "group by CUSTOMER_ID, SOURCE_UPDATED_AT having count(*) > 1)",
        1,
    ),
    ("L2.8 one row rejected with a reason", "select count(*) from SILVER.CUSTOMERS_REJECT", 1),
    (
        "L2.8 every reject reason is in the closed set",
        "select count(*) from SILVER.CUSTOMERS_REJECT where REJECT_REASON not in "
        "('NULL_BUSINESS_KEY', 'CAST_FAILED', 'FAILED_DOMAIN_RULE')",
        0,
    ),
    (
        "SCD 1: exactly one current row per key",
        "select count(*) from (select CUSTOMER_ID from GOLD.DIM_CUSTOMER "
        "where IS_CURRENT group by 1 having count(*) <> 1)",
        0,
    ),
    (
        "SCD: customer 2 has TWO versions (the tracked change opened one)",
        "select count(*) from GOLD.DIM_CUSTOMER where CUSTOMER_ID = 2",
        2,
    ),
    (
        "L3.F5 as-of FK: the pre-change order resolves to the OLD version",
        "select d.SEGMENT from GOLD.FCT_ORDER f join GOLD.DIM_CUSTOMER d "
        "on d.CUSTOMER_SK = f.CUSTOMER_SK where f.ORDER_ID = 9003",
        "SMB",
    ),
    (
        "L3.F5 as-of FK: the post-change order resolves to the NEW version",
        "select d.SEGMENT from GOLD.FCT_ORDER f join GOLD.DIM_CUSTOMER d "
        "on d.CUSTOMER_SK = f.CUSTOMER_SK where f.ORDER_ID = 9004",
        "ENTERPRISE",
    ),
    ("L3.F5 no unresolved FKs", "select count(*) from GOLD.FCT_ORDER where CUSTOMER_SK is null", 0),
    (
        "an unvalidatable gold object exists (no silver counterpart)",
        "select count(*) from DPHM_TEST.INFORMATION_SCHEMA.TABLES "
        "where TABLE_SCHEMA = 'GOLD' and TABLE_NAME = 'MART_CHURN'",
        1,
    ),
]


def verify(cur: Any) -> int:
    print("\nverifying the fixture still carries its deliberate defects:")
    failures = 0
    for description, sql, expected in CHECKS:
        cur.execute(sql)
        row = cur.fetchone()
        actual = row[0] if row else None
        ok = actual == expected
        print(
            f"  {'ok  ' if ok else 'FAIL'} {description}: {actual!r}"
            + ("" if ok else f" (expected {expected!r})")
        )
        failures += 0 if ok else 1

    # Conservation is the one worth computing rather than asserting a constant.
    cur.execute(
        """
        select (select count(*) from BRONZE.CUSTOMERS)
             - (select count(*) from SILVER.CUSTOMERS)
             - (select count(*) from SILVER.CUSTOMERS_REJECT)
             - (select count(*) from BRONZE.CUSTOMERS where IS_TEST = true)
             - (select count(*) - count(distinct CUSTOMER_ID) from BRONZE.CUSTOMERS
                where NAME is not null and NAME <> '' and IS_TEST = false)
        """
    )
    unexplained = cur.fetchone()[0]
    ok = unexplained == 0
    print(f"  {'ok  ' if ok else 'FAIL'} L2.4 conservation closes: unexplained = {unexplained}")
    failures += 0 if ok else 1
    return failures


def main() -> int:
    verify_only = "--verify-only" in sys.argv
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("alter session set query_tag = 'dphm:seed-fixture'")
        if not verify_only:
            seed(cur)
        failures = verify(cur)
    finally:
        conn.close()

    print()
    if failures:
        sys.stderr.write(
            f"{failures} fixture check(s) failed. A fixture that has lost its defects "
            "passes every check while testing nothing.\n"
        )
        return 1
    print("Fixture is correct and carries every defect it should.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
