#!/usr/bin/env python
"""Provision the dev state store and a separate writer role. Needs ACCOUNTADMIN.

    source ~/.dphm/credentials.env && python scripts/provision_dev_roles.py

Why a separate writer role in *dev*, when one role would be less work: the reader/writer
split is the mechanism that makes "checks cannot write" true, and `config/models.py`
refuses a config where the two are the same role. Collapsing them in dev would mean the
thing being tested is not the thing that ships.

Every statement goes through `warehouse/safety.py`. The grants need
`allow_account_level=True`, passed explicitly here, so an account-level change is always
a visible line of code rather than an ambient permission.
"""

from __future__ import annotations

import os
import sys
from typing import Any

DATABASE = "DPHM_TEST"
READER = "DPHM_DEV"
WRITER = "DPHM_DEV_WRITER"
STATE = "DPHM_STATE"
SCRATCH = "DPHM_SCRATCH"

# (statement, needs_account_level)
STATEMENTS: list[tuple[str, bool]] = [
    # The tool's own two schemas, inside the sandbox database.
    (f"create schema if not exists {DATABASE}.{STATE}", False),
    # Zero retention: scratch holds transient parity landings and mutation clones, and
    # may briefly contain copied rows. Nothing in it is worth recovering.
    (
        f"create schema if not exists {DATABASE}.{SCRATCH} with data_retention_time_in_days = 0",
        False,
    ),
    # A writer role that can touch those two schemas and nothing else.
    (f"create role if not exists {WRITER}", True),
    (f"grant usage, operate on warehouse DPHM_TEST_WH to role {WRITER}", True),
    (f"grant usage on database {DATABASE} to role {WRITER}", True),
    (f"grant all on schema {DATABASE}.{STATE} to role {WRITER}", True),
    (f"grant all on schema {DATABASE}.{SCRATCH} to role {WRITER}", True),
    # The mutation gate clones a fixture table into scratch, so the writer needs to READ
    # the fixture. Reading is not writing; the clone's target is still scratch.
    (f"grant usage on all schemas in database {DATABASE} to role {WRITER}", True),
    (f"grant select on all tables in database {DATABASE} to role {WRITER}", True),
    (f"grant select on future tables in database {DATABASE} to role {WRITER}", True),
    # ── Narrow the READER to read-only on the medallion ───────────────────────────
    # `grant all on all schemas` in the original setup left DPHM_DEV able to write to
    # BRONZE/SILVER/GOLD. That is a real deviation from the production posture, where
    # DPHM_READER cannot write anywhere: the diagnostics panel flagged it, correctly.
    # Revoking it makes the dev environment a faithful rehearsal, so what gets tested is
    # what ships. The fixture is seeded by an elevated role instead (seed_fixture.py).
    (f"revoke all on schema {DATABASE}.BRONZE from role {READER}", True),
    (f"revoke all on schema {DATABASE}.SILVER from role {READER}", True),
    (f"revoke all on schema {DATABASE}.GOLD from role {READER}", True),
    (f"revoke create schema on database {DATABASE} from role {READER}", True),
    (f"revoke modify on database {DATABASE} from role {READER}", True),
    (f"grant usage on schema {DATABASE}.BRONZE to role {READER}", True),
    (f"grant usage on schema {DATABASE}.SILVER to role {READER}", True),
    (f"grant usage on schema {DATABASE}.GOLD to role {READER}", True),
    (f"grant select on all tables in database {DATABASE} to role {READER}", True),
    (f"grant select on future tables in database {DATABASE} to role {READER}", True),
    (f"grant select on all views in database {DATABASE} to role {READER}", True),
    # The reader must be able to read the state store back.
    (f"grant usage on schema {DATABASE}.{STATE} to role {READER}", True),
    (f"grant select on all tables in schema {DATABASE}.{STATE} to role {READER}", True),
    (f"grant select on future tables in schema {DATABASE}.{STATE} to role {READER}", True),
    # Both roles on the one service user; the session picks which to use.
    (f"grant role {WRITER} to user {os.environ.get('DPHM_SF_USER', 'DPHM_DEV_SVC')}", True),
    # ── Disable SECONDARY ROLES. This one is load-bearing. ───────────────────────
    # Snowflake defaults a new user to DEFAULT_SECONDARY_ROLES = ('ALL'), which makes
    # EVERY role granted to the user active in EVERY session, whatever the primary role
    # is. Verified on the live account: with primary role DPHM_DEV, the session reported
    # secondary roles "DPHM_DEV_WRITER,ACCOUNTADMIN" and could write anywhere.
    #
    # That makes "checks execute as DPHM_READER and cannot write" false — the central
    # safety claim — and it is invisible unless you call current_secondary_roles().
    # Production is affected identically: DPHM_SVC holds both DPHM_READER and
    # DPHM_WRITER, so without this the reader session carries writer privileges.
    (
        f"alter user {os.environ.get('DPHM_SF_USER', 'DPHM_DEV_SVC')} "
        "set default_secondary_roles = ()",
        True,
    ),
    # ── Ownership. Also load-bearing. ────────────────────────────────────────────
    # The first fixture seed ran as DPHM_DEV, so DPHM_DEV OWNS the medallion tables —
    # and ownership confers full control that revoking privileges cannot remove. Moving
    # ownership to the elevated role is what actually makes the reader read-only.
    ("grant ownership on schema DPHM_TEST.BRONZE to role ACCOUNTADMIN revoke current grants", True),
    ("grant ownership on schema DPHM_TEST.SILVER to role ACCOUNTADMIN revoke current grants", True),
    ("grant ownership on schema DPHM_TEST.GOLD to role ACCOUNTADMIN revoke current grants", True),
    (
        "grant ownership on all tables in database DPHM_TEST to role ACCOUNTADMIN "
        "revoke current grants",
        True,
    ),
    # Ownership transfer revoked the reader's grants along with it, so re-grant SELECT.
    (f"grant usage on schema {DATABASE}.BRONZE to role {READER}", True),
    (f"grant usage on schema {DATABASE}.SILVER to role {READER}", True),
    (f"grant usage on schema {DATABASE}.GOLD to role {READER}", True),
    (f"grant select on all tables in database {DATABASE} to role {READER}", True),
    (f"grant select on future tables in database {DATABASE} to role {READER}", True),
    (f"grant usage on schema {DATABASE}.{STATE} to role {READER}", True),
    (f"grant select on all tables in schema {DATABASE}.{STATE} to role {READER}", True),
    (f"grant select on future tables in schema {DATABASE}.{STATE} to role {READER}", True),
    (f"grant all on schema {DATABASE}.{STATE} to role {WRITER}", True),
    (f"grant all on schema {DATABASE}.{SCRATCH} to role {WRITER}", True),
    (f"grant select on all tables in database {DATABASE} to role {WRITER}", True),
    (f"grant usage on all schemas in database {DATABASE} to role {WRITER}", True),
    # TABLE-level privileges on the state store. `grant all on schema` covers schema
    # privileges (CREATE TABLE etc.) but not DML on the tables inside it — and the
    # ownership transfer above revoked what the writer held from having created them.
    (f"grant all on all tables in schema {DATABASE}.{STATE} to role {WRITER}", True),
    (f"grant all on future tables in schema {DATABASE}.{STATE} to role {WRITER}", True),
    (f"grant all on all tables in schema {DATABASE}.{SCRATCH} to role {WRITER}", True),
    (f"grant all on future tables in schema {DATABASE}.{SCRATCH} to role {WRITER}", True),
]


def main() -> int:
    import snowflake.connector as sf

    from dphm.warehouse.connection import guarded_execute
    from dphm.warehouse.safety import UnsafeStatementError

    for key in ("DPHM_SF_ACCOUNT", "DPHM_SF_USER", "DPHM_SF_KEY_PATH"):
        if not os.environ.get(key):
            sys.stderr.write(f"{key} is not set. Try: source ~/.dphm/credentials.env\n")
            return 2

    conn: Any = sf.connect(
        account=os.environ["DPHM_SF_ACCOUNT"],
        user=os.environ["DPHM_SF_USER"],
        private_key_file=os.environ["DPHM_SF_KEY_PATH"],
        # Explicit escalation, for this task only. The default role stays low-privilege.
        role=os.environ.get("DPHM_SF_ADMIN_ROLE", "ACCOUNTADMIN"),
        warehouse="DPHM_TEST_WH",
        database=DATABASE,
    )
    print(
        f"connected as {os.environ['DPHM_SF_USER']} / "
        f"{os.environ.get('DPHM_SF_ADMIN_ROLE', 'ACCOUNTADMIN')} (elevated, deliberately)"
    )
    try:
        cur = conn.cursor()
        cur.execute("alter session set query_tag = 'dphm:provision-dev-roles'")
        for sql, account_level in STATEMENTS:
            try:
                guarded_execute(
                    cur,
                    sql,
                    writable_roots={DATABASE, STATE, SCRATCH},
                    allow_account_level=account_level,
                    context="provision_dev_roles",
                )
            except UnsafeStatementError as exc:
                sys.stderr.write(f"\nGUARD REFUSED: {sql}\n{exc}\n")
                return 1
            except Exception as exc:
                sys.stderr.write(f"\nFAILED: {sql}\n{exc}\n")
                return 1
            print(f"  ok {sql[:88]}")
    finally:
        conn.close()

    print(f"\nProvisioned. {READER} reads; {WRITER} writes {STATE} and {SCRATCH} only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
