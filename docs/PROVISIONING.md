# Provisioning request — what dphm needs, and why

Hand this to whoever owns Snowflake and RDS. Every grant below is the minimum the tool
needs, and the reasoning is stated so a reviewer can push back on any single line.

**Two things this tool will never do**, and the grants are shaped so it *cannot*:

* It never writes to RDS. The role has no `INSERT`/`UPDATE`/`DELETE` and cannot create
  even a temp table.
* In Snowflake it writes only to its own two schemas, `DPHM_STATE` and `DPHM_SCRATCH`.

The tool checks its own grants at startup and refuses to run if it finds a write
privilege it should not have. A grant change is reported as a permission event, never as
data loss — a revoked grant otherwise looks exactly like a table full of missing rows.

---

## 1. Snowflake

### 1.1 A dedicated warehouse

Separate so monitoring cost is attributable, and so monitoring can never queue behind or
starve production queries.

```sql
create warehouse if not exists DPHM_WH
    warehouse_size       = xsmall
    auto_suspend         = 60
    auto_resume          = true
    initially_suspended  = true
    comment = 'Data Pipeline Health Monitor. Monitoring only.';

-- A hard ceiling, so a runaway check cannot become a surprise bill.
create resource monitor if not exists DPHM_MONITOR
    with credit_quota = 50
    frequency         = monthly
    start_timestamp   = immediately
    triggers on 80 percent  do notify
             on 100 percent do suspend;

alter warehouse DPHM_WH set resource_monitor = DPHM_MONITOR;
```

Adjust `credit_quota` to taste — 50 is a starting guess, and the tool reports its own
spend so you can tune it after a week.

### 1.2 Two roles

```sql
create role if not exists DPHM_READER;
create role if not exists DPHM_WRITER;

grant usage, operate on warehouse DPHM_WH to role DPHM_READER;
grant usage, operate on warehouse DPHM_WH to role DPHM_WRITER;
```

### 1.3 Reader: read-only on what we monitor

Replace `ANALYTICS` and the schema list with your real names.

```sql
grant usage on database ANALYTICS to role DPHM_READER;

-- One block per monitored schema. FUTURE grants matter: without them, a table added
-- next month is invisible to the tool and silently uncovered.
grant usage  on schema ANALYTICS.BRONZE to role DPHM_READER;
grant select on all tables    in schema ANALYTICS.BRONZE to role DPHM_READER;
grant select on future tables in schema ANALYTICS.BRONZE to role DPHM_READER;
grant select on all views     in schema ANALYTICS.BRONZE to role DPHM_READER;
grant select on future views  in schema ANALYTICS.BRONZE to role DPHM_READER;

-- repeat for SILVER, GOLD, and any staging schema
```

### 1.4 Reader: account usage

Used for cost attribution and for root-cause evidence (load history, query history).

```sql
grant imported privileges on database SNOWFLAKE to role DPHM_READER;
```

If that is too broad for your policy, the specific views needed are
`ACCOUNT_USAGE.QUERY_HISTORY`, `LOAD_HISTORY`, `COPY_HISTORY` and `TABLES`. Without any
of it the tool still works — it loses cost reporting and its best diagnostic signals, and
says so rather than pretending.

**Worth knowing:** `ACCOUNT_USAGE` views lag by 45 minutes to 3 hours. Cost figures for a
run appear later than the run itself. That is expected, not a bug.

### 1.5 Writer: its own two schemas, and nothing else

```sql
create schema if not exists ANALYTICS.DPHM_STATE;

-- Scratch holds transient parity landings and mutation-test clones. Zero retention
-- because nothing in it is worth recovering, and it may briefly hold copied rows.
create schema if not exists ANALYTICS.DPHM_SCRATCH
    with data_retention_time_in_days = 0;

grant usage on database ANALYTICS to role DPHM_WRITER;
grant all   on schema ANALYTICS.DPHM_STATE   to role DPHM_WRITER;
grant all   on schema ANALYTICS.DPHM_SCRATCH to role DPHM_WRITER;

-- The app reads its own state back as the reader role.
grant usage  on schema ANALYTICS.DPHM_STATE to role DPHM_READER;
grant select on all tables    in schema ANALYTICS.DPHM_STATE to role DPHM_READER;
grant select on future tables in schema ANALYTICS.DPHM_STATE to role DPHM_READER;
```

### 1.6 One decision to make: role inheritance

```sql
grant role DPHM_READER to role DPHM_WRITER;
```

**Why it is needed.** The mutation test — the thing that proves a generated check can
actually fail — makes a zero-copy clone of a production table into `DPHM_SCRATCH`,
injects a specific bug, and confirms the check goes red. Cloning needs `SELECT` on the
source *and* `CREATE TABLE` on the target in the same session, so the writer role needs
read access to the monitored schemas.

**What it costs.** It slightly weakens the separation: the writer role can then read
production data, not just write to its own schemas. It still cannot write anywhere
outside `DPHM_STATE` and `DPHM_SCRATCH`.

**If you would rather not.** Skip this grant and the mutation gate cannot run. That is a
real loss, not a formality — no check can be activated without it, because a check that
has never been proven able to fail manufactures confidence. If this is a blocker, tell me
and we can point the mutation tests at a dedicated test database with copied data
instead.

### 1.7 A service user, key-pair only

No password anywhere in the target path.

```bash
# Generate the key pair. Keep the private key in your secret manager.
openssl genrsa -out dphm_key.pem 2048
openssl rsa -in dphm_key.pem -pubout -out dphm_key.pub
```

```sql
create user if not exists DPHM_SVC
    default_role      = DPHM_READER
    default_warehouse = DPHM_WH
    rsa_public_key    = '<contents of dphm_key.pub, header/footer lines removed>'
    comment = 'Data Pipeline Health Monitor service account. Key-pair auth only.';

grant role DPHM_READER to user DPHM_SVC;
grant role DPHM_WRITER to user DPHM_SVC;
```

If your Snowflake edition supports it, add `type = service` so the account cannot be used
for interactive login.

### 1.8 Verify it

```sql
-- Expect SELECT/USAGE on monitored schemas, and write privileges ONLY on the two
-- DPHM_ schemas. Anything else is a finding.
show grants to role DPHM_READER;
show grants to role DPHM_WRITER;
```

---

## 2. AWS RDS (PostgreSQL 13+)

Read-only, and used **only** for source-to-bronze comparison. The rest of the tool never
touches RDS.

### 2.1 The role

```sql
create role dphm_reader with login;

grant connect on database orders_prod to dphm_reader;
grant usage   on schema public         to dphm_reader;
grant select  on all tables in schema public to dphm_reader;

-- Without this, a table added later is unreadable and silently uncovered.
alter default privileges in schema public grant select on tables to dphm_reader;

-- Make sure nothing was inherited from PUBLIC.
revoke create on schema public from dphm_reader;
revoke all on database orders_prod from public;  -- only if your policy allows
```

### 2.2 Guardrails at the role level

This is a production OLTP database. The tool sets a statement timeout and a read-only
transaction on every connection, but setting it on the role too means it holds even if
the tool is misconfigured.

```sql
alter role dphm_reader set statement_timeout = '120s';
alter role dphm_reader set default_transaction_read_only = on;
alter role dphm_reader set idle_in_transaction_session_timeout = '60s';
alter role dphm_reader set lock_timeout = '5s';
```

### 2.3 Authentication

IAM authentication is preferred over a password. If that is not available, a password in
your secret manager is fine — the tool reads it from an environment variable and never
logs it.

### 2.4 Two questions

* **Is there a read replica we can point at instead of the primary?** Strongly preferred.
  Comparison queries are read-heavy, and running them against the primary means they
  compete with your application. Without a replica, this work becomes off-hours only, or
  a DBA may reasonably refuse it.
* **Can the service reach RDS?** It needs to sit inside the VPC, or reach it through a
  bastion or VPC endpoint. Assume no public exposure.

---

## 3. Git

A **read-only** deploy key or PAT on the pipeline repository.

Why it matters more than it sounds: the transform SQL usually contains the rules the
pipeline claims to follow, so the tool reads them rather than guessing. Commit history is
also how a sign-off expires automatically when the relevant code changes, and how a
failure is routed to the team that owns it.

Without it: no commit-bound sign-offs, no ownership routing, no staleness detection.

---

## 4. Everything else, and when it is actually needed

| What | Needed by | If absent |
|---|---|---|
| Anthropic API key | Not for a while | Everything runs and reports; incidents arrive with raw evidence and no written summary |
| Slack bot token + two channels (alerts, shadow) | Before alerting | Nobody finds out about a break unless they open the dashboard |
| Jira project key + service account | Before going live | Slack-only reporting |
| SSO / OIDC client, and group-to-role mapping | Before sign-offs go live | Sign-off cannot go live at all, because it would be unattributable |
| Somewhere to host it | Before anything runs on a schedule | Needs VPC access to RDS, egress to Snowflake, and an HTTPS ingress |

---

## 5. Non-secret details to send me

These are safe to put in a message or a ticket:

* Snowflake account identifier, and the real database + schema names for
  bronze / silver / gold
* Whether those medallion layers are three real schemas, or whether some are views
* RDS hostname, port, database name — and whether it is a replica
* Whether the service will run inside the VPC
* Pipeline repo URL and default branch

## 6. Secrets — do not put these in a message

The private key, the RDS password, the git token and the API key all go into your
platform's secret manager, exposed to the service as environment variables. The tool
refuses to start if it finds a credential written into a config file.

| Variable | Holds |
|---|---|
| `DPHM_SF_ACCOUNT`, `DPHM_SF_USER`, `DPHM_SF_KEY_PATH` | Snowflake account, `DPHM_SVC`, path to the private key |
| `DPHM_RDS_HOST`, `DPHM_RDS_USER`, `DPHM_RDS_PASSWORD` | RDS connection |
| `DPHM_GIT_TOKEN` | Repo read token |
| `ANTHROPIC_API_KEY` | Optional |
