# 06 — Check Model, the SCD Suite, and SQL Templates

## 1. What a check is

A check is defined by seven things:

```
lane  ·  trigger  ·  what it covers  ·  the relationship it asserts  ·  scope  ·  column rules  ·  tolerance
```

The **relationship** is what makes this flexible. A check is not "A equals B"; it asserts
something specific:

| Relationship | Lane | Meaning |
|---|---|---|
| `one_row_per_key` | A | The declared grain is actually unique |
| `valid_scd2_history` | A | The 11-check SCD suite |
| **`dedup_of`** | A | **B = distinct A on a declared key, by a total deterministic pick rule.** The bronze→silver contract — see `07B` §4 |
| `aggregate_of` | A | B = group-by of A with declared, additive measure formulas — see `07B` §5.3 |
| `filtered_from` | A | B ⊂ A, and the difference matches declared predicates |
| `conservation` | A | input rows = output rows + named losses |
| `fk_resolves` | A | Every foreign key finds a parent |
| `value_in_range` / `value_in_enum` | A | Domain constraints hold |
| `business_rule` | A | A hand-written, reviewed predicate |
| `aggregate_matches` | A | A mart agrees with its declared inputs |
| `exact_match` | B | RDS and Snowflake agree row-for-row on business columns |
| `filtered_subset` / `superset` | B | With a declared predicate |
| `unvalidated` | — | **We have no way to check this yet.** Reported, never silently omitted |

`unvalidated` is a first-class relationship. Knowing what we *cannot* check is part of the
product — a green dashboard that hides untested objects is the failure mode this tool exists
to prevent.

## 2. Execution pipeline

```
CheckDef (YAML)
   └─▶ resolve columns      columns.py   → compared / excluded lists, hash-only PII columns
   └─▶ resolve scope        scope.py     → batch predicate, e.g. BATCH_ID IN (completed batches)
   └─▶ render               render.py    → Jinja2 template + params → SQL text + sql_sha256
   └─▶ execute              execute.py   → Snowflake, timeout + row cap, query_id captured
   └─▶ CheckResult          result.py    → status, failing_row_count, sample rows, fingerprint,
                                            columns_compared, columns_excluded, cost, sql_sha256
```

### Template contract

Every template **must** return a uniform shape, so the engine never needs template-specific logic:

```sql
-- Every check template returns zero or more VIOLATION rows.
-- Zero rows = PASS. The engine never interprets a scalar.
select
    <grain columns>            as failing_key,      -- object, or object|json for composite
    '<violation_code>'         as violation_code,   -- from a closed vocabulary
    <detail expression>        as detail            -- OBJECT_CONSTRUCT, redacted per column rules
from ...
where ...
limit {{ sample_limit }}
```

Two consequences that matter:

- Pass/fail is "did this return rows", so a template cannot accidentally encode a threshold.
- `failing_key` is what the incident fingerprint is built from, so dedup works identically for
  every check family without special-casing.

## 3. The SCD suite — 11 checks per SCD2 dimension

Built from a business key plus the type-1 / type-2 / validity columns declared in `manifest.yaml`.
All eleven are Lane A, scheduled, Snowflake-only SQL.

| # | Template | Asserts | Catches |
|---|---|---|---|
| 1 | `scd/one_current_row_per_key` | Exactly one `IS_CURRENT` row per business key | Duplicate or missing current record |
| 2 | `scd/no_overlapping_ranges` | Validity ranges per key never overlap | Double-counting in as-of joins |
| 3 | `scd/no_gaps_in_history` | Consecutive versions are contiguous | Lost history windows |
| 4 | `scd/well_formed_ranges` | `VALID_FROM < VALID_TO`, no NULL `VALID_FROM`, open row uses the sentinel | Inverted / malformed ranges |
| 5 | `scd/current_flag_agrees_with_range` | `IS_CURRENT` ⇔ `VALID_TO` is the open sentinel | Flag and range drifting apart |
| 6 | `scd/no_spurious_versions` | A new version exists only if a **tracked** column changed | Churn: a new row on every load |
| 7 | **`scd/tracked_change_created_version`** | A tracked change **did** create a new version | **The dimension quietly becoming an overwrite** |
| 8 | `scd/type1_consistent_across_versions` | Type-1 columns are identical across all versions of a key | Type-1 update applied to only the current row |
| 9 | `scd/surrogate_key_unique` | Surrogate key is globally unique and never NULL | Broken fact joins |
| 10 | `scd/foreign_keys_resolve` | Declared FKs resolve to a parent version valid at that time | Orphaned facts |
| 11 | `scd/closed_versions_immutable` | A closed version's content hash never changes between runs | Silent back-dated rewrites |

**Check 7 is the one that motivates the product.** It is also the only one that needs a
comparison against upstream truth, so it runs against the staging/landing layer inside Snowflake
— still single-engine, still Lane A.

**Check 11 is the only surviving use of hashing in Lane A**, because there is no second table to
compare against; the hash of closed versions is stored in `DPHM_STATE.SCD_VERSION_HASHES` on
first sight and compared thereafter.

**None of these work until audit columns are excluded.** With `_LOADED_AT` in the tracked set,
check 6 fires on every row of every run and the suite tells us nothing.

### 3.1 Check 1 — exactly one current row per key

```sql
-- templates/scd/one_current_row_per_key.sql.j2
select
    object_construct({% for k in business_key %}'{{ k }}', {{ k }}{{ "," if not loop.last }}{% endfor %}) as failing_key,
    'SCD_MULTIPLE_CURRENT' as violation_code,
    object_construct('current_row_count', count(*)) as detail
from {{ table }}
where {{ scope_predicate }}
  and {{ is_current }} = true
group by {{ business_key | join(", ") }}
having count(*) <> 1
limit {{ sample_limit }}
```

### 3.2 Check 2 — no overlapping ranges

```sql
-- templates/scd/no_overlapping_ranges.sql.j2
with versions as (
    select
        {{ business_key | join(", ") }},
        {{ valid_from }} as valid_from,
        coalesce({{ valid_to }}, '{{ open_end_sentinel }}'::timestamp_ntz) as valid_to,
        lead({{ valid_from }}) over (
            partition by {{ business_key | join(", ") }}
            order by {{ valid_from }}
        ) as next_valid_from
    from {{ table }}
    where {{ scope_predicate }}
)
select
    object_construct({% for k in business_key %}'{{ k }}', {{ k }}{{ "," if not loop.last }}{% endfor %}) as failing_key,
    'SCD_OVERLAPPING_RANGE' as violation_code,
    object_construct('valid_from', valid_from, 'valid_to', valid_to,
                     'next_valid_from', next_valid_from) as detail
from versions
where next_valid_from is not null
  and next_valid_from < valid_to
limit {{ sample_limit }}
```

Check 3 (gaps) is the same CTE with `next_valid_from > valid_to`.

### 3.3 Check 6 — no spurious versions

```sql
-- templates/scd/no_spurious_versions.sql.j2
-- A new version must be justified by a change in a TRACKED column.
-- {{ tracked_columns }} excludes audit/ETL columns by construction (see column_rules.yaml).
with versions as (
    select
        {{ business_key | join(", ") }},
        {{ valid_from }} as valid_from,
        {% for c in tracked_columns %}{{ c }},
        {% endfor %}
        {% for c in tracked_columns %}
        lag({{ c }}) over (partition by {{ business_key | join(", ") }} order by {{ valid_from }}) as prev_{{ c }}{{ "," if not loop.last }}
        {% endfor %}
    from {{ table }}
    where {{ scope_predicate }}
)
select
    object_construct({% for k in business_key %}'{{ k }}', {{ k }}{{ "," if not loop.last }}{% endfor %}) as failing_key,
    'SCD_SPURIOUS_VERSION' as violation_code,
    object_construct('valid_from', valid_from) as detail
from versions
where prev_{{ tracked_columns[0] }} is not null            -- not the first version
  and not ({% for c in tracked_columns %}{{ c }} is distinct from prev_{{ c }}{{ " or " if not loop.last }}{% endfor %})
limit {{ sample_limit }}
```

### 3.4 Check 7 — a tracked change did create a new version

The check that catches an SCD2 dimension silently degrading into an overwrite.

```sql
-- templates/scd/tracked_change_created_version.sql.j2
-- Compare the CURRENT dimension row against the latest staged source row for the same key.
-- If a tracked column differs but no new version was opened in the required window,
-- the pipeline overwrote history instead of versioning it.
with current_dim as (
    select {{ business_key | join(", ") }},
           {% for c in tracked_columns %}{{ c }},{% endfor %}
           {{ valid_from }} as valid_from
    from {{ table }}
    where {{ scope_predicate }}
      and {{ is_current }} = true
),
latest_stage as (
    select {{ business_key | join(", ") }},
           {% for c in tracked_columns %}{{ c }},{% endfor %}
           {{ stage_loaded_at }} as staged_at,
           row_number() over (
               partition by {{ business_key | join(", ") }}
               order by {{ stage_loaded_at }} desc
           ) as rn
    from {{ stage_table }}
    where {{ stage_scope_predicate }}
)
select
    object_construct({% for k in business_key %}'{{ k }}', d.{{ k }}{{ "," if not loop.last }}{% endfor %}) as failing_key,
    'SCD_OVERWRITE_INSTEAD_OF_VERSION' as violation_code,
    object_construct(
        'changed_columns', array_construct_compact(
            {% for c in tracked_columns %}
            iff(d.{{ c }} is distinct from s.{{ c }}, '{{ c }}', null){{ "," if not loop.last }}
            {% endfor %}
        ),
        'dim_valid_from', d.valid_from,
        'stage_loaded_at', s.staged_at
    ) as detail
from current_dim d
join latest_stage s
  on {% for k in business_key %}d.{{ k }} = s.{{ k }}{{ " and " if not loop.last }}{% endfor %}
 and s.rn = 1
where ({% for c in tracked_columns %}d.{{ c }} is distinct from s.{{ c }}{{ " or " if not loop.last }}{% endfor %})
  -- a genuine change should have opened a version AFTER the source row landed
  and d.valid_from <= s.staged_at
limit {{ sample_limit }}
```

### 3.5 Check 11 — closed versions immutable

```sql
-- templates/scd/closed_versions_immutable.sql.j2
-- Content hash of every CLOSED version, over compared (non-audit) columns only.
-- The engine compares this to DPHM_STATE.SCD_VERSION_HASHES from prior runs.
select
    object_construct('{{ surrogate_key }}', {{ surrogate_key }}) as failing_key,
    'SCD_CLOSED_VERSION_MUTATED' as violation_code,
    object_construct('content_hash', content_hash, 'valid_to', {{ valid_to }}) as detail
from (
    select
        {{ surrogate_key }},
        {{ valid_to }},
        sha2(concat_ws('||',
            {% for c in compared_columns %}
            coalesce(to_varchar({{ c }}), '<NULL>'){{ "," if not loop.last }}
            {% endfor %}
        ), 256) as content_hash
    from {{ table }}
    where {{ valid_to }} < current_timestamp()
      and {{ scope_predicate }}
) t
-- joined against prior hashes by the engine; template emits the current state
limit {{ sample_limit }}
```

## 4. Non-SCD Lane A templates

| Template | Assertion | Notes |
|---|---|---|
| `keys/grain_unique` | `group by <grain> having count(*) > 1` | Runs on every table with a confirmed grain |
| `keys/key_not_null` | No NULL in any key column | Cheap, catches load truncation |
| `ref/fk_resolves` | Anti-join to the parent; returns orphan keys | Time-aware variant for SCD2 parents |
| `domain/value_in_range` | Numeric bounds, NULL policy explicit | |
| `domain/value_in_enum` | Declared allowed values | |
| `cast/cast_failure_rate` | `try_cast` NULL rate between raw and typed layers, above a declared bound | Snowflake `TRY_CAST` |
| `conservation/row_conservation` | `rows(in) = rows(out) + named_losses` | Losses must be *named*, e.g. `rejected`, `filtered_test` |
| `fanout/join_fanout_guard` | Output rows per input key ≤ a declared bound | Catches an accidental many-to-many |
| `business/custom` | A reviewed hand-written predicate | Full code review, not template params |

### 4.1 Layer templates (`templates/layer/`)

The medallion hop bundles in [`07B`](07B_LAYER_PARITY_MEDALLION.md) are built from these. They
follow the same uniform contract — return violating rows, zero rows is a pass.

| Template | Hop | Asserts |
|---|---|---|
| `layer/row_conservation` | L1, L2, L3 | input = kept + named losses + computed dedup collapse |
| `layer/key_set_equal` | L1 | Key sets match in both directions |
| `layer/duplicate_profile_preserved` | L1 | Bronze preserved the source's duplicate multiplicity — protects L2 |
| `layer/batch_immutable` | L1 | A closed bronze batch is never rewritten |
| `layer/dedup_key_unique` | L2 | No duplicate survived |
| `layer/dedup_pick_rule_fidelity` | L2 | The *right* duplicate survived |
| `layer/dedup_pick_rule_total` | L2 | The pick rule resolves every tie |
| `layer/no_invented_keys` · `layer/no_lost_keys` | L2 | Silver ⊆ bronze, and nothing vanishes unnamed |
| `layer/reject_reasons_closed` | L2 | Every reject carries a reason from a closed set |
| `layer/dim_key_coverage` · `layer/dim_matches_silver` | L3 | Gold dimensions agree with silver |
| `layer/measure_conservation` | L3 | Additive measures survive the hop within tolerance |
| `layer/fk_resolves_as_of` | L3 | FKs resolve to the dimension version valid at event time |
| `layer/group_cardinality` · `layer/aggregate_of` | L3 | Marts recompute correctly from silver |

## 5. Row scoping — never read mid-load

Risk R2. The `scope_predicate` is resolved per run and **logged with the result**, so a
false failure can always be traced back to what was read.

| `batch.strategy` | Predicate |
|---|---|
| `control_table` | `BATCH_ID IN (select BATCH_ID from ETL_BATCH_LOG where STATUS='SUCCESS' and COMPLETED_AT > ...)` |
| `audit_column` | `_LOADED_AT < <last completed load boundary>` |
| `hook_only` | Batch id passed in by the post-load hook: `BATCH_ID = :batch_id` |

If no strategy can be resolved, the run does **not** fall back to wall-clock silently. It marks
affected checks `INCONCLUSIVE` and reports the reason. Guessing produces false failures, and
false failures are how a monitoring tool loses its audience.

## 6. Tolerance

```yaml
tolerance:
  max_failing_rows: 0          # default; a violation is a violation
  max_failing_ratio: null      # e.g. 0.0001 for known-dirty legacy domains
  float_abs: 1e-9
  float_rel: 1e-9
  min_rows_to_evaluate: 1      # below this, result is INCONCLUSIVE, not PASS
```

`min_rows_to_evaluate` exists because a check over an empty table trivially passes. That is
exactly the "check that can never fail" the design doc warns about, and it is caught by gate 3.

## 7. Check identity and versioning

```python
check_id     = f"{template}.{table}.{stable_param_digest}"   # stable across cosmetic edits
sql_sha256   = sha256(rendered_sql)                          # changes when behaviour changes
```

`check_id` is the dedup and history key. `sql_sha256` changing on an active check invalidates its
validation gates and its sign-offs — the check must be revalidated before it can fire again.
