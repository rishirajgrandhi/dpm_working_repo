-- RDS-side fixture: the migration SOURCE (14 §4).
--
-- Deliberately contains one instance of every canonicalization case in 07 §3, because
-- cross-engine comparison is only meaningful over a canonical projection and those rules
-- are where homegrown parity tooling dies. Also carries the defects the L2 dedup contract
-- exists to catch, so the fixture can actually assert something.

drop schema if exists public cascade;
create schema public;

-- ── customers: the SCD2 dimension's source ────────────────────────────────
create table public.customers (
    customer_id        integer        not null,
    source_row_id      bigint         not null,   -- the pick rule's tiebreaker
    name               text           not null,
    segment            text,                      -- tracked: SMB -> ENTERPRISE is the story
    country            varchar(2),
    credit_limit       numeric(12,2),             -- fixed-scale numeric case
    score              double precision,          -- float: NEVER hashed, tolerance only
    is_test            boolean        not null default false,
    email              text,                      -- PII: compared as a hash
    phone              text,                      -- type-1: overwritten in place
    external_ref       uuid,
    attributes         jsonb,                     -- VARIANT on the Snowflake side
    notes              text,                      -- NULL vs empty string case
    legacy_notes       text,                      -- dropped at cutover (declared transform)
    source_updated_at  timestamptz,               -- the pick rule's primary ordering
    created_at         timestamp      not null default now(),
    updated_at         timestamptz    not null default now()   -- audit: excluded
);

insert into public.customers
    (customer_id, source_row_id, name, segment, country, credit_limit, score,
     is_test, email, phone, external_ref, attributes, notes, legacy_notes, source_updated_at)
values
    -- 1. plain row, no duplicates
    (1, 1001, 'Acme Ltd', 'SMB', 'GB', 5000.00, 0.5,  false,
     'ops@acme.example', '+44 20 7000 0001', '3f6c1a2e-0000-4000-8000-000000000001'::uuid,
     '{"tier":"gold","regions":["eu","uk"]}'::jsonb, 'first note', 'legacy A',
     '2026-08-01 09:00:00+00'),

    -- 2. THE STORY: a tracked column changes. Segment moves SMB -> ENTERPRISE, so the
    --    dimension must open a new version rather than overwrite the old one.
    (2, 1002, 'Borealis GmbH', 'SMB', 'DE', 12000.50, 0.75, false,
     'finance@borealis.example', '+49 30 000002', '3f6c1a2e-0000-4000-8000-000000000002'::uuid,
     '{"tier":"silver"}'::jsonb, '', 'legacy B',
     '2026-08-02 10:00:00+00'),
    (2, 1003, 'Borealis GmbH', 'ENTERPRISE', 'DE', 90000.00, 0.81, false,
     'finance@borealis.example', '+49 30 000002', '3f6c1a2e-0000-4000-8000-000000000002'::uuid,
     '{"tier":"gold"}'::jsonb, '', 'legacy B',
     '2026-08-20 11:30:00+00'),

    -- 3. A DUPLICATE with a clear winner. The pick rule (source_updated_at desc, then
    --    source_row_id desc) must keep source_row_id 1005 — the newer one.
    (3, 1004, 'Cygnus SA', 'MID', 'FR', 7000.00, 0.42, false,
     'hello@cygnus.example', '+33 1 000003', '3f6c1a2e-0000-4000-8000-000000000003'::uuid,
     '{"tier":"bronze"}'::jsonb, null, 'legacy C',
     '2026-08-05 08:00:00+00'),
    (3, 1005, 'Cygnus SA', 'MID', 'FR', 7500.00, 0.44, false,
     'hello@cygnus.example', '+33 1 000003', '3f6c1a2e-0000-4000-8000-000000000003'::uuid,
     '{"tier":"bronze"}'::jsonb, null, 'legacy C',
     '2026-08-06 08:00:00+00'),

    -- 4. A FULL-ORDERING TIE: identical source_updated_at. Only the source_row_id
    --    tiebreaker resolves it. Without that tiebreaker the pipeline is
    --    non-reproducible and L2.6 must fire.
    (4, 1006, 'Draco Inc', 'SMB', 'US', 2500.00, 0.31, false,
     'ap@draco.example', '+1 555 000004', '3f6c1a2e-0000-4000-8000-000000000004'::uuid,
     '{"tier":"bronze"}'::jsonb, 'tied row A', 'legacy D',
     '2026-08-07 12:00:00+00'),
    (4, 1007, 'Draco Inc', 'SMB', 'US', 2500.00, 0.31, false,
     'ap@draco.example', '+1 555 000004', '3f6c1a2e-0000-4000-8000-000000000004'::uuid,
     '{"tier":"bronze"}'::jsonb, 'tied row B', 'legacy D',
     '2026-08-07 12:00:00+00'),

    -- 5. NULL business key: belongs in the reject table with NULL_BUSINESS_KEY.
    (5, 1008, '', null, null, null, null, false,
     null, null, null, null, null, null, null),

    -- 6. A test account: filtered out by a DECLARED filter, not lost silently.
    (6, 1009, 'Internal QA', 'SMB', 'GB', 1.00, 0.0, true,
     'qa@internal.example', null, '3f6c1a2e-0000-4000-8000-000000000006'::uuid,
     '{}'::jsonb, null, null, '2026-08-08 07:00:00+00'),

    -- 7. Unicode and multibyte, for the VARCHAR-length-in-bytes-vs-characters trap.
    (7, 1010, 'Ærø Håndværk ApS', 'MID', 'DK', 33333.33, 0.66, false,
     'kontakt@aeroe.example', '+45 00000007', '3f6c1a2e-0000-4000-8000-000000000007'::uuid,
     '{"tier":"silver","note":"æøå"}'::jsonb, '  trailing space  ', 'legacy G',
     '2026-08-09 06:00:00+00');

-- ── orders: the fact source ───────────────────────────────────────────────
create table public.orders (
    order_id     bigint        not null,
    customer_id  integer       not null,
    order_date   date          not null,
    country      varchar(2),
    amount       numeric(12,2) not null,
    status       text          not null,
    is_test      boolean       not null default false,
    placed_at    timestamptz   not null,
    updated_at   timestamptz   not null default now()
);

insert into public.orders
    (order_id, customer_id, order_date, country, amount, status, is_test, placed_at)
values
    (9001, 1, '2026-08-01', 'GB',  120.00, 'COMPLETE', false, '2026-08-01 10:00:00+00'),
    (9002, 1, '2026-08-01', 'GB',   80.50, 'COMPLETE', false, '2026-08-01 14:00:00+00'),
    -- Placed while customer 2 was still SMB. An as-of FK check must resolve it to the
    -- OLD dimension version, not the current one.
    (9003, 2, '2026-08-02', 'DE',  400.00, 'COMPLETE', false, '2026-08-02 12:00:00+00'),
    -- Placed after the segment change: resolves to the new version.
    (9004, 2, '2026-08-21', 'DE', 1500.00, 'COMPLETE', false, '2026-08-21 09:00:00+00'),
    (9005, 3, '2026-08-06', 'FR',  222.22, 'COMPLETE', false, '2026-08-06 09:30:00+00'),
    (9006, 4, '2026-08-07', 'US',   10.00, 'CANCELLED', false, '2026-08-07 13:00:00+00'),
    (9007, 6, '2026-08-08', 'GB',    1.00, 'COMPLETE', true,  '2026-08-08 08:00:00+00'),
    (9008, 7, '2026-08-09', 'DK',  999.99, 'COMPLETE', false, '2026-08-09 07:00:00+00');

-- ── the completed-batch signal (assumption A4) ────────────────────────────
-- Batch 3 is deliberately left RUNNING. A scheduled check must NOT read it: reading
-- mid-load produces false failures, which is the fastest way to lose trust (risk R2).
create table public.etl_batch_log (
    batch_id     integer     not null,
    status       text        not null,
    completed_at timestamptz
);
insert into public.etl_batch_log (batch_id, status, completed_at) values
    (1, 'SUCCESS', '2026-08-10 02:00:00+00'),
    (2, 'SUCCESS', '2026-08-11 02:00:00+00'),
    (3, 'RUNNING', null);

-- A view, to confirm the catalog reader distinguishes views from tables.
create view public.v_active_customers as
    select customer_id, name, segment from public.customers where not is_test;
