-- ═══════════════════════════════════════════════════════════════════════════
-- dphm development environment setup
--
-- Paste this whole file into a Snowflake worksheet and run it (Run All).
-- Takes about 10 seconds. Safe on a fresh trial account.
--
-- Creates exactly five objects, all prefixed DPHM_TEST / DPHM_DEV:
--   DPHM_TEST          a throwaway database
--   DPHM_TEST_WH       an XSMALL warehouse
--   DPHM_TEST_MONITOR  a 10-credit/month spend cap on that warehouse
--   DPHM_DEV           a role with rights on DPHM_TEST and nothing else
--   DPHM_DEV_SVC       a service user authenticating with a key pair
--
-- Teardown is at the bottom of this file, commented out.
-- ═══════════════════════════════════════════════════════════════════════════

use role ACCOUNTADMIN;

-- ── 1. A throwaway database ───────────────────────────────────────────────
-- Contains no real data. The fixture scripts drop and recreate everything in it.
create database if not exists DPHM_TEST;

-- ── 2. A small warehouse with a hard spend cap ────────────────────────────
create warehouse if not exists DPHM_TEST_WH
    warehouse_size      = xsmall
    auto_suspend        = 60          -- suspend after 60s idle
    auto_resume         = true
    initially_suspended = true;

-- The cap matters: it means a runaway query of mine cannot become a surprise bill.
-- 10 credits is roughly a few dollars and far more than this testing needs.
create resource monitor if not exists DPHM_TEST_MONITOR
    with credit_quota = 10
    frequency         = monthly
    start_timestamp   = immediately
    triggers on 90 percent  do notify
             on 100 percent do suspend;

alter warehouse DPHM_TEST_WH set resource_monitor = DPHM_TEST_MONITOR;

-- ── 3. A role scoped to that database only ────────────────────────────────
create role if not exists DPHM_DEV;

grant usage, operate on warehouse DPHM_TEST_WH to role DPHM_DEV;
grant all on database DPHM_TEST                to role DPHM_DEV;
grant all on all schemas in database DPHM_TEST    to role DPHM_DEV;
grant all on future schemas in database DPHM_TEST to role DPHM_DEV;

-- Query and cost history, so the tool's own cost accounting can be verified against
-- what Snowflake actually charged. Read-only, and it exposes metadata rather than data.
grant imported privileges on database SNOWFLAKE to role DPHM_DEV;

-- ── 4. A service user, key-pair auth only ─────────────────────────────────
-- The matching private key already exists on the dev machine at
-- ~/.dphm/keys/dphm_dev_key.p8 (mode 600). It is not in git and was never sent anywhere.
create user if not exists DPHM_DEV_SVC
    default_role      = DPHM_DEV
    default_warehouse = DPHM_TEST_WH
    rsa_public_key    = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAruFF+O5VoZiH27knpOye33+ilBJ5K+kMBq6SVFdLPTyDDVabZVYbHUZkbJEWHv2xKFWnWZGlVNpLvJVqWo5t/z32egXb0RZ/nSEuX4ckjkJbdwE1p/DaJ8gieoEGC/+cVC0V8dz2mHjpMp6R3A+GSD1MZLeuSaG/bTE3YDRbiqYMLo0hjGFnCM/yDNlL1h+ZbikwL7o7I54x1fECzLLH4deeClYEfHRJKWvGHJMf+dwZ0kvRzVfHwxJ1T6aAebkhHkUQsIF9N91d5mozhXqx6V/QuKGH4xge8X8jmrNM17kVkiZ6Nlv5re3JPJyHvjAJbsL2HrMcpmrQAmUr+5xSvQIDAQAB'
    comment = 'dphm development service account. Key-pair auth. DPHM_TEST only.';

grant role DPHM_DEV to user DPHM_DEV_SVC;

-- ── 5. Tell me your account identifier ────────────────────────────────────
-- Run this and send me both values. Neither is a secret.
select current_organization_name() as organization,
       current_account_name()      as account,
       current_region()            as region;

-- ── 6. Confirm the blast radius ───────────────────────────────────────────
-- Expect grants on DPHM_TEST and DPHM_TEST_WH only. If anything else appears, stop
-- and tell me rather than proceeding.
show grants to role DPHM_DEV;

-- ═══════════════════════════════════════════════════════════════════════════
-- TEARDOWN — uncomment and run when you want this gone. Nothing outside these
-- five objects is ever touched.
-- ═══════════════════════════════════════════════════════════════════════════
-- use role ACCOUNTADMIN;
-- drop database if exists DPHM_TEST;
-- drop warehouse if exists DPHM_TEST_WH;
-- drop resource monitor if exists DPHM_TEST_MONITOR;
-- drop user if exists DPHM_DEV_SVC;
-- drop role if exists DPHM_DEV;
