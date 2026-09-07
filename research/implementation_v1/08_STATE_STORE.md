# 08 — State Store

One schema in the existing Snowflake account. **No new database, no new infrastructure to
provision, secure, or back up.**

- `DPHM_STATE` — durable state. Written only by `state/`.
- `DPHM_SCRATCH` — transient Lane B landing and mutation-test fixtures. Auto-swept.

Both owned by `DPHM_WRITER`, which has no grants anywhere else.

## 1. Tables

```sql
-- state/ddl.sql  (forward-only, applied by state/migrate.py)
create schema if not exists DPHM_STATE;

-- ── Migration ledger (read by state/migrate.py before anything else) ──────
create table if not exists DPHM_STATE.SCHEMA_MIGRATIONS (
    VERSION      number(6,0)  not null primary key,   -- forward-only, numbered
    FILENAME     string       not null,
    CHECKSUM     string       not null,               -- refuse to re-apply an edited migration
    APPLIED_AT   timestamp_ntz not null,
    APPLIED_BY   string       not null,               -- tool version + role
    DURATION_MS  number(18,0)
);

-- ── Runs ──────────────────────────────────────────────────────────────────
create table if not exists DPHM_STATE.RUNS (
    RUN_ID              string       not null primary key,   -- ULID
    PROJECT             string       not null,
    LANE                string       not null,               -- A | B
    TRIGGER             string       not null,               -- schedule | load_hook | ui | pr_hook
    STARTED_AT          timestamp_ntz not null,
    ENDED_AT            timestamp_ntz,
    STATUS              string       not null,               -- RUNNING|OK|FAILED|ERROR|PARTIAL
    SCOPE_PREDICATE     string,                              -- exactly what we read
    CONFIG_SHA          string       not null,
    SOURCE_COMMIT_SHA   string,
    PROMPT_SHA          string,                              -- attribute behaviour to prompt version
    TOOL_VERSION        string       not null,
    SHADOW_MODE         boolean      not null default true,
    WAREHOUSE_SECONDS   number(18,3),
    ESTIMATED_USD       number(12,4),
    LLM_USD             number(12,4),
    NOTES               string
);

-- ── Check registry ────────────────────────────────────────────────────────
create table if not exists DPHM_STATE.CHECKS (
    CHECK_ID            string       not null primary key,
    PROJECT             string       not null,
    LANE                string       not null,
    TEMPLATE            string       not null,
    TABLE_NAME          string       not null,
    RELATIONSHIP        string       not null,   -- dedup_of | aggregate_of | scd2_of | ...
    HOP                 string,                  -- source_to_bronze | bronze_to_silver | silver_to_gold
    HOP_ID              string,                  -- FK-in-spirit to layers.yaml hop id
    LAYER               string,                  -- bronze | silver | gold — dashboard grouping
    SEVERITY            string       not null,
    STATUS              string       not null,   -- active|shadow|snoozed|draft|retired
    PARAMS              variant      not null,
    SQL_SHA256          string       not null,
    SOURCE_COMMIT_SHA   string,
    GRAIN_CONFIRMED_BY  string,
    GRAIN_CONFIRMED_AT  timestamp_ntz,
    GATE_BUDGET_OK      boolean      not null default false,
    GATE_PASSES_GOOD    boolean      not null default false,
    GATE_MUTATION_OK    boolean      not null default false,   -- non-negotiable
    GATE_DETERMINISTIC  boolean      not null default false,
    VALIDATED_AT        timestamp_ntz,
    CREATED_AT          timestamp_ntz not null,
    RETIRED_AT          timestamp_ntz
);

-- ── Results ───────────────────────────────────────────────────────────────
create table if not exists DPHM_STATE.CHECK_RESULTS (
    RESULT_ID           string       not null primary key,
    RUN_ID              string       not null,
    CHECK_ID            string       not null,
    STATUS              string       not null,   -- PASS|FAIL|INCONCLUSIVE|UNVALIDATED
    FAILING_ROW_COUNT   number(18,0),
    EVALUATED_ROW_COUNT number(18,0),
    FAILING_FINGERPRINT string,                  -- sha256 of sorted failing_key set → dedup
    SAMPLE_ROWS         variant,                 -- redacted per column_rules
    COLUMNS_COMPARED    array        not null,   -- REQUIRED. a run that cannot record these errors
    COLUMNS_EXCLUDED    array        not null,
    EXCLUSION_REASONS   variant,                 -- column → audit_pattern | pii_hash | transform:<id>
    SCOPE_PREDICATE     string,
    SQL_SHA256          string       not null,
    QUERY_ID            string,                  -- Snowflake query id, for cost + replay
    DURATION_MS         number(18,0),
    ESTIMATED_USD       number(12,4),
    SUPPRESSED_BY       string,                  -- incident id of the earliest failing layer;
                                                 -- the result is still recorded, just not re-alerted
    ERROR_MESSAGE       string,
    CREATED_AT          timestamp_ntz not null
);

-- ── Incidents (Lane A only) ───────────────────────────────────────────────
create table if not exists DPHM_STATE.INCIDENTS (
    INCIDENT_ID         string       not null primary key,
    PROJECT             string       not null,
    FINGERPRINT         string       not null,   -- dedup key across runs
    TITLE               string       not null,
    CATEGORY            string,                  -- from the CLOSED vocabulary only
    CATEGORY_CONFIDENCE number(4,3),
    STATUS              string       not null,   -- OPEN|ACKED|SNOOZED|RESOLVED|FALSE_POSITIVE
    SEVERITY            string       not null,
    OWNER_TEAM          string,
    LIKELY_AUTHOR       string,                  -- mentioned, never assigned (risk R6)
    JIRA_KEY            string,
    SLACK_TS            string,
    FIRST_SEEN_RUN_ID   string       not null,
    LAST_SEEN_RUN_ID    string       not null,
    OCCURRENCE_COUNT    number(9,0)  not null default 1,
    EVIDENCE            variant,                 -- deterministic evidence bundle (redacted)
    NARRATIVE           string,                  -- LLM text; always labelled as such in the UI
    CREATED_AT          timestamp_ntz not null,
    UPDATED_AT          timestamp_ntz not null,
    RESOLVED_AT         timestamp_ntz
);

create table if not exists DPHM_STATE.INCIDENT_MEMBERS (
    INCIDENT_ID string not null,
    RESULT_ID   string not null,
    RUN_ID      string not null
);

-- ── Sign-offs and exceptions (commit-bound, self-expiring) ────────────────
create table if not exists DPHM_STATE.SIGNOFFS (
    SIGNOFF_ID          string       not null primary key,
    CHECK_ID            string       not null,
    FINGERPRINT         string,                  -- null = whole check; else specific failing rows
    REASON              string       not null,
    SIGNED_BY           string       not null,
    SIGNED_AT           timestamp_ntz not null,
    BOUND_COMMIT_SHA    string       not null,   -- expires when this changes
    WATCHED_PATHS       array,                   -- files whose change expires this
    EXPIRES_AT          timestamp_ntz,           -- hard ceiling, default +90 days
    REVOKED_AT          timestamp_ntz
);

-- ── SCD immutability baseline (check 11) ──────────────────────────────────
create table if not exists DPHM_STATE.SCD_VERSION_HASHES (
    TABLE_NAME     string not null,
    SURROGATE_KEY  string not null,
    CONTENT_HASH   string not null,
    FIRST_SEEN_RUN string not null,
    FIRST_SEEN_AT  timestamp_ntz not null
);

-- ── Golden windows — the believed-correct scope gate 2 validates against ──
-- Required by validation gate 2 (`10`). Without a row here a check cannot pass gate 2,
-- and therefore cannot be activated.
create table if not exists DPHM_STATE.GOLDEN_WINDOWS (
    WINDOW_ID       string       not null primary key,
    PROJECT         string       not null,
    TABLE_NAME      string       not null,
    SCOPE_PREDICATE string       not null,   -- the exact batch range being vouched for
    FROM_TS         timestamp_ntz,
    TO_TS           timestamp_ntz,
    VOUCHED_BY      string,                  -- a real person via SSO, or NULL if inferred
    INFERRED        boolean      not null default false,  -- Q12 default: last 7 incident-free days
    REASON          string       not null,
    SUPERSEDED_BY   string,                  -- window ids are never edited, only superseded
    CREATED_AT      timestamp_ntz not null
);

-- ── Coverage snapshot, per run ────────────────────────────────────────────
create table if not exists DPHM_STATE.COVERAGE (
    RUN_ID           string not null,
    PROJECT          string not null,
    TABLE_NAME       string not null,
    TABLE_TYPE       string,
    LAYER            string,                 -- bronze | silver | gold
    HOP_ID           string,
    HOP_RELATION     string,                 -- dedup_of | aggregate_of | ... | unvalidated
    CONTRACT_CONFIRMED boolean,              -- dedup key / pick rule / mart grain confirmed
    UNVERIFIED_MEASURES array,               -- non-additive mart measures we cannot recompute
    CHECKS_ACTIVE    number(9,0) not null,
    CHECKS_EXPECTED  number(9,0) not null,   -- from table_type → family mapping
    COLUMNS_TOTAL    number(9,0) not null,
    COLUMNS_COMPARED number(9,0) not null,
    GRAIN_CONFIRMED  boolean     not null,
    UNVALIDATED      boolean     not null,
    CREATED_AT       timestamp_ntz not null
);

-- ── Permission fingerprint (a grant change must not look like data loss) ──
create table if not exists DPHM_STATE.PERMISSION_FINGERPRINTS (
    RUN_ID       string not null,
    ENGINE       string not null,             -- snowflake | postgres
    ROLE_NAME    string not null,
    GRANTS_HASH  string not null,
    GRANTS       variant,
    CHANGED      boolean not null,
    CREATED_AT   timestamp_ntz not null
);

-- ── LLM egress log (what left the perimeter) ──────────────────────────────
create table if not exists DPHM_STATE.LLM_EGRESS_LOG (
    EGRESS_ID     string not null primary key,
    RUN_ID        string not null,
    GRAPH         string not null,            -- authoring | reporting
    NODE          string not null,
    PROMPT_SHA    string not null,
    MODEL         string not null,
    PAYLOAD_KINDS array  not null,            -- e.g. ['schema','git_log','redacted_sample']
    ROW_VALUES_SENT boolean not null,
    PII_COLUMNS_PRESENT array,
    INPUT_TOKENS  number(12,0),
    OUTPUT_TOKENS number(12,0),
    USD           number(12,4),
    CREATED_AT    timestamp_ntz not null
);

-- ── Job queue (durable; the in-process worker pool is only the dispatcher) ─
create table if not exists DPHM_STATE.JOBS (
    JOB_ID        string not null primary key,
    PROJECT       string not null,
    KIND          string not null,            -- lane_a_run | parity | authoring
    PAYLOAD       variant not null,
    STATUS        string not null,            -- QUEUED|RUNNING|DONE|FAILED|CANCELLED
    SUBMITTED_BY  string,                     -- the signed-in user, or 'system:scheduler'
    RUN_ID        string,
    ATTEMPTS      number(4,0) not null default 0,
    CREATED_AT    timestamp_ntz not null,
    STARTED_AT    timestamp_ntz,
    ENDED_AT      timestamp_ntz,
    ERROR_MESSAGE string
);

-- ── LangGraph checkpoints (durable: an onboarding run waits days for a human) ─
create table if not exists DPHM_STATE.GRAPH_CHECKPOINTS (
    THREAD_ID    string not null,
    CHECKPOINT_ID string not null,
    PARENT_ID    string,
    GRAPH        string not null,             -- authoring | reporting
    STATE        variant not null,
    CREATED_AT   timestamp_ntz not null
);

-- ── Paused agent-graph interrupts → the app's Review Queue (12 §2.4) ───────
create table if not exists DPHM_STATE.REVIEW_ITEMS (
    ITEM_ID       string not null primary key,
    PROJECT       string not null,
    THREAD_ID     string not null,            -- LangGraph checkpoint thread
    KIND          string not null,            -- confirm_grain | confirm_dedup_contract |
                                              -- confirm_mart_grain | confirm_loss | approve_checks
    SUBJECT       string not null,            -- the table or hop being confirmed
    PROPOSAL      variant not null,
    EVIDENCE      variant not null,           -- what it was inferred from — shown to the reviewer
    STATUS        string not null,            -- PENDING|ANSWERED|SUPERSEDED
    ANSWERED_BY   string,                     -- a real person, via SSO
    ANSWER        variant,
    CREATED_AT    timestamp_ntz not null,
    ANSWERED_AT   timestamp_ntz
);

-- ── Feedback from Slack buttons and the app (feeds precision metric) ───────
create table if not exists DPHM_STATE.FEEDBACK (
    FEEDBACK_ID string not null primary key,
    INCIDENT_ID string not null,
    ACTION      string not null,              -- ack | false_positive | snooze | confirm_root_cause
    ACTOR       string not null,
    COMMENT     string,
    CREATED_AT  timestamp_ntz not null
);
```

## 2. Design notes

**No foreign key constraints.** Snowflake does not enforce them; referential integrity is
maintained in `state/repo.py` and asserted in tests. Declaring unenforced constraints would be
exactly the kind of false confidence this product is against.

**`COLUMNS_COMPARED` / `COLUMNS_EXCLUDED` are `not null`.** If the engine cannot say what it
compared, the result is an error, not a pass. This is the schema-level expression of risk R1.

**Incidents are fingerprinted on failing rows, not on the check.** `FINGERPRINT =
sha256(check_id + sorted(failing_key set))`. A recurring identical failure updates one incident
(`OCCURRENCE_COUNT`, `LAST_SEEN_RUN_ID`); a **new** failing row produces a new fingerprint and
therefore a fresh look. This is what stops one break from filing thirty tickets, and what stops a
March exception from hiding a July bug.

**Sign-offs are bound to a commit and a set of watched paths.** On every run,
`state/signoff.py` re-resolves `BOUND_COMMIT_SHA` against the current HEAD of `WATCHED_PATHS`.
If those files changed, the sign-off is expired and the incident reopens — automatically.

**Retention.** `CHECK_RESULTS` and `LLM_EGRESS_LOG` keep 400 days; `SAMPLE_ROWS` is nulled after
30 days by a scheduled task, because samples are the only place row data lives at rest.
`RUNS`, `CHECKS`, `INCIDENTS`, `SIGNOFFS` are kept indefinitely — they are the audit trail.

**Migrations** are forward-only numbered SQL files applied by `state/migrate.py`, tracked in
`DPHM_STATE.SCHEMA_MIGRATIONS`. Migrations run at service startup; the service refuses to serve
if they fail, and the Settings diagnostics panel shows the current level.

## 3. Read patterns

| Question | Query |
|---|---|
| What is failing right now? | `INCIDENTS where STATUS in ('OPEN','ACKED')` |
| Is this a new problem? | `FINGERPRINT` lookup, then `OCCURRENCE_COUNT` |
| What are we not checking? | `COVERAGE where UNVALIDATED or not GRAIN_CONFIRMED or not CONTRACT_CONFIRMED or CHECKS_ACTIVE < CHECKS_EXPECTED or array_size(UNVERIFIED_MEASURES) > 0` |
| Which layer broke first? | `CHECK_RESULTS where STATUS='FAIL' and SUPPRESSED_BY is null`, ordered by hop |
| Which checks are unvalidated? | `CHECKS where not GATE_MUTATION_OK` |
| Did a permission change explain this? | `PERMISSION_FINGERPRINTS where CHANGED` for the run |
| What did we send to the LLM? | `LLM_EGRESS_LOG` for the run |
| Is our precision holding? | `FEEDBACK` joined to `INCIDENTS` |
| What is waiting on a human? | `REVIEW_ITEMS where STATUS='PENDING'` |
| What scope did we vouch for, and who vouched? | `GOLDEN_WINDOWS where SUPERSEDED_BY is null` |
| Who triggered this, and who confirmed that? | `JOBS.SUBMITTED_BY`, `REVIEW_ITEMS.ANSWERED_BY` |

The web app's screens are exactly these queries and nothing more. The frontend computes nothing —
every verdict is decided once, in SQL, and recorded here, which is what makes the dashboard,
Slack, Jira, and the PR comment all say the same thing.
