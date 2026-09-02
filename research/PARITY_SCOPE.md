# Parity Checks — Clearer Scope

Narrower companion to [SCOPE.md](SCOPE.md). This one answers a single question: **in a stack like the one described — API sources (Ringside, Tradable Bits, …) landing into a Snowflake bronze/silver/gold medallion, while an existing RDS + dbt-on-Redshift estate is being re-implemented in Snowflake — what exactly is a "source", what is a "target", and what must the application therefore be able to do?**

The organising insight: **a parity check is not "A equals B". It is an assertion about a declared *relationship* between A and B.** Most homegrown parity tooling fails because it assumes `identical` everywhere, then drowns in explained-away failures. Getting the relationship vocabulary right is most of the scope work.

---

## 1. The Four Parity Families in This Stack

Everything in this environment falls into one of four families. They look similar but assert different things, need different evidence, and fail for different reasons.

| # | Family | Asserts | Lifespan | Difficulty |
|---|---|---|---|---|
| **F1** | Source → Bronze | Nothing was lost or duplicated in ingestion | Permanent | Hard — the source is often not queryable |
| **F2** | Bronze → Silver | Every bronze row is accounted for: kept, deduped, filtered, or rejected | Permanent | Medium — deliberately *not* equality |
| **F3** | Silver → Gold | Gold measures are re-derivable from silver at a declared grain | Permanent | Medium — grain and fanout are the traps |
| **F4** | Legacy (RDS / Redshift-dbt) ↔ Snowflake | The new stack reproduces the old stack's outputs | Migration + shadow period, then retire | Hardest — cross-dialect, both sides moving |

F1–F3 are *vertical* (down one pipeline). F4 is *horizontal* (across two pipelines that should agree). **F4 is the one described as ultimately the key**, and it is the one with a deadline attached — so §5 treats it in depth.

An important structural point: F4 can be checked at *every* layer, giving a grid rather than a list:

```
                 LEGACY (RDS + Redshift/dbt)          NEW (Snowflake)
  ingestion  ┌─ RDS tables / Redshift raw ──── F4a ──── bronze ─┐
             │        ▲                                    ▲    │
             │        F1(legacy)                       F1(new)  │
             │        └──── API: Ringside, TradableBits ─┘      │
  transform  ├─ Redshift staging/int ───────── F4b ──── silver ─┤
             │        ▲ F2(legacy)                 ▲ F2(new)    │
  serving    ├─ Redshift marts ──────────────── F4c ──── gold ──┤
             │        ▲ F3(legacy)                 ▲ F3(new)    │
  consumers  └─ BI / exports ────────────────── F4d ── BI/exports
```

`F4c` (marts vs gold) is the business-visible one and the one cutover sign-off actually rests on. `F4a` (raw landing) is where divergence is cheapest to diagnose. Checking both ends and drilling inward on failure is the efficient strategy — see §7.

---

## 2. Relationship Vocabulary (the core abstraction)

Every check should declare `relation:` rather than implying equality. This single field determines which comparison logic runs, what counts as a failure, and what RCA should look for.

| `relation` | Meaning | Where used | Failure means |
|---|---|---|---|
| `identical` | Same grain, same key set, same values after canonicalization | bronze copy vs legacy raw; replica parity | Real loss, corruption, or duplication |
| `conserved` | `input = output + declared_losses`, every loss attributed to a named rule | bronze → silver | Unexplained row loss (the dangerous kind) |
| `filtered_from` | Target ⊂ source, difference matches declared predicates | silver excluding test accounts / invalid records | Filter is over-matching or a new data pattern hits it |
| `dedup_of` | Target = distinct source on key, with a **deterministic** pick rule | bronze → silver | Duplicates survived, or the pick rule is non-deterministic |
| `flattened_from` | Relational rows derived from nested JSON | bronze VARIANT → silver columns | A JSON field or array element was silently dropped |
| `aggregate_of` | Target = group-by of source with declared measure formulas | silver → gold | Fanout, wrong join, non-additive measure summed |
| `equivalent_to` | Different platform/dialect, same intent, same declared grain and measures | Redshift marts ↔ Snowflake gold | Migration defect **or** a dialect semantic difference |
| `as_of_equal` | Equal restricted to a closed window `t <= watermark − grace` | any cross-system pair with independent schedules | Real divergence rather than lag |
| `reconciles_to` | Equal within an explicit tolerance to an external number | internal revenue vs settlement report | Beyond-tolerance discrepancy |
| `superset_of` | Target legitimately has more (e.g. deletes not propagated) | CDC-gap situations | Target is *missing* rows |
| `unvalidated` | Declared: no counterpart exists to compare against | net-new Snowflake models with no legacy twin | Nothing — but it must appear in the coverage report |

That last row matters more than it looks. For a migration, **knowing what you cannot validate is part of the deliverable.** A cutover scorecard that silently omits unvalidatable objects is misleading.

---

## 3. What Can Be "the Source"

Concretely, in this stack, in rough order of how often it will be the left-hand side of a check:

**Queryable systems**
1. **Snowflake** — bronze, silver, gold; also the state/monitoring schema
2. **Snowflake at an earlier timestamp** — Time Travel; the source-of-truth for "did history change?"
3. **Legacy Redshift** — raw / staging / intermediate / mart layers as built by the legacy dbt project
4. **Operational RDS** (Postgres/MySQL) — always via a replica or a snapshot, never a heavy scan on prod
5. **Reference/config data** — seed tables, mapping tables, hand-maintained spreadsheets. Disproportionately often the actual breaker: someone adds a venue or product code and nothing maps it.

**Non-queryable sources (the interesting problem)**
6. **External APIs** — Ringside, Tradable Bits, ticketing, CRM, social, commerce, loyalty. Cannot be `SELECT`-ed and cannot be cheaply counted.
7. **Landed raw payloads** — the JSON/CSV files the extractor wrote to S3/internal stage before parsing. **This is the practical stand-in for the API as source of truth** (§4).
8. **Ingestion manifests / extractor logs** — the extractor's own record of what it fetched: pages requested, records received, cursor from→to, HTTP status counts, retries, bytes. Comparable against bronze without touching the API again.

**Metadata as a source**
9. **dbt artifacts** — the legacy project's `manifest.json` (expected model set + lineage + owners) and `run_results.json`. Also the new project's, if dbt is used on Snowflake. Comparing the two manifests is the cheapest way to generate the F4 object mapping (§5.2).
10. **Warehouse metadata** — `INFORMATION_SCHEMA`, `COPY_HISTORY`, `LOAD_HISTORY`, `ACCESS_HISTORY`, Redshift `STL_*`/`SVL_*`.

**External truth**
11. **Vendor-reported totals** — an API's own `meta.total`/count endpoint, a vendor's UI export, a settlement or box-office report. Use with `reconciles_to`, never `identical`.
12. **BI layer numbers** — what a dashboard actually renders. The last mile, and the one stakeholders judge the migration by.

**Targets** are drawn from the same list — mostly Snowflake layers, but also reverse-ETL destinations, exports back out to Tradable Bits-style platforms, and BI extracts.

---

## 4. F1: Source → Bronze, when the source is an API

This is the family that needs a decision before anything can be built, because **there is usually no queryable "source" to compare against.** The options, best-first:

**(a) Raw payload retention as the reference.** The extractor writes the raw API response to a stage/S3 before parsing. Parity then becomes an intra-Snowflake check: *records present in the landed payload files* vs *rows in bronze*. Fully queryable (external table / `PARSE_JSON`), cheap, and it also gives replay capability. If payload retention isn't already happening, adding it is the single highest-value change to make this family work.

**(b) Ingestion manifest reconciliation.** The extractor emits a per-run record: endpoint, cursor window, pages fetched, records received, HTTP error counts, retries. Check: `manifest.records_received == bronze rows loaded for that run`, plus `http_errors == 0`, plus cursor continuity. Catches the most common real failure — *a partial fetch that exited 0*.

**(c) API-reported totals for closed periods.** Call the count/`meta.total` endpoint for a period that can no longer change (e.g. two days ago) and compare to bronze. Cheap, but only as trustworthy as the vendor's own counter — many vendors' totals disagree with their detail endpoints, so treat as `reconciles_to` with tolerance.

**(d) Bounded replay.** Re-fetch a small window and diff against bronze. Highest fidelity, highest cost and rate-limit risk. Use as *on-failure evidence gathering*, not as a routine check.

**API-specific pathologies the application must handle**, all of which have burned real ingestion pipelines:
- **Pagination truncation** — a page-limit or cursor reset ends the fetch early; the job succeeds. Detect via cursor continuity + per-run count vs history.
- **Rate limiting treated as end-of-data** — 429 handled as "no more records".
- **Silent soft deletes** — records vanish from the API with no delete event, so bronze accumulates rows the source no longer has. This must be declared (`superset_of`) or it alarms forever.
- **Retroactive restatement** — refunds, ticket transfers, fan-profile merges, attribution updates. Yesterday's numbers legitimately change. Strict `identical` on recent windows is guaranteed to false-alarm; use `as_of_equal` on closed periods plus a self-parity check on *frozen* periods to catch restatement that should *not* have happened.
- **Overlapping incremental windows** producing duplicates — needs PK-uniqueness checks on bronze, not just counts.
- **JSON schema drift** — new fields appear, nested arrays change shape, a scalar becomes an array. Check the *observed key set* of bronze VARIANT against the last-known set; a new key is informational, a *disappeared* key is an alert.
- **Timezone semantics** — event-local time vs UTC vs venue time. Ticketing/event data is the worst offender, and it moves row counts across day boundaries, which then looks like row loss in a daily-partitioned check.
- **API version / sandbox-vs-prod** changes.

---

## 5. F4: Making Sure the Snowflake Build Actually Mimics RDS + Redshift-dbt

This is the key ask, so in detail. What the application must deal with:

### 5.1 A mapping registry — the migration spec as code
`identical` is meaningless without knowing *which* Snowflake object corresponds to *which* legacy object. This cannot be inferred reliably; it must be declared:

```yaml
- id: f4c_fan_engagement_daily
  relation: equivalent_to
  legacy:   { platform: redshift,   object: analytics.mart_fan_engagement_daily }
  new:      { platform: snowflake,  object: GOLD.MARTS.FAN_ENGAGEMENT_DAILY }
  grain:    [event_date, fan_id, channel]        # declared, not guessed
  measures: [sessions, ticket_scans, spend_usd]
  column_map:
    fan_engagement_score: ENGAGEMENT_SCORE
  expected_differences:
    - column: spend_usd
      reason: "legacy summed pre-refund; new nets refunds — deliberate fix, ticket DATA-812"
      direction: new_lower
    - column: channel
      reason: "legacy emitted NULL, new emits 'unknown' — normalization"
  legacy_owning_paths: [ legacy_dbt/models/marts/mart_fan_engagement_daily.sql ]
  new_owning_paths:    [ pipelines/gold/fan_engagement_daily.sql ]
```

Two things this file quietly buys you beyond checking: it is the **auditable record of the migration's intent**, and `expected_differences` is what keeps the parity report from being 60% known-and-accepted noise. A migration is almost always also a cleanup — bugs fixed, columns renamed, grain changed — and without a place to record "this difference is intentional and here's why", the tool gets ignored within a week.

### 5.2 Generating that mapping cheaply
If the legacy project is dbt and the new one is too (or has consistent naming), diff the two `manifest.json` files: model names usually survive a migration, so most of the mapping auto-generates, leaving humans to resolve only the renamed/merged/split/net-new cases. That residue list is itself valuable — it's the set of places where the migration changed shape, which is exactly where defects concentrate. Any legacy model with no counterpart is either a deliberate drop or an omission; the tool should force that to be stated rather than discovered post-cutover.

### 5.3 Dialect semantics that silently change results
The reason cross-platform "equivalent" SQL disagrees is rarely a typo. The recurring culprits, worth encoding as both canonicalization rules and RCA hypotheses:

- **`NULLS FIRST/LAST` defaults differ** in `ORDER BY` and window functions. Combined with `row_number()` dedup, **a different row survives** — same row count, different values, no obvious cause. This is the classic silent migration bug.
- **Ties in `row_number()`/`first_value` without a full ordering** — legitimately non-deterministic on *both* platforms. The tool must be able to say "this divergence is caused by a non-deterministic source query, not a migration defect," otherwise it will chase ghosts.
- **Numeric type and rounding** — Redshift vs Snowflake differ in division result types, decimal scale propagation through aggregates, rounding mode, and overflow behaviour. Fixed-scale casting before comparison is mandatory.
- **Float aggregation order** — `SUM(float)` is order-dependent; distributed engines don't guarantee order. Never hash floats; compare with tolerance.
- **VARCHAR length in bytes vs characters** — Redshift counts bytes, Snowflake counts characters. Multibyte fan names and addresses truncate differently. Look for values sitting at exactly the declared length.
- **Timestamp handling** — `TIMESTAMP` vs `TIMESTAMPTZ`, session timezone parameters, `getdate()` vs `current_timestamp()`, DST edges. Normalize to UTC at a declared precision.
- **JSON functions are entirely different** — Redshift's `JSON_EXTRACT_PATH_TEXT` on varchar vs Snowflake `VARIANT`/`PARSE_JSON`/`:` access, with different behaviour on missing keys, type coercion, and nulls. **For an API-sourced stack this is the highest-probability divergence source in the whole migration.**
- **Empty string vs NULL**, implicit cast rules, `LIKE`/regex flavour differences (POSIX vs Snowflake regex), `SPLIT_PART` edge cases, `LISTAGG` ordering, percentile/median interpolation and approximate-vs-exact distinct counts.
- **Identifier casing** — Redshift lowercases, Snowflake uppercases unquoted identifiers. A column-mapping nuisance rather than a data bug, but it breaks naive automated mapping.

### 5.4 Both sides are moving
The legacy stack keeps getting bugfixes during migration; the new stack evolves daily; the two run on different schedules. Consequences the application must handle: `as_of_equal` on closed periods only; a grace window with re-verify before alerting; and **git history for *both* repos**, because the first RCA question is "did legacy change or did new change?" A divergence caused by a legacy hotfix that wasn't ported is a completely different ticket than a new-stack defect.

### 5.5 The output is a cutover decision, not just tickets
For F4, the primary artifact should be a **migration readiness scorecard**, per object: current parity state, magnitude of any diff, consecutive days at parity, unresolved differences by severity, accepted-difference count, and coverage (validated / unvalidated / no-counterpart). "Object X has been at parity for 14 consecutive days across 3 full month-end closes" is what actually licenses cutover, and it is a much better fit for this family than a stream of Jira tickets. Tickets are right for F1–F3 steady-state operations; a scorecard is right for a migration.

Then: keep the checks running for a **shadow period after cutover** — same machinery, now catching regressions in the new stack against a still-running legacy reference — and retire them deliberately when legacy is decommissioned. That transition (F4 checks retiring, F1–F3 checks remaining) should be an explicit lifecycle in the config, not an orphaned pile of failing checks nobody dares delete.

---

## 6. F2 and F3 Briefly — because they are *not* equality

**Bronze → Silver.** The useful check is conservation, not count equality:

```
bronze_rows = silver_rows + deduped + filtered_by_rule + rejected_to_quarantine
```

Every removed row must be attributed to a named rule, and each rule's volume should sit in an expected band — a filter that suddenly removes 10× more rows is a defect even though the arithmetic still balances. On top of that: PK uniqueness on silver, cast-failure rate (a `TRY_CAST` that quietly yields NULL is invisible row-level data loss), JSON flatten completeness against the observed bronze key set, array-explosion cardinality guards, and SCD2 correctness (no overlapping ranges, exactly one current row per key).

**Silver → Gold.** Recompute the aggregate independently from silver and compare to gold — the check is a *second implementation*, which is what makes it meaningful. Plus: declared grain enforcement (one row per declared key tuple), join-fanout guards (output rows within an expected multiple of input — the single most common gold defect), dimension conformity (every fact FK resolves; watch the `unknown`/`-1` bucket growing), additive-vs-non-additive measure discipline (distinct counts do not sum across slices), and internal consistency (sum of slices = reported total; daily rolls up to monthly).

---

## 7. Where to Point It First: edges first, drill on failure

With four families across four layers and two platforms, the check count explodes fast. The efficient strategy:

**Check the edges.**
- **F1 at the ingest edge** — did everything the API gave us land in bronze?
- **F4c at the business edge** — do legacy marts and Snowflake gold agree on the numbers people actually read?

If both edges hold, the middle is very likely fine. When an edge breaks, **then** run the intermediate layer checks (F4a, F2, F4b, F3) as *localization*, narrowing to the earliest layer where divergence appears. This is the same cheap-tier / on-failure-escalation pattern from SCOPE.md §4, applied to the medallion: it cuts routine check volume by most of an order of magnitude and produces better RCA, because "divergence originates at silver, bronze is clean" is already half the diagnosis.

Corollary for RCA: the application must **know the layer graph and the legacy↔new correspondence**, so that when 30 checks fail it reports one incident at the earliest failing layer rather than 30 tickets. In a medallion stack, a single bronze problem fans out to nearly everything downstream — grouping is not a nice-to-have here, it's the difference between usable and unusable.

---

## 8. Consolidated: What the Application Must Deal With

Directly answering the question. To be genuinely useful in this environment, the system needs:

**Connectivity & comparison**
1. Two **independent** connections per check, with a dialect abstraction covering Snowflake, Redshift, and RDS (Postgres/MySQL) — plus a read path for staged raw files.
2. A **canonicalization layer** with per-engine, per-type rules (fixed-scale numerics, UTC timestamps at declared precision, string trim/case/Unicode policy, explicit NULL sentinel, never-hash-floats) and per-check overrides. Without this, cross-platform checksums will simply never match and the project stalls at week two.
3. **Cross-engine localization on failure**: bucketed/bisecting hash to find diverging key ranges, then a bounded sampled cell diff to name the offending column. Naming the column is what turns a ticket from "counts differ" into "`spend_usd` is 100× low — minor-units bug".
4. **Semi-structured comparison** — Snowflake `VARIANT` vs Redshift JSON-in-varchar, with key-set tracking rather than blind whole-blob hashing.

**Semantics**
5. The **`relation` vocabulary** of §2, so checks assert the right thing instead of assuming equality.
6. **Conservation accounting** for bronze→silver, with named loss rules and per-rule volume bands.
7. **Declared grain and measures** for gold-layer and `equivalent_to` checks — aggregates have no natural row key, so the grain must be stated.
8. A **mapping registry with `expected_differences`** (§5.1) — the migration spec as code, and the thing that keeps the report signal-dense.
9. **Watermark / as-of / closed-period-only comparison**, plus a grace window and automatic re-verify before alerting. Most cross-system divergence is lag.
10. A **tolerance and severity model**: relative and absolute thresholds, an absolute floor so small tables don't alarm on one row, directional thresholds, and money tolerance in minor units.

**Sources that can't be queried**
11. Support for **raw-payload-as-reference** and **ingestion-manifest reconciliation** as first-class check inputs (§4), since the APIs themselves are not a queryable source of truth.
12. **Cursor/watermark continuity** checks — no gaps, no resets, monotonic advance — because partial fetches exit successfully.

**Diagnosis & routing**
13. **Layer-graph and legacy↔new awareness**, so failures are reported at the earliest failing layer and downstream fan-out collapses into one incident.
14. RCA hypothesis classes specific to this stack, on top of the generic set: *dialect semantic difference*, *non-deterministic source query*, *legacy-side change not ported*, *pagination/rate-limit truncation*, *upstream restatement*, *JSON schema drift*, *filter over-matching*, *accepted intentional difference*.
15. **Ownership across three repos** — the legacy dbt/Redshift project, the new Snowflake pipeline code, and the ingestion/extractor code — since which repo the fix belongs in is the routing decision.
16. **Two output modes**: incident tickets for steady-state F1–F3, and a **migration readiness scorecard** for F4, including honest coverage reporting of what could not be validated.

**Constraints**
17. **Cost governance on both engines**, with particular care on the legacy Redshift cluster, which is likely capacity-constrained and shared with real workloads — partition-scoped predicates, metadata-only tier, hard per-check budgets.
18. **PII discipline** — fan and ticketing data is names, emails, addresses, and behaviour. Column-level PII annotation, hash-only comparison for sensitive columns, and redaction before anything reaches an LLM. Not optional given the data domain.
19. **A check lifecycle** — F4 checks are born with the migration, run through a post-cutover shadow period, and are retired deliberately; F1–F3 checks are permanent. Encode that so retired checks don't rot into ignored red.

---

## 9. Sketch of the Config This Implies

```yaml
# F1 — API → bronze, using retained raw payloads + manifest
- id: f1_tradablebits_fans
  relation: conserved
  source: { type: stage_json, location: "@RAW.STAGE.TRADABLEBITS/fans/", window: "{{ run_date }}" }
  target: { type: snowflake,  object: BRONZE.TRADABLEBITS.FANS }
  manifest: { type: snowflake, object: OPS.INGEST_RUNS, filter: "source='tradablebits'" }
  assertions: [payload_records_eq_bronze_rows, manifest_records_eq_bronze_rows,
               cursor_continuity, pk_unique(fan_id, _loaded_batch)]
  owning_paths: [ingestion/tradablebits/extract.py]

# F2 — bronze → silver, conservation with attributed losses
- id: f2_fans_bronze_to_silver
  relation: conserved
  source: BRONZE.TRADABLEBITS.FANS
  target: SILVER.FAN.FANS
  losses:
    - { rule: dedup_latest_by_updated_at, expect_pct: [0, 15] }
    - { rule: exclude_test_accounts,      expect_pct: [0, 2] }
    - { rule: reject_missing_fan_id,      expect_pct: [0, 0.5], quarantine: SILVER.FAN.FANS_REJECTS }
  assertions: [json_keyset_stable, cast_failure_rate_lt(0.001), pk_unique(fan_id)]

# F3 — silver → gold, independent recomputation at a declared grain
- id: f3_fan_engagement_daily
  relation: aggregate_of
  source: SILVER.FAN.ENGAGEMENT_EVENTS
  target: GOLD.MARTS.FAN_ENGAGEMENT_DAILY
  grain: [event_date, fan_id, channel]
  measures:
    sessions:     "count(distinct session_id)"
    spend_usd:    "sum(amount_usd)"
  assertions: [grain_unique, fanout_ratio_lte(1.0), dim_conformity(channel), slices_sum_to_total]
  window: "event_date < current_date - 1"      # closed periods only

# F4c — legacy Redshift mart ↔ Snowflake gold  (see §5.1 for the full form)
- id: f4c_fan_engagement_daily
  relation: equivalent_to
  legacy: { platform: redshift,  object: analytics.mart_fan_engagement_daily }
  new:    { platform: snowflake, object: GOLD.MARTS.FAN_ENGAGEMENT_DAILY }
  grain: [event_date, fan_id, channel]
  canonicalize: { numeric_scale: 2, timestamps: utc_seconds, strings: trim, nulls: sentinel }
  tolerance: { spend_usd: { abs_minor_units: 1 } }
  as_of: { column: event_date, closed_period: true, grace: 26h }
  lifecycle: { phase: migration, retire_after: legacy_decommission }
```

The point of the sketch is not the syntax — it's that all four families and both platforms fit one model, distinguished by `relation`, `losses`/`measures`, `canonicalize`, `as_of`, and `lifecycle`. Those five fields are the scope of the parity engine.

---

## 10. What I'd Nail Down Before Building

1. **Are raw API payloads retained today?** If not, F1 has no reference and adding retention is the first task.
2. **Does the extractor emit a per-run manifest?** If not, that's the second task — it's a few lines and it makes ingestion parity possible at all.
3. **Is the legacy transform layer dbt on Redshift, and is the new one dbt too?** If both, manifest-diff auto-generates most of the F4 mapping. If the new side is hand-written SQL/Python, the mapping is manual and should be treated as a deliverable in its own right.
4. **Which gold/mart objects are the cutover gate?** Those get F4c checks first; the rest can wait.
5. **Are intentional legacy-vs-new differences currently written down anywhere?** If they only live in people's heads, capturing them into `expected_differences` is a prerequisite, not a nice-to-have.
6. **Do bronze/silver tables carry a reliable `updated_at`/batch/watermark column?** Determines whether `as_of_equal` is possible or everything needs snapshot-based comparison.
7. **How much Redshift capacity can monitoring use, and when?** Sets the ceiling on F4 check depth and frequency.
8. **What is the cutover date?** It sets the F4 lifecycle and decides whether the scorecard or the ticketing path is built first.
