# What I need to build and test this end to end

There are **two completely separate access requests** for this project, and conflating
them is the main risk in this document.

| | Production access | Development access |
|---|---|---|
| Purpose | What the finished tool uses when it monitors your real pipeline | What I need to build and test the tool |
| Posture | **Read-only.** Cannot write to RDS at all; in Snowflake writes only to its own two schemas | **Full DDL** — I create tables, seed fake data, and deliberately corrupt it |
| Points at | Your real databases | A throwaway database that contains no real data |
| Documented in | [PROVISIONING.md](PROVISIONING.md) | This file |

**Nothing in this file should ever be granted against a production database.** The
fixture scripts drop and recreate everything they touch, and the mutation tests exist
specifically to corrupt data on purpose.

---

## 1. The short version

I need **one thing**: a throwaway Snowflake database I can create and destroy tables in.

Everything else I have already solved locally — see §4.

---

## 2. Snowflake development access

### 2.1 What to create

Best option is a **separate Snowflake trial account**, which is free and has zero blast
radius. If that is inconvenient, a dedicated database inside your existing account works,
provided the role cannot see anything else.

```sql
-- A database that exists only for testing. It will be dropped and recreated often.
create database if not exists DPHM_TEST;

-- A small warehouse, with a hard spend cap so a mistake of mine cannot become a bill.
create warehouse if not exists DPHM_TEST_WH
    warehouse_size      = xsmall
    auto_suspend        = 60
    auto_resume         = true
    initially_suspended = true;

create resource monitor if not exists DPHM_TEST_MONITOR
    with credit_quota = 10          -- deliberately small
    frequency         = monthly
    start_timestamp   = immediately
    triggers on 90 percent  do notify
             on 100 percent do suspend;
alter warehouse DPHM_TEST_WH set resource_monitor = DPHM_TEST_MONITOR;

-- A role with full rights on that database and NOTHING ELSE.
create role if not exists DPHM_DEV;
grant usage, operate on warehouse DPHM_TEST_WH to role DPHM_DEV;
grant all on database DPHM_TEST to role DPHM_DEV;
grant all on all schemas in database DPHM_TEST to role DPHM_DEV;
grant all on future schemas in database DPHM_TEST to role DPHM_DEV;

-- Cost and query history, so I can verify the tool's own cost accounting works.
grant imported privileges on database SNOWFLAKE to role DPHM_DEV;

create user if not exists DPHM_DEV_SVC
    default_role      = DPHM_DEV
    default_warehouse = DPHM_TEST_WH
    rsa_public_key    = '<public key, header/footer lines removed>';
grant role DPHM_DEV to user DPHM_DEV_SVC;
```

### 2.2 Confirm the blast radius is what you expect

```sql
-- Should list DPHM_TEST and nothing else. If it lists a real database, stop.
show grants to role DPHM_DEV;
```

### 2.3 Why full DDL is genuinely required, not convenient

Three things cannot be tested any other way:

**The mutation gate.** The tool's central claim is that a generated check has been *proven
able to fail*. Proving it means cloning a table, injecting the exact bug the check exists
to catch, and confirming the check goes red with the right error on the right row. That
needs `CREATE TABLE ... CLONE` and then `UPDATE`/`INSERT`/`DELETE` on the clone. Without
it, no check can ever be activated — the gate is not decorative.

**The fixture pipeline.** A test fixture that inserts the *correct* answer directly proves
nothing, because a broken check would still pass. So the fixture has to be a real
pipeline: a genuine `QUALIFY ROW_NUMBER()` dedup, a genuine SCD2 merge. That means
creating and populating tables across three schemas.

**Zero-copy clone behaviour.** It is a Snowflake-specific feature the design depends on
for cost. I cannot verify it works cheaply by reading documentation.

---

## 3. What I will do with it

Already written and waiting (see §4 — the PostgreSQL half of this already runs):

- `tests/fixtures/snowflake_seed.sql` — builds BRONZE / SILVER / GOLD in `DPHM_TEST`,
  populated through a real pipeline, carrying deliberate defects:
  - three customers with duplicate rows
  - one customer whose duplicates **tie** on the full sort order, so the
    "is this pipeline reproducible?" check has something to find
  - one row with a null business key, which belongs in a reject table
  - one test account, which should be filtered out by a *declared* rule rather than
    vanishing silently
  - one customer who changes from SMB to ENTERPRISE — the exact story the whole product
    exists to catch
  - a gold table with no upstream counterpart, so coverage reporting has something honest
    to declare as unverifiable
- Then, per milestone: apply the state-store schema, run the check engine against the
  fixture, and run the mutation tests that prove each check can fail.

---

## 4. What I have already solved, and do not need from you

I expected to need help with these. I do not.

| Gap | Resolved by |
|---|---|
| Python 3.11 (this machine had 3.9) | Installed a standalone 3.11.15 toolchain via `uv`. The full stack now runs on it — 144 tests, `mypy --strict` clean |
| PostgreSQL for the RDS side | The `pgserver` package bundles a real PostgreSQL binary. **A real server is running locally with no Docker and no Homebrew** |
| Docker for integration tests | Not needed — see above. The Dockerfile still needs a real build somewhere before deployment |

So **the entire RDS half is already tested end to end against a real PostgreSQL server.**
That includes proving the tool cannot write to it: eight tests issue `INSERT`, `UPDATE`,
`DELETE`, `TRUNCATE`, `CREATE`, `CREATE TEMP`, `DROP` and `ALTER` on a live connection and
assert every one is refused. That is the product's first principle, tested rather than
asserted.

Snowflake is the one thing I cannot substitute. There is no local Snowflake.

---

## 5. Details to send me

Safe in a message or ticket:

- Snowflake account identifier (the `org-account` form)
- Confirmation of the database name if you used something other than `DPHM_TEST`
- Whether this is a separate trial account or a database inside your main account

**Do not put the private key in a message.** Put it on disk where the service can read it
and tell me the path, or place it in your secret manager. The tool refuses to start if it
finds a credential written into a config file.

```bash
# Environment variables I will read
export DPHM_SF_ACCOUNT=<org-account>
export DPHM_SF_USER=DPHM_DEV_SVC
export DPHM_SF_KEY_PATH=/path/to/dphm_dev_key.p8
```

---

## 6. Teardown

When you want it gone:

```sql
drop database if exists DPHM_TEST;
drop warehouse if exists DPHM_TEST_WH;
drop resource monitor if exists DPHM_TEST_MONITOR;
drop user if exists DPHM_DEV_SVC;
drop role if exists DPHM_DEV;
```

Nothing outside those five objects is ever touched.
