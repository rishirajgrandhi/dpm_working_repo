# 13 — Security, PII, and Cost

## 1. Access model

| Principal | Grants | Enforced by |
|---|---|---|
| `DPHM_RDS_READER` (Postgres) | `SELECT` on migrated schemas + catalogs. Nothing else | RDS role; asserted at startup |
| `DPHM_READER` (Snowflake) | `SELECT` on monitored DBs, `USAGE` on `DPHM_WH`, read on `ACCOUNT_USAGE` | Snowflake role |
| `DPHM_WRITER` (Snowflake) | `ALL` on `DPHM_STATE` and `DPHM_SCRATCH` **only** | Snowflake role + a code-level allowlist of modules that may request a writer connection |

The tool cannot write to RDS, and cannot write anywhere in Snowflake except its own two schemas.
That is a property of the grants, not of the code — the code assertion is a second line of
defence and a clearer error message.

**Permission fingerprinting.** At startup the tool hashes its effective grants on both engines and
compares to the previous run. A change is recorded in `PERMISSION_FINGERPRINTS` and reported as a
`PERMISSION_OR_ACCESS_CHANGE` event. Affected checks become `INCONCLUSIVE`, never `FAIL`.
A revoked grant looks exactly like a table full of missing rows; without this, the tool's most
alarming possible alert is also its most likely false one.

## 2. PII

PII is a real constraint here, not a checkbox: the reporting agent sees actual violating rows,
and that counts as **data leaving the warehouse**.

### Five controls

1. **Column-level tagging.** `column_rules.yaml` marks `pii: true`. Snowflake object tags, where
   they exist, are read and merged in as an additional source of truth.
2. **Hash-only comparison.** A PII column is compared as `SHA2(canonical_value, 256)`. Equality
   is preserved; the value never materializes outside the warehouse. This works for parity and
   for immutability checks — the two places PII columns actually need comparing.
3. **Redaction before any LLM call.** `evidence/redact.py` runs as a graph node, not as a helper
   someone might forget to call. PII values become `<pii:sha256:a1b2c3…:varchar(64)>` — enough
   for the model to reason about cardinality and equality, nothing more.
4. **Egress record.** Every LLM call writes an `LLM_EGRESS_LOG` row: node, prompt SHA, model,
   payload kinds, whether row values were included, which PII columns were present, token counts,
   cost. This is the answer to "what left the perimeter", available as a query.
5. **Metadata-only authoring.** The authoring graph sees schemas, code, and cardinality estimates.
   Never row values. Enforced by a CI test asserting no authoring run logs
   `ROW_VALUES_SENT = true`.

### Where row values do exist

| Location | Retention | Control |
|---|---|---|
| `CHECK_RESULTS.SAMPLE_ROWS` | 30 days, then nulled by a scheduled task | Inside Snowflake; PII columns already hashed |
| Slack "view sample rows" modal | Ephemeral | Redacted; PII shows hash prefix only |
| Jira issue body | Permanent | Redacted; sample capped at 10 rows |
| `DPHM_SCRATCH` parity landings | Dropped at end of run; nightly sweeper | Inside Snowflake; never leaves |

Sample row counts are capped (`sample_limit`, default 20). A report needs enough rows to
recognise a pattern, not the failing set.

## 2b. Application access

SSO via the company OIDC provider; no local accounts and no shared service logins for humans.
Session in an httpOnly, SameSite=Lax cookie. Four roles (`12` §6): Viewer, Engineer, Data Owner,
Admin.

Two rules carry real weight:

- **Sign-off and contract confirmation require Data Owner, not Engineer.** If the monitoring team
  can sign off on its own alerts, the board gets rubber-stamped green — the adoption failure the
  design doc names as risk C1.
- **Every mutating action records the acting user.** `grain_confirmed_by`, `signed_by`,
  `answered_by`, and `submitted_by` are people, resolved from the session, never a service account.

Webhook endpoints (`/hooks/*`) are machine-only and authenticated by HMAC signature with a
per-source secret, plus a replay window. They are the only unauthenticated-by-session surface, and
they can only *trigger* work — never read results or change state directly.

## 3. Secrets

- Env vars only, backed by the platform secret manager. Per-project isolation from day one.
- A secret literal in a YAML file is a **load-time error**, detected by an entropy + pattern scan
  in `config/loader.py`.
- Snowflake auth is key-pair (`snowflake_jwt`); no passwords in the target path.
- Nothing is logged from the connection layer except host, database, role, and user.

## 4. Cost

### Structure

| Lane | Cost posture |
|---|---|
| A | Cheap by construction — templated SQL, gate 1 caps every check, dedicated XS warehouse |
| B | The expensive one — hard per-run budget with an abort switch at tier boundaries |
| LLM | Two calls per Lane A run in the common case; budget-capped, and every call logged |

### Budgets (`project.yaml`)

```yaml
budgets:
  lane_a_warehouse_seconds_per_run: 900     # ~15 min of XS = cents
  lane_b_usd_per_run: 5.00
  lane_b_rds_seconds_per_run: 600
  llm_usd_per_run: 2.00
```

### Accounting

- Every query carries `QUERY_TAG = 'dphm:{run_id}:{check_id}'`; credits are read back from
  `ACCOUNT_USAGE.QUERY_HISTORY` and written to `CHECK_RESULTS.ESTIMATED_USD`.
- RDS cost is elapsed time × a configured rate — approximate, but enough to bound Lane B.
- LLM cost comes from the callback's token counts × the model's published rate.
- `RUNS` carries the totals, so "monitoring cost per pipeline per month" is one query, not an
  estimate.

### Abort behaviour

Budget overrun **aborts at a tier or check boundary** and returns `PARTIAL`. It never
silently truncates a comparison and reports green. Partial is an honest state; a truncated green
is not.

## 5. Data residency

If residency requires it, `agents/llm.py` swaps `ChatAnthropic` for `ChatBedrock` or the Vertex
equivalent. That is the only file that changes — one more reason the LLM sits behind a factory
and structured output rather than being scattered through the codebase.

## 6. Audit trail

Answerable from `DPHM_STATE` alone, with no log archaeology:

| Question | Source |
|---|---|
| What did we check on date D, and with what SQL? | `RUNS` + `CHECK_RESULTS.SQL_SHA256` + `CHECKS.PARAMS` |
| What did we exclude, and why? | `CHECK_RESULTS.COLUMNS_EXCLUDED` + `EXCLUSION_REASONS` |
| What data left the perimeter, when, to which model? | `LLM_EGRESS_LOG` |
| Who signed off on what, against which commit? | `SIGNOFFS` |
| Did our access change? | `PERMISSION_FINGERPRINTS` |
| Who marked this a false positive? | `FEEDBACK` |
| Who confirmed this grain or dedup contract? | `REVIEW_ITEMS.ANSWERED_BY` |
| Who triggered this run? | `JOBS.SUBMITTED_BY` |
| Which prompt version produced this narrative? | `RUNS.PROMPT_SHA` |
