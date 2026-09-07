-- Grant elevated access to the dphm service user.
--
-- Run as ACCOUNTADMIN. One statement, one statement to undo it.
--
-- WHY THIS USER AND NOT YOURS:
--   * Your own login and password stay yours. Nothing is shared.
--   * Every action is attributable to DPHM_DEV_SVC in QUERY_HISTORY, so you can audit
--     exactly what was done and by whom.
--   * Revoking is one line and takes effect immediately.
--   * The key pair already works, so there is nothing new to set up.

use role ACCOUNTADMIN;

grant role ACCOUNTADMIN to user DPHM_DEV_SVC;

-- DELIBERATELY NOT SET: default_role stays DPHM_DEV.
--
-- That means every connection lands in the low-privilege role and must ask for
-- ACCOUNTADMIN explicitly, per session. Elevated access is available when a provisioning
-- task genuinely needs it, and is never the accidental default.
--
-- Confirm that is still the case:
show parameters like 'default_role' for user DPHM_DEV_SVC;
desc user DPHM_DEV_SVC;

-- ── Verify ────────────────────────────────────────────────────────────────
-- Expect DPHM_DEV and ACCOUNTADMIN.
show grants to user DPHM_DEV_SVC;

-- ═══════════════════════════════════════════════════════════════════════════
-- REVOKE — one line, immediate. Everything in DPHM_TEST keeps working, because
-- DPHM_DEV is a separate grant.
-- ═══════════════════════════════════════════════════════════════════════════
-- use role ACCOUNTADMIN;
-- revoke role ACCOUNTADMIN from user DPHM_DEV_SVC;
