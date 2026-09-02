# 07 — Lane B: RDS ↔ Snowflake Parity

## 1. The problem this doc solves

The design doc assumed both compared sources sat in one engine, which made Lane B a single SQL
statement. This deployment is a **migration**: source is AWS RDS (PostgreSQL), target is
Snowflake. The doc's own assumption register says cross-engine work then re-enters scope.

We do **not** build a general cross-engine diff engine. We build the narrowest thing that answers
"do these two agree, and if not, which rows and which column" within a hard budget.

**Guiding rule:** move as little data as possible, and never move it in the wrong direction.
All scratch work happens in Snowflake (`DPHM_SCRATCH`). RDS is read-only and untouched.

## 2. Three tiers

```
Tier 0  aggregates          both engines, independently      ~2 queries      cents
   │    counts, sums, min/max, per-bucket fingerprints
   │    compare small result sets in Python
   ├── all buckets agree ─────────────────────────────────▶  PASS. Stop.
   ▼
Tier 1  bucket narrowing    only disagreeing buckets         ~2N queries     cents
   │    sub-bucket by key hash, recompute fingerprints
   ├── narrows to a small set of suspect key ranges
   ▼
Tier 2  landed row diff     suspect ranges only              1 unload + 1 join   dollars
        pull those RDS rows → DPHM_SCRATCH → full outer join in Snowflake
        → exact differing rows and the causing column
```

Most runs stop at tier 0. A run that reaches tier 2 does so over a small fraction of the table,
which is what keeps the budget honest.

## 3. Canonicalization — the part that must be right

A cross-engine comparison is only meaningful over a **canonical projection**: the same logical
value rendered identically on both engines. This is generated once per table from
`column_rules.yaml` + `transforms.yaml`, and the resulting projection is recorded with the run.

| Type | PostgreSQL expression | Snowflake expression |
|---|---|---|
| Integer | `col::text` | `TO_VARCHAR(col)` |
| Numeric / decimal | `round(col::numeric, {s})::text` | `TO_VARCHAR(ROUND(col::NUMBER(38,{s}), {s}))` |
| Float | *excluded from the fingerprint*; compared in tier 2 with tolerance | same |
| Text | `coalesce(col, '<NULL>')` (+ `trim`/`lower` if declared) | `COALESCE(col, '<NULL>')` (+ `TRIM`/`LOWER`) |
| Boolean | `case when col then 'T' when not col then 'F' else '<NULL>' end` | `IFF(col, 'T', IFF(col IS NULL, '<NULL>', 'F'))` |
| Timestamp | `to_char(col at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS')` | `TO_CHAR(CONVERT_TIMEZONE('UTC', col), 'YYYY-MM-DD HH24:MI:SS')` |
| Date | `to_char(col, 'YYYY-MM-DD')` | `TO_CHAR(col, 'YYYY-MM-DD')` |
| UUID | `lower(col::text)` | `LOWER(TO_VARCHAR(col))` |
| JSON | `jsonb_canonical(col)` (sorted keys, no whitespace) | `TO_JSON(col)` after key-sorted normalization |
| PII (`compare: hash`) | `encode(sha256(canon::bytea),'hex')` | `SHA2(canon, 256)` |

Rules:

- **Floats never enter a fingerprint.** Cross-engine float rendering is not reliably identical;
  they are compared only in tier 2, with the declared absolute and relative tolerance.
- NULL is rendered as the literal `<NULL>` sentinel on both sides, so `IS DISTINCT FROM`
  semantics hold through a string concatenation.
- Column ordering in the concatenation is the sorted `columns_compared` list — deterministic,
  and recorded on the result.
- Audit/ETL columns are excluded from both sides, and the exclusion list is reported. The RDS
  side has its own audit columns (`updated_at`, `created_at`) that must not leak into the
  comparison unless declared business-meaningful.

### Row fingerprint

```
row_fp = sha256( concat_ws('||', canon(c1), canon(c2), ..., canon(cn)) )
```

### Bucket fingerprint (order-independent, so no sorting is required on either side)

```sql
-- both engines: an XOR/sum aggregation over row fingerprints, per bucket
bucket   = mod(abs(hashtext_or_sha_prefix(key_canon)), {{ bucket_count }})
bucket_fp = bit_xor( ('x' || substr(row_fp, 1, 16))::bit(64) )   -- postgres
BUCKET_FP = BITXOR_AGG( TO_NUMBER(SUBSTR(row_fp, 1, 15), 'XXXXXXXXXXXXXXX') )  -- snowflake
```

The bucket function must be **identical arithmetic on both engines**. v1 uses
`sha256(key_canon)` → take the first 8 hex chars → integer → `mod bucket_count`, computed with
the same digits on both sides. `bucket_count` defaults to 1024 and is recorded per run — changing
it invalidates cached fingerprints.

An XOR aggregate is order-independent and collision-tolerant enough at this granularity; a
collision hides a difference only if two rows in the same bucket differ in exactly compensating
ways, and tier 0 also compares per-bucket `count(*)` and `sum(numeric)` independently, which
catches that.

## 4. Tier 0 — aggregates

Issued once per engine:

```sql
-- Snowflake side (structurally identical on Postgres)
select
    mod(abs(to_number(substr(sha2(key_canon, 256), 1, 8), 'XXXXXXXX')), {{ bucket_count }}) as bucket,
    count(*)                                                          as row_count,
    bitxor_agg(to_number(substr(row_fp, 1, 15), 'XXXXXXXXXXXXXXX'))   as bucket_fp,
    {% for c in numeric_columns %}
    sum(round({{ c }}::number(38,{{ scale }}), {{ scale }}))           as sum_{{ c }},
    {% endfor %}
    min({{ scope_column }}) as min_scope, max({{ scope_column }}) as max_scope
from (
    select {{ key_canon_expr }} as key_canon, {{ row_fp_expr }} as row_fp, {{ numeric_columns | join(", ") }}, {{ scope_column }}
    from {{ table }}
    where {{ scope_predicate }} {{ transform_row_filter }}
)
group by 1
```

Result: ≤ 1024 rows from each engine. Compared in Python. Outcomes:

| Comparison | Meaning |
|---|---|
| All buckets match on count and fp | **PASS** — stop, cost is two queries |
| A bucket's count differs | Missing or extra rows in that key range |
| Count matches, fp differs | Same rows, different values |
| Bucket present on one side only | Whole key range missing |

## 5. Tier 1 — narrowing

For each disagreeing bucket, re-bucket that bucket's rows with a second-level modulus
(`sub_bucket = mod(sha_prefix(key_canon) / bucket_count, 256)`) and recompute. Two or three
rounds reduce a table of hundreds of millions to a few thousand suspect keys, at the cost of a
handful of narrow aggregate queries.

Stop conditions:
- suspect key count ≤ `tier2_max_keys` (default 50,000) → go to tier 2, or
- budget exhausted → return `PARTIAL` with the suspect key ranges named, or
- max depth (3) reached → go to tier 2 with whatever narrowed.

## 6. Tier 2 — landed row diff

1. Read the suspect rows from RDS with a server-side cursor, keyed by the narrowed ranges,
   projecting only the canonical compared columns. Statement timeout and row cap enforced.
2. Write them to `DPHM_SCRATCH.PARITY_<run_id>_<table>` via a Snowflake `PUT` of Parquet +
   `COPY INTO` (chunked; never a row-by-row `INSERT`).
3. Run the design doc's original single statement — now legitimately same-engine:

```sql
-- templates/parity/full_outer_diff.sql.j2
select
    coalesce(a.{{ key }}, b.{{ key }}) as {{ key }},
    case
        when a.{{ key }} is null then 'MISSING_IN_SOURCE'
        when b.{{ key }} is null then 'MISSING_IN_TARGET'
        else 'VALUE_MISMATCH'
    end as violation_code,
    object_construct_keep_null(
        {% for c in compared_columns %}
        '{{ c }}', iff(a.{{ c }} is distinct from b.{{ c }},
                       object_construct('source', a.{{ c }}, 'target', b.{{ c }}),
                       null){{ "," if not loop.last }}
        {% endfor %}
    ) as differing_columns
from {{ scratch_table }} a                     -- landed RDS rows
full outer join {{ target_table }} b using ({{ key }})
where a.{{ key }} is null
   or b.{{ key }} is null
   {% for c in compared_columns %}
   or {{ tolerance_aware_compare(c) }}
   {% endfor %}
-- audit/ETL columns deliberately excluded: {{ columns_excluded | join(", ") }}
limit {{ sample_limit }}
```

where `tolerance_aware_compare` is `a.c IS DISTINCT FROM b.c` for exact types and
`abs(a.c - b.c) > greatest({{ float_abs }}, {{ float_rel }} * abs(b.c))` for floats.

4. Drop the scratch table in a `finally` block. Scratch tables also carry a 24-hour
   `DATA_RETENTION_TIME_IN_DAYS=0` and a nightly sweeper drops orphans.

## 7. Budget and abort

```yaml
budgets:
  lane_b_usd_per_run: 5.00
  lane_b_rds_seconds_per_run: 600
```

- Cost is estimated before each tier and accumulated after each query (Snowflake credits from
  `QUERY_HISTORY`, RDS by elapsed time × a configured rate).
- Crossing the budget aborts **at a tier boundary**, never mid-tier, and returns `PARTIAL` with
  everything established so far plus the named suspect ranges.
- The Parity screen pre-fills the budget from config and lets the user **lower** it for a run;
  there is no way to raise it past the configured cap, and no way to disable it.

## 8. Applying `transforms.yaml`

Declared divergences are applied to the projection *before* fingerprinting:

| kind | Effect |
|---|---|
| `column_dropped` | Removed from `compared_columns`; listed in `columns_excluded` with the transform id |
| `column_renamed` | Mapped in the projection |
| `row_filter` | Added to the source-side `scope_predicate` |
| `type_change` | `compare_as` type used for canonicalization on both sides |
| `value_mapping` | A `CASE` applied to the source side |
| `derived_column` | Target-only; excluded, and reported as uncovered |

Every applied transform appears in the run output: *"3 declared transforms applied; 2 columns
excluded by transform; 1 transform expired (commit 3ab77e1 no longer matches
sql/transforms/dim_customer.sql) — treating as undeclared."*

An **expired** transform is not silently honoured and not silently dropped. It is honoured for
the run and reported as expired, with the PR that changed the file linked, so a human decides.

## 9. Lane B invocation paths

Both hit the same endpoint; there is no second implementation.

```
# ad hoc — the Parity screen (12 §2.8); progress streams live via SSE so an expensive
# run can be watched and aborted
POST /api/v1/projects/orders/parity
     {"tables": ["public.orders"], "budget_usd": 5.00}

# PR gate — the GitHub App webhook, non-blocking in v1
POST /api/v1/hooks/github        (pull_request opened/synchronized)
     → derives affected tables from the diff, runs with the PR budget,
       posts a comment + a neutral check status
```

The PR path derives which tables to compare from the files changed in the PR, via the lineage
extracted by `repo/sql_parse.py`. It posts a comment, sets a neutral check status, and **asks the
author to declare intent** if it finds a difference — capturing the reason while they still
remember it.
