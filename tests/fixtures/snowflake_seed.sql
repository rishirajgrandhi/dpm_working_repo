-- Snowflake-side fixture: a real medallion pipeline in miniature (14 §4).
--
-- Runs in a dedicated DPHM_TEST database. NEVER point this at a production database:
-- it drops and recreates everything it touches.
--
-- The point of this fixture is that the pipeline is REAL, not staged output. L2 does an
-- actual QUALIFY ROW_NUMBER() dedup and L3 does an actual SCD2 merge, so the checks are
-- asserting against a pipeline that genuinely behaves the way pipelines behave. A fixture
-- built by inserting the "correct" answer directly would let a broken check pass.
--
-- Mirrors tests/fixtures/postgres_seed.sql. Deliberate defects carried across:
--   * customers 2, 3, 4 have duplicate rows in bronze
--   * customer 4's duplicates TIE on the full pick ordering  -> L2.6 must fire
--   * customer 3's correct winner is SOURCE_ROW_ID 1005      -> L2.5 asserts this
--   * customer 5 has a NULL business key                     -> reject table
--   * customer 6 is a test account                           -> declared filter
--   * customer 2 changes SMB -> ENTERPRISE                    -> SCD check 7, the story

-- The database itself is provisioned once by scripts/setup_dev_snowflake.sql, running as
-- ACCOUNTADMIN. This script deliberately does NOT create it: the dev role has no
-- CREATE DATABASE on the account, and it should not — the blast radius is the point.
--
-- Every object below is FULLY QUALIFIED rather than relying on `use database`. An
-- unqualified name depends on session context, which warehouse/safety.py cannot see and
-- therefore refuses. Qualifying is what lets the guard verify the target.

create schema if not exists DPHM_TEST.BRONZE;
create schema if not exists DPHM_TEST.SILVER;
create schema if not exists DPHM_TEST.GOLD;

-- ══════════════════════════════════════════════════════════════════════════
-- BRONZE — a faithful landing. Same rows, same values, no cleaning, no dedup.
-- ══════════════════════════════════════════════════════════════════════════
create or replace table DPHM_TEST.BRONZE.CUSTOMERS (
    CUSTOMER_ID        number(38,0),
    SOURCE_ROW_ID      number(38,0),
    NAME               varchar,
    SEGMENT            varchar,
    COUNTRY            varchar(2),
    CREDIT_LIMIT       number(12,2),
    SCORE              float,
    IS_TEST            boolean,
    EMAIL              varchar,
    PHONE              varchar,
    EXTERNAL_REF       varchar(36),
    ATTRIBUTES         variant,
    NOTES              varchar,
    SOURCE_UPDATED_AT  timestamp_ntz,
    -- audit columns: excluded from comparison by default (risk R1)
    ETL_BATCH_ID       number(38,0),
    _LOADED_AT         timestamp_ntz
);

insert into DPHM_TEST.BRONZE.CUSTOMERS
select column1, column2, column3, column4, column5, column6, column7, column8, column9,
       column10, column11, parse_json(column12), column13, column14, column15, column16
from values
    (1, 1001, 'Acme Ltd', 'SMB', 'GB', 5000.00, 0.5, false,
     'ops@acme.example', '+44 20 7000 0001', '3f6c1a2e-0000-4000-8000-000000000001',
     '{"tier":"gold","regions":["eu","uk"]}', 'first note',
     '2026-08-01 09:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    -- customer 2: two versions, the second changes a TRACKED column
    (2, 1002, 'Borealis GmbH', 'SMB', 'DE', 12000.50, 0.75, false,
     'finance@borealis.example', '+49 30 000002', '3f6c1a2e-0000-4000-8000-000000000002',
     '{"tier":"silver"}', '', '2026-08-02 10:00:00'::timestamp_ntz, 1,
     '2026-08-10 02:00:00'::timestamp_ntz),
    (2, 1003, 'Borealis GmbH', 'ENTERPRISE', 'DE', 90000.00, 0.81, false,
     'finance@borealis.example', '+49 30 000002', '3f6c1a2e-0000-4000-8000-000000000002',
     '{"tier":"gold"}', '', '2026-08-20 11:30:00'::timestamp_ntz, 2,
     '2026-08-11 02:00:00'::timestamp_ntz),
    -- customer 3: a duplicate with a CLEAR winner (1005, the newer row)
    (3, 1004, 'Cygnus SA', 'MID', 'FR', 7000.00, 0.42, false,
     'hello@cygnus.example', '+33 1 000003', '3f6c1a2e-0000-4000-8000-000000000003',
     '{"tier":"bronze"}', null, '2026-08-05 08:00:00'::timestamp_ntz, 1,
     '2026-08-10 02:00:00'::timestamp_ntz),
    (3, 1005, 'Cygnus SA', 'MID', 'FR', 7500.00, 0.44, false,
     'hello@cygnus.example', '+33 1 000003', '3f6c1a2e-0000-4000-8000-000000000003',
     '{"tier":"bronze"}', null, '2026-08-06 08:00:00'::timestamp_ntz, 1,
     '2026-08-10 02:00:00'::timestamp_ntz),
    -- customer 4: duplicates that TIE on SOURCE_UPDATED_AT. Only SOURCE_ROW_ID resolves
    -- it; without that tiebreaker the pipeline is silently non-reproducible.
    (4, 1006, 'Draco Inc', 'SMB', 'US', 2500.00, 0.31, false,
     'ap@draco.example', '+1 555 000004', '3f6c1a2e-0000-4000-8000-000000000004',
     '{"tier":"bronze"}', 'tied row A', '2026-08-07 12:00:00'::timestamp_ntz, 1,
     '2026-08-10 02:00:00'::timestamp_ntz),
    (4, 1007, 'Draco Inc', 'SMB', 'US', 2500.00, 0.31, false,
     'ap@draco.example', '+1 555 000004', '3f6c1a2e-0000-4000-8000-000000000004',
     '{"tier":"bronze"}', 'tied row B', '2026-08-07 12:00:00'::timestamp_ntz, 1,
     '2026-08-10 02:00:00'::timestamp_ntz),
    -- customer 5: NULL business key -> rejected
    (5, 1008, '', null, null, null, null, false,
     null, null, null, null, null, null, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    -- customer 6: a test account -> filtered by a DECLARED filter
    (6, 1009, 'Internal QA', 'SMB', 'GB', 1.00, 0.0, true,
     'qa@internal.example', null, '3f6c1a2e-0000-4000-8000-000000000006',
     '{}', null, '2026-08-08 07:00:00'::timestamp_ntz, 1,
     '2026-08-10 02:00:00'::timestamp_ntz),
    -- customer 7: unicode / multibyte
    (7, 1010, 'Ærø Håndværk ApS', 'MID', 'DK', 33333.33, 0.66, false,
     'kontakt@aeroe.example', '+45 00000007', '3f6c1a2e-0000-4000-8000-000000000007',
     '{"tier":"silver","note":"æøå"}', '  trailing space  ',
     '2026-08-09 06:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz);

create or replace table DPHM_TEST.BRONZE.ORDERS (
    ORDER_ID     number(38,0),
    CUSTOMER_ID  number(38,0),
    ORDER_DATE   date,
    COUNTRY      varchar(2),
    AMOUNT       number(12,2),
    STATUS       varchar,
    IS_TEST      boolean,
    PLACED_AT    timestamp_ntz,
    ETL_BATCH_ID number(38,0),
    _LOADED_AT   timestamp_ntz
);

insert into DPHM_TEST.BRONZE.ORDERS
select column1, column2, column3, column4, column5, column6, column7, column8, column9, column10
from values
    (9001, 1, '2026-08-01'::date, 'GB',  120.00, 'COMPLETE',  false, '2026-08-01 10:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    (9002, 1, '2026-08-01'::date, 'GB',   80.50, 'COMPLETE',  false, '2026-08-01 14:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    (9003, 2, '2026-08-02'::date, 'DE',  400.00, 'COMPLETE',  false, '2026-08-02 12:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    (9004, 2, '2026-08-21'::date, 'DE', 1500.00, 'COMPLETE',  false, '2026-08-21 09:00:00'::timestamp_ntz, 2, '2026-08-11 02:00:00'::timestamp_ntz),
    (9005, 3, '2026-08-06'::date, 'FR',  222.22, 'COMPLETE',  false, '2026-08-06 09:30:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    (9006, 4, '2026-08-07'::date, 'US',   10.00, 'CANCELLED', false, '2026-08-07 13:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    (9007, 6, '2026-08-08'::date, 'GB',    1.00, 'COMPLETE',  true,  '2026-08-08 08:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz),
    (9008, 7, '2026-08-09'::date, 'DK',  999.99, 'COMPLETE',  false, '2026-08-09 07:00:00'::timestamp_ntz, 1, '2026-08-10 02:00:00'::timestamp_ntz);

-- The completed-batch control table. Batch 3 is left RUNNING on purpose: a scheduled
-- check must not read it (risk R2).
create or replace table DPHM_TEST.BRONZE.ETL_BATCH_LOG (
    BATCH_ID number(38,0), STATUS varchar, COMPLETED_AT timestamp_ntz
);
insert into DPHM_TEST.BRONZE.ETL_BATCH_LOG
select column1, column2, column3 from values
    (1, 'SUCCESS', '2026-08-10 02:00:00'::timestamp_ntz),
    (2, 'SUCCESS', '2026-08-11 02:00:00'::timestamp_ntz),
    (3, 'RUNNING', null);

-- ══════════════════════════════════════════════════════════════════════════
-- SILVER — bronze, deduplicated by a total pick rule, minus NAMED losses.
-- This is a REAL dedup, not staged output.
-- ══════════════════════════════════════════════════════════════════════════
create or replace table DPHM_TEST.SILVER.CUSTOMERS_REJECT (
    CUSTOMER_ID    number(38,0),
    SOURCE_ROW_ID  number(38,0),
    REJECT_REASON  varchar,       -- from a CLOSED set; L2.8 asserts that
    REJECTED_AT    timestamp_ntz
);

insert into DPHM_TEST.SILVER.CUSTOMERS_REJECT
select CUSTOMER_ID, SOURCE_ROW_ID, 'NULL_BUSINESS_KEY', current_timestamp()::timestamp_ntz
from DPHM_TEST.BRONZE.CUSTOMERS
where NAME is null or NAME = '';

create or replace table DPHM_TEST.SILVER.CUSTOMERS as
select CUSTOMER_ID, SOURCE_ROW_ID, NAME, SEGMENT, COUNTRY, CREDIT_LIMIT, SCORE,
       EMAIL, PHONE, EXTERNAL_REF, ATTRIBUTES, NOTES, SOURCE_UPDATED_AT
from DPHM_TEST.BRONZE.CUSTOMERS
where NAME is not null
  and NAME <> ''                                    -- named loss: rejected
  and IS_TEST = false                               -- named loss: filtered_test_accounts
-- THE DEDUP CONTRACT, written in SQL so repo/dedup_extract.py can read it back out.
-- The second ORDER BY term is the tiebreaker that makes the ordering total.
qualify row_number() over (
    partition by CUSTOMER_ID
    order by SOURCE_UPDATED_AT desc nulls last, SOURCE_ROW_ID desc nulls last
) = 1;

-- NOT a dedup: bronze.orders has no duplicate ORDER_IDs, so this hop is a pure filter.
-- An earlier version used QUALIFY here with ORDER_ID as its own tiebreaker, which cannot
-- resolve anything (every row in a partition shares the partition key). config/models.py
-- rejects that pick rule, which is how the mistake surfaced.
create or replace table DPHM_TEST.SILVER.ORDERS as
select ORDER_ID, CUSTOMER_ID, ORDER_DATE, COUNTRY, AMOUNT, STATUS, PLACED_AT
from DPHM_TEST.BRONZE.ORDERS
where IS_TEST = false;

-- ══════════════════════════════════════════════════════════════════════════
-- GOLD — dimensions, facts, marts.
-- ══════════════════════════════════════════════════════════════════════════
create or replace table DPHM_TEST.GOLD.DIM_CUSTOMER (
    CUSTOMER_SK   number(38,0),
    CUSTOMER_ID   number(38,0),
    NAME          varchar,
    SEGMENT       varchar,
    COUNTRY       varchar(2),
    CREDIT_LIMIT  number(12,2),
    PHONE         varchar,        -- type-1: overwritten in place, no new version
    EMAIL         varchar,
    VALID_FROM    timestamp_ntz,
    VALID_TO      timestamp_ntz,
    IS_CURRENT    boolean,
    DW_UPDATED_AT timestamp_ntz   -- audit
);

-- A REAL SCD2 build: one closed version and one open version for customer 2, because a
-- tracked column changed. This is the behaviour SCD check 7 asserts, and the behaviour
-- that the motivating bug destroys.
insert into DPHM_TEST.GOLD.DIM_CUSTOMER
with versioned as (
    select CUSTOMER_ID, NAME, SEGMENT, COUNTRY, CREDIT_LIMIT, PHONE, EMAIL,
           SOURCE_UPDATED_AT as VALID_FROM,
           lead(SOURCE_UPDATED_AT) over (
               partition by CUSTOMER_ID order by SOURCE_UPDATED_AT
           ) as NEXT_FROM
    -- Built from BRONZE, the change HISTORY, not from silver.
    --
    -- Silver holds current state (one row per customer), so a dimension built from it
    -- has one version per key — and then a fact placed before the latest change has no
    -- version valid at its event time. The as-of FK check caught exactly that, which is
    -- the check doing its job.
    --
    -- Deduplicated on (key, timestamp) so an EXACT duplicate does not become a spurious
    -- version. Genuine changes at different timestamps still produce genuine versions.
    from DPHM_TEST.BRONZE.CUSTOMERS
    where NAME is not null and NAME <> '' and IS_TEST = false
    qualify row_number() over (
        partition by CUSTOMER_ID, SOURCE_UPDATED_AT order by SOURCE_ROW_ID desc
    ) = 1
)
select row_number() over (order by CUSTOMER_ID, VALID_FROM)  as CUSTOMER_SK,
       CUSTOMER_ID, NAME, SEGMENT, COUNTRY, CREDIT_LIMIT, PHONE, EMAIL,
       VALID_FROM,
       coalesce(NEXT_FROM, '9999-12-31'::timestamp_ntz)       as VALID_TO,
       NEXT_FROM is null                                      as IS_CURRENT,
       current_timestamp()::timestamp_ntz                     as DW_UPDATED_AT
from versioned;

create or replace table DPHM_TEST.GOLD.FCT_ORDER as
select o.ORDER_ID,
       d.CUSTOMER_SK,          -- resolved AS OF the event time, not to the current row
       o.ORDER_DATE, o.COUNTRY, o.AMOUNT, o.STATUS, o.PLACED_AT
from DPHM_TEST.SILVER.ORDERS o
left join DPHM_TEST.GOLD.DIM_CUSTOMER d
       on d.CUSTOMER_ID = o.CUSTOMER_ID
      and o.PLACED_AT >= d.VALID_FROM
      and o.PLACED_AT <  d.VALID_TO;

create or replace table DPHM_TEST.GOLD.MART_DAILY_REVENUE as
select ORDER_DATE,
       COUNTRY,
       sum(AMOUNT)                  as REVENUE,        -- additive
       count(*)                     as ORDER_COUNT,    -- additive
       avg(AMOUNT)                  as AVG_BASKET,     -- NON-additive: reported unverified
       count(distinct CUSTOMER_ID)  as UNIQUE_BUYERS   -- NON-additive: reported unverified
from DPHM_TEST.SILVER.ORDERS
group by ORDER_DATE, COUNTRY;

-- A gold object with NO silver counterpart, so coverage has something honest to report
-- as `unvalidated` rather than quietly omitting it.
create or replace table DPHM_TEST.GOLD.MART_CHURN (
    ORDER_DATE date, CHURN_RATE float
);
