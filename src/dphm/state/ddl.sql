-- DPHM_STATE — the whole state store (08).
--
-- One schema in the existing Snowflake account. No new database, nothing extra to
-- provision, secure, or back up. Applied forward-only by state/migrate.py.
--
-- Two deliberate omissions, both documented in 08 §2:
--   * No FOREIGN KEY constraints. Snowflake does not enforce them, and declaring
--     unenforced constraints is exactly the kind of false confidence this product is
--     against. Referential integrity lives in state/repo.py and is asserted in tests.
--   * COLUMNS_COMPARED / COLUMNS_EXCLUDED are NOT NULL. If the engine cannot say what it
--     compared, the result is an error, not a pass. This is risk R1 at the schema level.

-- The schema itself is NOT created here. DPHM_WRITER holds `ALL on DPHM_STATE and
-- DPHM_SCRATCH` and nothing else (02 §1) — it has no CREATE SCHEMA on the database, and
-- should not. The two schemas are provisioned once by an administrator; state/migrate.py
-- verifies they exist and names the provisioning step if they do not.

-- ── Migration ledger (read by state/migrate.py before anything else) ──────
create table if not exists DPHM_STATE.SCHEMA_MIGRATIONS (
    VERSION      number(6,0)   not null primary key,
    FILENAME     string        not null,
    CHECKSUM     string        not null,   -- refuse to re-apply an edited migration
    APPLIED_AT   timestamp_ntz not null,
    APPLIED_BY   string        not null,
    DURATION_MS  number(18,0)
);

-- ── Runs ──────────────────────────────────────────────────────────────────
create table if not exists DPHM_STATE.RUNS (
    RUN_ID              string        not null primary key,   -- ULID
    PROJECT             string        not null,
    LANE                string        not null,               -- A | B
    -- Named TRIGGER_KIND, not TRIGGER: `TRIGGER` is a reserved word in Snowflake and the
    -- DDL will not compile with it. Quoting would work but would burden every query
    -- forever, so the column is renamed instead.
    TRIGGER_KIND        string        not null,               -- schedule|load_hook|ui|pr_hook
    STARTED_AT          timestamp_ntz not null,
    ENDED_AT            timestamp_ntz,
    STATUS              string        not null,               -- RUNNING|OK|FAILED|ERROR|PARTIAL
    SCOPE_PREDICATE     string,                               -- exactly what we read
    CONFIG_SHA          string        not null,
    SOURCE_COMMIT_SHA   string,
    PROMPT_SHA          string,
    TOOL_VERSION        string        not null,
    SHADOW_MODE         boolean       not null default true,
    WAREHOUSE_SECONDS   number(18,3),
    ESTIMATED_USD       number(12,4),
    LLM_USD             number(12,4),
    NOTES               string
);

-- ── Check registry ────────────────────────────────────────────────────────
create table if not exists DPHM_STATE.CHECKS (
    CHECK_ID            string        not null primary key,
    PROJECT             string        not null,
    LANE                string        not null,
    TEMPLATE            string        not null,
    TABLE_NAME          string        not null,
    RELATIONSHIP        string        not null,
    HOP                 string,
    HOP_ID              string,
    LAYER               string,                               -- bronze|silver|gold
    SEVERITY            string        not null,
    STATUS              string        not null,               -- active|shadow|snoozed|draft|retired
    PARAMS              variant       not null,
    SQL_SHA256          string        not null,
    SOURCE_COMMIT_SHA   string,
    GRAIN_CONFIRMED_BY  string,
    GRAIN_CONFIRMED_AT  timestamp_ntz,
    GATE_BUDGET_OK      boolean       not null default false,
    GATE_PASSES_GOOD    boolean       not null default false,
    GATE_MUTATION_OK    boolean       not null default false,  -- non-negotiable
    GATE_DETERMINISTIC  boolean       not null default false,
    VALIDATED_AT        timestamp_ntz,
    CREATED_AT          timestamp_ntz not null,
    RETIRED_AT          timestamp_ntz
);

-- ── Results ───────────────────────────────────────────────────────────────
create table if not exists DPHM_STATE.CHECK_RESULTS (
    RESULT_ID           string        not null primary key,
    RUN_ID              string        not null,
    CHECK_ID            string        not null,
    STATUS              string        not null,   -- PASS|FAIL|INCONCLUSIVE|UNVALIDATED
    FAILING_ROW_COUNT   number(18,0),
    EVALUATED_ROW_COUNT number(18,0),
    FAILING_FINGERPRINT string,
    SAMPLE_ROWS         variant,                  -- redacted per column_rules
    COLUMNS_COMPARED    array         not null,   -- REQUIRED (risk R1)
    COLUMNS_EXCLUDED    array         not null,
    EXCLUSION_REASONS   variant,
    SCOPE_PREDICATE     string,
    SQL_SHA256          string        not null,
    QUERY_ID            string,
    DURATION_MS         number(18,0),
    ESTIMATED_USD       number(12,4),
    SUPPRESSED_BY       string,                   -- incident id of the earliest failing layer
    ERROR_MESSAGE       string,
    CREATED_AT          timestamp_ntz not null
);

-- ── Incidents (Lane A only) ───────────────────────────────────────────────
create table if not exists DPHM_STATE.INCIDENTS (
    INCIDENT_ID         string        not null primary key,
    PROJECT             string        not null,
    FINGERPRINT         string        not null,   -- dedup key across runs
    TITLE               string        not null,
    CATEGORY            string,                   -- CLOSED vocabulary only
    CATEGORY_CONFIDENCE number(4,3),
    STATUS              string        not null,   -- OPEN|ACKED|SNOOZED|RESOLVED|FALSE_POSITIVE
    SEVERITY            string        not null,
    OWNER_TEAM          string,
    LIKELY_AUTHOR       string,                   -- mentioned, never assigned (risk R6)
    JIRA_KEY            string,
    SLACK_TS            string,
    FIRST_SEEN_RUN_ID   string        not null,
    LAST_SEEN_RUN_ID    string        not null,
    OCCURRENCE_COUNT    number(9,0)   not null default 1,
    EVIDENCE            variant,
    NARRATIVE           string,                   -- LLM text; always labelled as such
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
    SIGNOFF_ID          string        not null primary key,
    CHECK_ID            string        not null,
    FINGERPRINT         string,                   -- null = whole check; else specific rows
    REASON              string        not null,
    SIGNED_BY           string        not null,
    SIGNED_AT           timestamp_ntz not null,
    BOUND_COMMIT_SHA    string        not null,   -- expires when this changes
    WATCHED_PATHS       array,
    EXPIRES_AT          timestamp_ntz,            -- hard ceiling, default +90 days
    REVOKED_AT          timestamp_ntz
);

-- ── SCD immutability baseline (check 11) ──────────────────────────────────
create table if not exists DPHM_STATE.SCD_VERSION_HASHES (
    TABLE_NAME     string        not null,
    SURROGATE_KEY  string        not null,
    CONTENT_HASH   string        not null,
    FIRST_SEEN_RUN string        not null,
    FIRST_SEEN_AT  timestamp_ntz not null
);

-- ── Golden windows — the believed-correct scope gate 2 validates against ──
create table if not exists DPHM_STATE.GOLDEN_WINDOWS (
    WINDOW_ID       string        not null primary key,
    PROJECT         string        not null,
    TABLE_NAME      string        not null,
    SCOPE_PREDICATE string        not null,
    FROM_TS         timestamp_ntz,
    TO_TS           timestamp_ntz,
    VOUCHED_BY      string,
    INFERRED        boolean       not null default false,
    REASON          string        not null,
    SUPERSEDED_BY   string,
    CREATED_AT      timestamp_ntz not null
);

-- ── Coverage snapshot, per run ────────────────────────────────────────────
create table if not exists DPHM_STATE.COVERAGE (
    RUN_ID              string      not null,
    PROJECT             string      not null,
    TABLE_NAME          string      not null,
    TABLE_TYPE          string,
    LAYER               string,
    HOP_ID              string,
    HOP_RELATION        string,
    CONTRACT_CONFIRMED  boolean,
    UNVERIFIED_MEASURES array,
    CHECKS_ACTIVE       number(9,0) not null,
    CHECKS_EXPECTED     number(9,0) not null,
    COLUMNS_TOTAL       number(9,0) not null,
    COLUMNS_COMPARED    number(9,0) not null,
    GRAIN_CONFIRMED     boolean     not null,
    UNVALIDATED         boolean     not null,
    CREATED_AT          timestamp_ntz not null
);

-- ── Permission fingerprint (a grant change must not look like data loss) ──
create table if not exists DPHM_STATE.PERMISSION_FINGERPRINTS (
    RUN_ID       string        not null,
    ENGINE       string        not null,          -- snowflake | postgres
    ROLE_NAME    string        not null,
    GRANTS_HASH  string        not null,
    GRANTS       variant,
    CHANGED      boolean       not null,
    CREATED_AT   timestamp_ntz not null
);

-- ── LLM egress log (what left the perimeter) ──────────────────────────────
create table if not exists DPHM_STATE.LLM_EGRESS_LOG (
    EGRESS_ID           string        not null primary key,
    RUN_ID              string        not null,
    GRAPH               string        not null,   -- authoring | reporting
    NODE                string        not null,
    PROMPT_SHA          string        not null,
    MODEL               string        not null,
    PAYLOAD_KINDS       array         not null,
    ROW_VALUES_SENT     boolean       not null,
    PII_COLUMNS_PRESENT array,
    INPUT_TOKENS        number(12,0),
    OUTPUT_TOKENS       number(12,0),
    USD                 number(12,4),
    CREATED_AT          timestamp_ntz not null
);

-- ── Job queue (durable; the in-process pool is only the dispatcher) ───────
create table if not exists DPHM_STATE.JOBS (
    JOB_ID        string        not null primary key,
    PROJECT       string        not null,
    KIND          string        not null,         -- lane_a_run | parity | authoring
    PAYLOAD       variant       not null,
    STATUS        string        not null,         -- QUEUED|RUNNING|DONE|FAILED|CANCELLED
    SUBMITTED_BY  string,                         -- the signed-in user, or system:scheduler
    RUN_ID        string,
    ATTEMPTS      number(4,0)   not null default 0,
    CREATED_AT    timestamp_ntz not null,
    STARTED_AT    timestamp_ntz,
    ENDED_AT      timestamp_ntz,
    ERROR_MESSAGE string
);

-- ── LangGraph checkpoints ─────────────────────────────────────────────────
-- Backed by DPHM_STATE, not a container-local SQLite file: an onboarding run pauses for
-- days at the human interrupt and must survive a redeploy (09 §1.3).
create table if not exists DPHM_STATE.GRAPH_CHECKPOINTS (
    THREAD_ID     string        not null,
    CHECKPOINT_ID string        not null,
    PARENT_ID     string,
    GRAPH         string        not null,         -- authoring | reporting
    STATE         variant       not null,
    CREATED_AT    timestamp_ntz not null
);

-- ── Paused agent-graph interrupts -> the app's Review Queue (12 §2.4) ─────
create table if not exists DPHM_STATE.REVIEW_ITEMS (
    ITEM_ID       string        not null primary key,
    PROJECT       string        not null,
    THREAD_ID     string        not null,
    KIND          string        not null,
    SUBJECT       string        not null,
    PROPOSAL      variant       not null,
    EVIDENCE      variant       not null,         -- what it was inferred from
    STATUS        string        not null,         -- PENDING|ANSWERED|SUPERSEDED
    ANSWERED_BY   string,                         -- a real person, via SSO
    ANSWER        variant,
    CREATED_AT    timestamp_ntz not null,
    ANSWERED_AT   timestamp_ntz
);

-- ── Feedback from Slack buttons and the app (feeds the precision metric) ──
create table if not exists DPHM_STATE.FEEDBACK (
    FEEDBACK_ID string        not null primary key,
    INCIDENT_ID string        not null,
    ACTION      string        not null,           -- ack|false_positive|snooze|confirm_root_cause
    ACTOR       string        not null,
    COMMENT     string,
    CREATED_AT  timestamp_ntz not null
);
