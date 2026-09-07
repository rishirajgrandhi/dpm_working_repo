#!/usr/bin/env python
"""Verify the Snowflake dev account is reachable and correctly scoped.

    DPHM_SF_ACCOUNT=<org-account> python scripts/verify_snowflake.py

Checks, in order, stopping at the first hard failure:

1. We can connect at all, with key-pair auth.
2. The role and warehouse are what we expect.
3. We can create and drop a table in DPHM_TEST — the mutation gate needs this.
4. Zero-copy clone works, which is what makes gate 3 cheap on a large table.
5. The role can see DPHM_TEST and *nothing else* — the blast radius is what we agreed.
6. ACCOUNT_USAGE is readable, so cost accounting can be verified later.

Step 5 is the one worth watching. If it lists a database that is not DPHM_TEST, the role
is broader than intended and I would rather fail here than discover it by writing
somewhere I should not.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_KEY = Path.home() / ".dphm" / "keys" / "dphm_dev_key.p8"
EXPECTED_DB = "DPHM_TEST"


def main() -> int:
    account = os.environ.get("DPHM_SF_ACCOUNT")
    user = os.environ.get("DPHM_SF_USER", "DPHM_DEV_SVC")
    key_path = Path(os.environ.get("DPHM_SF_KEY_PATH", str(DEFAULT_KEY)))

    if not account:
        sys.stderr.write(
            "DPHM_SF_ACCOUNT is not set. It is the org-account identifier, e.g.\n"
            "  export DPHM_SF_ACCOUNT=myorg-ab12345\n"
        )
        return 2
    if not key_path.exists():
        sys.stderr.write(f"private key not found at {key_path}\n")
        return 2

    try:
        import snowflake.connector as sf
    except ImportError:
        sys.stderr.write("snowflake-connector-python is not installed\n")
        return 2

    print(f"connecting as {user} to {account} …")
    try:
        conn = sf.connect(
            account=account,
            user=user,
            private_key_file=str(key_path),
            role="DPHM_DEV",
            warehouse="DPHM_TEST_WH",
            database=EXPECTED_DB,
            login_timeout=30,
        )
    except Exception as exc:
        sys.stderr.write(f"\nFAILED to connect: {type(exc).__name__}: {exc}\n")
        return 1

    failures: list[str] = []
    try:
        cur = conn.cursor()

        cur.execute(
            "select current_role(), current_warehouse(), current_database(), current_version()"
        )
        role, warehouse, database, version = cur.fetchone()
        print(f"  ok   connected · role={role} warehouse={warehouse} db={database}")
        print(f"       snowflake {version}")
        if role != "DPHM_DEV":
            failures.append(f"expected role DPHM_DEV, got {role}")
        if database != EXPECTED_DB:
            failures.append(f"expected database {EXPECTED_DB}, got {database}")

        # 3. DDL — the mutation gate cannot exist without this.
        cur.execute("create schema if not exists DPHM_TEST.PROBE")
        cur.execute("create or replace table DPHM_TEST.PROBE.T (ID number, TXT varchar)")
        cur.execute("insert into DPHM_TEST.PROBE.T select 1, 'a'")
        cur.execute("select count(*) from DPHM_TEST.PROBE.T")
        assert cur.fetchone()[0] == 1
        print("  ok   create / insert / select in DPHM_TEST")

        # 4. Zero-copy clone — what makes gate 3 affordable on a large table.
        cur.execute("create or replace table DPHM_TEST.PROBE.T_CLONE clone DPHM_TEST.PROBE.T")
        cur.execute("update DPHM_TEST.PROBE.T_CLONE set TXT = 'mutated' where ID = 1")
        cur.execute("select TXT from DPHM_TEST.PROBE.T where ID = 1")
        original = cur.fetchone()[0]
        cur.execute("select TXT from DPHM_TEST.PROBE.T_CLONE where ID = 1")
        mutated = cur.fetchone()[0]
        if original == "a" and mutated == "mutated":
            print("  ok   zero-copy clone + mutate, original untouched (gate 3 works)")
        else:
            failures.append(f"clone isolation broken: original={original} clone={mutated}")

        # 5. Blast radius.
        cur.execute("show databases")
        rows = cur.fetchall()
        name_idx = [c[0].upper() for c in cur.description].index("NAME")
        visible = {str(r[name_idx]).upper() for r in rows} - {"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA"}
        if visible <= {EXPECTED_DB}:
            print(f"  ok   blast radius confirmed · role sees only {sorted(visible)}")
        else:
            extra = sorted(visible - {EXPECTED_DB})
            failures.append(
                f"role can see databases beyond {EXPECTED_DB}: {extra}. "
                "Broader than intended — do not seed fixtures until this is narrowed."
            )

        # 6. ACCOUNT_USAGE. Degraded, not fatal.
        try:
            cur.execute("select count(*) from SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY limit 1")
            cur.fetchone()
            print("  ok   ACCOUNT_USAGE readable (cost accounting verifiable)")
        except Exception as exc:
            print(f"  warn ACCOUNT_USAGE not readable: {type(exc).__name__}")
            print("       Not fatal. Cost reporting degrades; everything else works.")
            print("       Note it also lags 45min-3h, so it is empty on a brand-new account.")

        cur.execute("drop schema if exists DPHM_TEST.PROBE")
        print("  ok   cleaned up probe schema")
    finally:
        conn.close()

    print()
    if failures:
        for f in failures:
            sys.stderr.write(f"FAIL {f}\n")
        return 1
    print("All checks passed. Ready to seed the fixture pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
