# 07B — Layer-by-Layer Parity (Source → Bronze → Silver → Gold)

Part of the v1 definition of done. The SCD suite proves *one table* is internally correct; layer
parity proves *the pipeline between two layers* did what it claimed. Together they cover the two
ways a migration goes wrong.

**Everything in this document is deterministic SQL.** The AI agent's entire job here is to infer
the **layer contract** — which table feeds which, on what key, with which pick rule and which
declared losses — and a human confirms the two decisions that matter. The checks themselves are
templated, gated, and mutation-tested exactly like every other check.

## 1. The four layers and three hops

```
   AWS RDS              BRONZE                SILVER                 GOLD
   (source)             raw landing           cleaned + deduped      dims / facts / marts
      │                     │                      │                      │
      └──── L1 ─────────────┘                      │                      │
            Lane B, cross-engine                   │                      │
            relation: identical / conserved        │                      │
                                                   │                      │
            └──────── L2 ──────────────────────────┘                      │
                      Lane A, same-engine                                 │
                      relation: DEDUP_OF  ← the initial condition          │
                                                                          │
                      └──────── L3 ─────────────────────────────────────┘
                                Lane A, same-engine
                                relation: aggregate_of / conserved / SCD suite
```

| Hop | Lane | Engine | Primary relation | Cost | Trigger |
|---|---|---|---|---|---|
| **L1** source → bronze | **B** | RDS ↔ Snowflake | `identical`, `conserved` | The expensive one, tiered + budgeted (`07`) | On demand / PR gate / post-load hook at cutover |
| **L2** bronze → silver | **A** | Snowflake only | **`dedup_of`**, `conserved`, `filtered_from` | Cheap SQL | Scheduled / post-load hook |
| **L3** silver → gold | **A** | Snowflake only | `aggregate_of`, `conserved`, SCD suite, FK | Cheap SQL | Scheduled / post-load hook |

Two consequences worth being explicit about:

- **L1 is the only cross-engine hop.** Everything downstream is one engine, one SQL statement,
  and names the offending column for free. This is why the RDS→Snowflake reality (`02` D2) costs
  us one hop's worth of complexity rather than the whole product's.
- **L2 and L3 are where most real defects live**, and they are the cheap ones. The dimension that
  silently stops versioning, the dedup that picks an arbitrary row, the mart that double-counts
  after a join fanout — none of these are visible at L1, because L1 is green in all three cases.

## 2. Layer contract configuration

A new file per project, `layers.yaml`. This is what the authoring agent proposes and a human
confirms.

```yaml
layers:
  bronze: {database: ANALYTICS, schema: BRONZE}
  silver: {database: ANALYTICS, schema: SILVER}
  gold:   {database: ANALYTICS, schema: GOLD}

hops:
  # ── L1: source → bronze ────────────────────────────────────────────────
  - id: l1_customers
    hop: source_to_bronze
    lane: B
    source: public.customers                    # AWS RDS
    target: ANALYTICS.BRONZE.CUSTOMERS
    relation: identical                         # bronze is a faithful landing
    key: [CUSTOMER_ID]
    expect_duplicates: true                     # bronze keeps source duplicates as-is
    declared_losses: []                         # nothing may be dropped at L1
    transforms_ref: [drop_legacy_notes]         # from transforms.yaml

  # ── L2: bronze → silver ────────────────────────────────────────────────
  - id: l2_customers
    hop: bronze_to_silver
    lane: A
    source: ANALYTICS.BRONZE.CUSTOMERS
    target: ANALYTICS.SILVER.CUSTOMERS
    relation: dedup_of                          # THE initial condition for this hop
    dedup:
      key: [CUSTOMER_ID]                        # what "duplicate" means
      key_confirmed_by: "priya@acme.com"        # ← human confirmation required
      key_confirmed_at: "2026-09-04T10:12:00Z"
      pick_rule:                                # which duplicate survives — deterministic
        order_by:
          - {column: SOURCE_UPDATED_AT, direction: desc, nulls: last}
          - {column: SOURCE_ROW_ID,     direction: desc, nulls: last}   # tiebreaker
        must_be_total: true                     # the ordering must resolve every tie
      compare_columns: business                 # surviving row must match the picked bronze row
    declared_losses:
      - {name: dedup_collapse, kind: dedup}     # bronze rows removed by dedup — computed, not asserted
      - {name: rejected, kind: reject_table,
         table: ANALYTICS.SILVER.CUSTOMERS_REJECT, reason_column: REJECT_REASON,
         allowed_reasons: [NULL_BUSINESS_KEY, CAST_FAILED, FAILED_DOMAIN_RULE]}
      - {name: filtered_test_accounts, kind: filter, predicate: "IS_TEST = true"}
    strict_conservation: true                   # input = kept + rejected + filtered + dedup_collapse

  # ── L3: silver → gold ──────────────────────────────────────────────────
  - id: l3_dim_customer
    hop: silver_to_gold
    lane: A
    source: ANALYTICS.SILVER.CUSTOMERS
    target: ANALYTICS.GOLD.DIM_CUSTOMER
    relation: scd2_of                           # → the full 11-check SCD suite, plus hop checks
    key: [CUSTOMER_ID]

  - id: l3_fct_order
    hop: silver_to_gold
    lane: A
    source: ANALYTICS.SILVER.ORDERS
    target: ANALYTICS.GOLD.FCT_ORDER
    relation: conserved
    key: [ORDER_ID]
    fanout_max: 1                               # a fact row per silver row; >1 means a join multiplied
    measures:
      - {column: AMOUNT, source_column: AMOUNT, additive: true, tolerance: {abs: 0.01}}

  - id: l3_mart_daily_revenue
    hop: silver_to_gold
    lane: A
    source: ANALYTICS.SILVER.ORDERS
    target: ANALYTICS.GOLD.MART_DAILY_REVENUE
    relation: aggregate_of
    group_by: [ORDER_DATE, COUNTRY]
    group_by_confirmed_by: "priya@acme.com"     # ← grain confirmation, same class as SCD grain
    measures:
      - {column: REVENUE,     expr: "sum(AMOUNT)",        additive: true}
      - {column: ORDER_COUNT, expr: "count(*)",           additive: true}
      - {column: AVG_BASKET,  expr: "avg(AMOUNT)",        additive: false}   # excluded from summation
      - {column: UNIQUE_BUYERS, expr: "count(distinct CUSTOMER_ID)", additive: false}
```

Two fields carry a human signature, for the same reason SCD grain does: **`dedup.key` and
`group_by` are the decisions that, if wrong, produce a check that passes while comparing nothing.**
A dedup check on the wrong key finds no duplicates and reports green forever.

---

## 3. L1 — source → bronze

Bronze must be a **faithful landing**: same rows, same values, no cleaning, no dedup. Anything
transformed at L1 is a transformation hiding in the ingest layer, and it should be declared or
moved to L2.

| # | Check | Template | Catches |
|---|---|---|---|
| L1.1 | Row conservation per batch: `count(source) = count(bronze)` | `layer/row_conservation` | Truncated or partial load |
| L1.2 | Key set equality (both directions) | `layer/key_set_equal` | Missing or extra rows |
| L1.3 | Value parity on business columns | `parity/full_outer_diff` (`07` tiered) | Corruption, precision loss, encoding |
| L1.4 | Type fidelity / cast health | `cast/cast_failure_rate` | A `numeric` landing as text and silently failing later |
| L1.5 | No dedup happened at L1 | `layer/duplicate_profile_preserved` | Ingest quietly deduping, so L2 has nothing to do and L2's checks are meaningless |
| L1.6 | Bronze is append-only for a closed batch | `layer/batch_immutable` | A backfill rewriting landed history |

L1.5 is the non-obvious one and it exists to protect L2. It compares the **duplicate profile** —
`count(*)` grouped by the dedup key, then histogrammed — rather than the rows:

```sql
-- templates/layer/duplicate_profile_preserved.sql.j2
-- Bronze must preserve the source's duplicate multiplicity. Compared as a small histogram,
-- so this costs one aggregate per side, not a row-level diff.
with bronze_profile as (
    select dup_count, count(*) as key_count
    from (
        select {{ dedup_key | join(", ") }}, count(*) as dup_count
        from {{ bronze_table }}
        where {{ scope_predicate }}
        group by {{ dedup_key | join(", ") }}
    ) group by dup_count
)
select
    object_construct('dup_count', coalesce(b.dup_count, s.dup_count)) as failing_key,
    'L1_DUPLICATE_PROFILE_CHANGED' as violation_code,
    object_construct('bronze_keys', b.key_count, 'source_keys', s.key_count) as detail
from bronze_profile b
full outer join {{ source_profile_scratch }} s using (dup_count)
where coalesce(b.key_count, -1) <> coalesce(s.key_count, -1)
limit {{ sample_limit }}
```

The source side of L1.5 is one `GROUP BY` on RDS returning a handful of rows — cheap enough to run
on every parity invocation, unlike L1.3.

---

## 4. L2 — bronze → silver: **the deduplication contract**

This is the hop with an explicit initial condition, so it gets the most checks. The contract is:

> Silver is bronze, deduplicated on `dedup.key` by a **total, deterministic** pick rule, minus
> **named** losses.

Everything below follows from that sentence. Nine checks per L2 hop, from ten templates —
L2.9 is two (`domain/not_null` and `domain/value_in_range`).

| # | Check | Asserts | Catches |
|---|---|---|---|
| **L2.1** | Silver is unique on `dedup.key` | The dedup actually happened | Duplicates survived — the primary failure |
| **L2.2** | No invented rows: every silver key exists in bronze | Silver ⊆ bronze | A join fanout or a bad backfill inventing keys |
| **L2.3** | No lost keys: every bronze key appears in silver or in a **named** loss | Nothing vanishes silently | Over-aggressive filtering, a reject bucket nobody reads |
| **L2.4** | Conservation: `bronze = silver + rejects + filtered + dedup_collapse` | The arithmetic closes | Unexplained loss — the dangerous kind |
| **L2.5** | **Pick-rule fidelity**: the surviving row *is* the row the declared rule selects | The right duplicate won | **Dedup picking an arbitrary row — green on every other check** |
| **L2.6** | **Pick-rule totality**: no unresolved ties | The rule is deterministic | A non-deterministic pick that changes on every rerun |
| **L2.7** | Value fidelity (`layer/dedup_value_fidelity`): the surviving row's business columns match its bronze original | Dedup did not also transform | A "cleanup" smuggled into the dedup step |
| **L2.8** | Every rejected row carries a reason from the closed set | Rejects are explained | A silent catch-all reject bucket |
| **L2.9** | Domain and NOT NULL on silver business columns | Cleaning did what it claimed | Nulls introduced by a failed cast |

**L2.5 and L2.6 are the pair that matter, and they are the reason this hop needs a contract at
all.** A dedup that keeps *a* row per key passes L2.1 through L2.4 and L2.7 perfectly while
returning different data on every rerun. That is a silent, non-reproducible pipeline, and no
row-count check can see it.

### 4.1 L2.1 — dedup actually happened

```sql
-- templates/layer/dedup_key_unique.sql.j2
select
    object_construct({% for k in dedup_key %}'{{ k }}', {{ k }}{{ "," if not loop.last }}{% endfor %}) as failing_key,
    'L2_DUPLICATE_SURVIVED_DEDUP' as violation_code,
    object_construct('surviving_rows', count(*)) as detail
from {{ silver_table }}
where {{ scope_predicate }}
group by {{ dedup_key | join(", ") }}
having count(*) > 1
limit {{ sample_limit }}
```

### 4.2 L2.5 — pick-rule fidelity

The whole hop's correctness in one statement. Recompute the pick from bronze with the declared
ordering, then assert silver holds exactly that row.

```sql
-- templates/layer/dedup_pick_rule_fidelity.sql.j2
-- Recompute the declared pick from bronze and compare to what silver actually kept.
-- {{ compare_columns }} excludes audit/ETL columns by construction.
with expected as (
    select
        {{ dedup_key | join(", ") }},
        {% for c in compare_columns %}{{ c }},{% endfor %}
        row_number() over (
            partition by {{ dedup_key | join(", ") }}
            order by {{ pick_order_by }}          -- e.g. SOURCE_UPDATED_AT desc nulls last, SOURCE_ROW_ID desc
        ) as rn
    from {{ bronze_table }}
    where {{ bronze_scope_predicate }}
      {{ loss_filter_predicate }}                 -- exclude declared filters/rejects
),
picked as (
    select * from expected where rn = 1
)
select
    object_construct({% for k in dedup_key %}'{{ k }}', coalesce(s.{{ k }}, p.{{ k }}){{ "," if not loop.last }}{% endfor %}) as failing_key,
    case
        when p.{{ dedup_key[0] }} is null then 'L2_SILVER_ROW_NOT_IN_BRONZE'
        when s.{{ dedup_key[0] }} is null then 'L2_PICKED_ROW_MISSING_FROM_SILVER'
        else 'L2_WRONG_DUPLICATE_KEPT'
    end as violation_code,
    object_construct(
        'differing_columns', array_construct_compact(
            {% for c in compare_columns %}
            iff(s.{{ c }} is distinct from p.{{ c }}, '{{ c }}', null){{ "," if not loop.last }}
            {% endfor %}
        )
    ) as detail
from {{ silver_table }} s
full outer join picked p
  on {% for k in dedup_key %}s.{{ k }} = p.{{ k }}{{ " and " if not loop.last }}{% endfor %}
where s.{{ dedup_key[0] }} is null
   or p.{{ dedup_key[0] }} is null
   {% for c in compare_columns %}
   or s.{{ c }} is distinct from p.{{ c }}
   {% endfor %}
limit {{ sample_limit }}
```

**Disambiguating the third branch.** "Silver's row differs from the picked row" has two distinct
causes, and they are different tickets: the dedup kept a *different duplicate*, or the dedup
*altered* the row it kept. Split them on whether the silver row matches **any** bronze row for
that key:

```sql
    case
        when p.{{ dedup_key[0] }} is null then 'L2_SILVER_ROW_NOT_IN_BRONZE'
        when s.{{ dedup_key[0] }} is null then 'L2_PICKED_ROW_MISSING_FROM_SILVER'
        when exists (                                   -- silver's values ARE some bronze row's,
            select 1 from {{ bronze_table }} b          --   just not the one the rule picks
            where {% for k in dedup_key %}b.{{ k }} = s.{{ k }} and {% endfor %}
                  {% for c in compare_columns %}b.{{ c }} is not distinct from s.{{ c }}
                  {{ "and " if not loop.last }}{% endfor %}
              and {{ bronze_scope_predicate }}
        ) then 'L2_WRONG_DUPLICATE_KEPT'
        else 'L2_SURVIVING_ROW_VALUE_MISMATCH'          -- matches no bronze row: dedup transformed it
    end as violation_code,
```

Without this split, the pick-rule injector and the value-fidelity injector produce the same code,
and gate 3's anti-cheat #2 (`10`) cannot tell "the wrong duplicate won" from "the dedup rewrote
the row" — two failures with different owners and different fixes.

This single check subsumes L2.2, L2.5 and L2.7 when it runs. The narrower checks still exist
because they are far cheaper and they name the failure precisely — L2.1 tells you "duplicates
survived", which is a different conversation from "the wrong duplicate survived."

### 4.3 L2.6 — pick-rule totality (no unresolved ties)

```sql
-- templates/layer/dedup_pick_rule_total.sql.j2
-- If two bronze rows tie on the FULL ordering, the pick is arbitrary and the pipeline
-- is non-reproducible. This must be zero, or the pick rule needs another tiebreaker.
select
    object_construct({% for k in dedup_key %}'{{ k }}', {{ k }}{{ "," if not loop.last }}{% endfor %}) as failing_key,
    'L2_PICK_RULE_NOT_TOTAL' as violation_code,
    object_construct('tied_rows', tied, 'order_by', '{{ pick_order_by }}') as detail
from (
    select
        {{ dedup_key | join(", ") }},
        count(*) as tied
    from {{ bronze_table }}
    where {{ bronze_scope_predicate }} {{ loss_filter_predicate }}
    group by {{ dedup_key | join(", ") }}, {{ pick_order_columns | join(", ") }}
    having count(*) > 1
)
limit {{ sample_limit }}
```

When `must_be_total: true` and this returns rows, the finding is reported as
**`CHECK_DEFECT`-adjacent**: the *contract* is under-specified, not necessarily the data. The
remedy is a tiebreaker column in `layers.yaml`, and the incident is routed to whoever owns the
transform with that suggestion attached.

### 4.4 L2.4 — conservation with named losses

```sql
-- templates/layer/row_conservation.sql.j2
-- Every row that entered bronze must be accounted for. Losses must be NAMED.
with counts as (
    select
        (select count(*) from {{ bronze_table }} where {{ bronze_scope_predicate }})                as input_rows,
        (select count(*) from {{ silver_table }} where {{ silver_scope_predicate }})                as kept_rows,
        {% for loss in declared_losses %}
        ({{ loss.count_expr }})                                                                     as loss_{{ loss.name }},
        {% endfor %}
        (select count(*) - count(distinct {{ dedup_key | join(", ") }}) from {{ bronze_table }}
         where {{ bronze_scope_predicate }} {{ loss_filter_predicate }})                            as loss_dedup_collapse
)
select
    object_construct('batch', '{{ batch_id }}') as failing_key,
    'L2_UNEXPLAINED_ROW_LOSS' as violation_code,
    object_construct(
        'input', input_rows, 'kept', kept_rows,
        {% for loss in declared_losses %}'{{ loss.name }}', loss_{{ loss.name }},{% endfor %}
        'dedup_collapse', loss_dedup_collapse,
        'unexplained', input_rows - kept_rows - loss_dedup_collapse
                       {% for loss in declared_losses %} - loss_{{ loss.name }}{% endfor %}
    ) as detail
from counts
where input_rows - kept_rows - loss_dedup_collapse
      {% for loss in declared_losses %} - loss_{{ loss.name }}{% endfor %} <> 0
```

`dedup_collapse` is **computed, not asserted** — it is whatever the declared dedup key implies.
That keeps the equation honest: if the dedup key is wrong, `dedup_collapse` is wrong, the equation
does not close, and the check fires rather than silently absorbing the difference into a fudge
term. This is deliberate. A conservation check with an unconstrained slack term is a check that
can never fail.

---

## 5. L3 — silver → gold

Gold is where silver becomes dimensions, facts, and marts. The relation depends on what is being
built.

### 5.1 `scd2_of` — dimensions

The full **11-check SCD suite** from `06` §3, plus two hop-level checks:

| # | Check | Catches |
|---|---|---|
| L3.D1 | Every silver business key has a current dimension row | Keys dropped between silver and gold |
| L3.D2 | Every current dimension row's tracked columns match silver | The dimension drifting from its source of truth |

L3.D2 is the same statement as SCD check 7 (`tracked_change_created_version`), pointed at silver
instead of a staging table — which is why the SCD suite and layer parity share a template.
**This is the pairing that catches the motivating bug**: L3.D2 says gold disagrees with silver,
and SCD check 7 says no new version was opened to explain it.

### 5.2 `conserved` — facts

| # | Check | Catches |
|---|---|---|
| L3.F1 | Grain uniqueness on the gold fact | Duplicated facts |
| L3.F2 | Fanout guard: gold rows per silver key ≤ `fanout_max` | A join multiplying rows |
| L3.F3 | Row conservation silver → gold with named losses | Silent filtering |
| L3.F4 | Additive measure conservation: `sum(silver.m) = sum(gold.m)` within tolerance | Precision loss, double-counting, a wrong join |
| L3.F5 | FK resolution: every dimension key resolves to a version **valid at the event timestamp** | Facts pointing at a version that does not exist yet, or no longer does |

L3.F5 is time-aware, which is what makes it different from a plain FK check:

```sql
-- templates/layer/fk_resolves_as_of.sql.j2
select
    object_construct('{{ fact_key }}', f.{{ fact_key }}) as failing_key,
    'L3_FK_UNRESOLVED_AS_OF' as violation_code,
    object_construct('{{ fk_column }}', f.{{ fk_column }}, 'event_ts', f.{{ event_ts }}) as detail
from {{ fact_table }} f
left join {{ dim_table }} d
       on f.{{ fk_column }} = d.{{ dim_business_key }}
      and f.{{ event_ts }} >= d.{{ valid_from }}
      and f.{{ event_ts }} <  coalesce(d.{{ valid_to }}, '{{ open_end_sentinel }}'::timestamp_ntz)
where {{ scope_predicate }}
  and d.{{ dim_business_key }} is null
limit {{ sample_limit }}
```

### 5.3 `aggregate_of` — marts

| # | Check | Catches |
|---|---|---|
| L3.M1 | Group cardinality: gold groups = distinct silver groups | Missing or invented groups |
| L3.M2 | Grain uniqueness on `group_by` | A duplicated mart row |
| L3.M3 | **Recomputed additive measures match**, within tolerance | The core mart assertion |
| L3.M4 | Non-additive measures are declared and excluded, and that exclusion is reported | Someone summing an average |

```sql
-- templates/layer/aggregate_of.sql.j2
-- Recompute the mart from silver and compare group by group.
-- Non-additive measures ({{ non_additive | join(", ") }}) are deliberately excluded — see report.
with recomputed as (
    select {{ group_by | join(", ") }},
           {% for m in additive_measures %}
           {{ m.expr }} as {{ m.column }}{{ "," if not loop.last }}
           {% endfor %}
    from {{ silver_table }}
    where {{ silver_scope_predicate }}
    group by {{ group_by | join(", ") }}
)
select
    object_construct({% for g in group_by %}'{{ g }}', coalesce(m.{{ g }}, r.{{ g }}){{ "," if not loop.last }}{% endfor %}) as failing_key,
    case
        when m.{{ group_by[0] }} is null then 'L3_GROUP_MISSING_IN_MART'
        when r.{{ group_by[0] }} is null then 'L3_GROUP_NOT_IN_SOURCE'
        else 'L3_MEASURE_MISMATCH'
    end as violation_code,
    object_construct(
        {% for m in additive_measures %}
        '{{ m.column }}', object_construct('mart', m.{{ m.column }}, 'recomputed', r.{{ m.column }}){{ "," if not loop.last }}
        {% endfor %}
    ) as detail
from {{ mart_table }} m
full outer join recomputed r
  on {% for g in group_by %}m.{{ g }} = r.{{ g }}{{ " and " if not loop.last }}{% endfor %}
where m.{{ group_by[0] }} is null
   or r.{{ group_by[0] }} is null
   {% for m in additive_measures %}
   or abs(coalesce(m.{{ m.column }}, 0) - coalesce(r.{{ m.column }}, 0))
      > greatest({{ m.tolerance.abs }}, {{ m.tolerance.rel }} * abs(coalesce(r.{{ m.column }}, 0)))
   {% endfor %}
limit {{ sample_limit }}
```

L3.M4 is not a SQL check — it is a **reporting obligation**. `AVG_BASKET` and `UNIQUE_BUYERS`
cannot be verified by recomputing an additive aggregate, so they are declared non-additive,
excluded, and listed in the run's coverage output as **unvalidated at the measure level**. A mart
reported as green with two silently unverified measures is the exact lie of omission this product
exists to end.

---

## 6. How the AI agent generates these — and why it stays deterministic

The user-facing property: *an agent produces the tests; the tests are deterministic code.*

```
   DETERMINISTIC                  LLM (LangGraph)              HUMAN                DETERMINISTIC
   ─────────────                  ───────────────              ─────                ─────────────
   catalog: schemas,              infer_layer_map              confirm              expand hop → check set
   row counts, key                → which bronze feeds         · dedup key          (a lookup table:
   cardinality,                     which silver, which        · pick rule            relation → templates)
   duplicate profiles               silver feeds which gold    · mart group_by            ↓
                                                               · non-additive       render Jinja → SQL
   repo: sqlglot parse            infer_dedup_contract           measures                 ↓
   of the transform SQL           → dedup key, pick rule                            four validation gates
   (ROW_NUMBER OVER,                ORDER BY, reject table,                                ↓
    QUALIFY, DISTINCT ON,           filter predicates                               git PR (human merges)
    GROUP BY, MERGE)
                                  infer_measure_map
   → candidate contracts            → additive vs non-additive
```

### 6.1 What the deterministic layer hands the model

The transform SQL usually *contains* the contract. `sqlglot` extracts it before the LLM is asked
anything:

| Pattern found in the transform | Extracted |
|---|---|
| `ROW_NUMBER() OVER (PARTITION BY … ORDER BY …) = 1` / `QUALIFY` | Dedup key **and** pick rule, verbatim |
| `DISTINCT ON (…) … ORDER BY …` (Postgres origin) | Same |
| `GROUP BY` in a mart model | Candidate `group_by`, and the measure expressions |
| `MERGE … WHEN MATCHED THEN UPDATE / WHEN NOT MATCHED THEN INSERT` | SCD2 vs overwrite shape |
| `INSERT INTO …_REJECT` / a `REJECT_REASON` column | Reject bucket and its reason column |
| `WHERE` clauses that reduce row count | Candidate declared filters |

When the parser finds an explicit `QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY
source_updated_at DESC) = 1`, the dedup contract is **read, not inferred**, and the LLM's job
shrinks to "does this partition key match the business meaning of a customer?" That is a
classification question, which is what models are reliable at.

### 6.2 What the LLM actually decides

| Node | Decision | Output type | Fallback if the LLM is unavailable |
|---|---|---|---|
| `infer_layer_map` | Which table feeds which across hops | `list[HopProposal]` | Name matching (`BRONZE.X` → `SILVER.X`) plus lineage from `sqlglot` |
| `infer_dedup_contract` | Dedup key + pick rule, when not literally in the SQL | `DedupContract` | Parsed `QUALIFY`/`DISTINCT ON` only; otherwise the hop is `unvalidated` |
| `classify_losses` | Is this `WHERE` a business filter or a bug? | `list[DeclaredLoss]` | All reducing predicates proposed as filters, flagged for review |
| `infer_measure_map` | Additive vs non-additive measures | `MeasureMap` | `sum`/`count` additive; everything else non-additive (the safe direction) |

Every one of these returns a Pydantic model via `with_structured_output`. **None of them emits
SQL.** The check SQL comes from `expand_hop()`, a lookup from `relation` to a template list:

```python
HOP_CHECK_BUNDLES: dict[str, list[str]] = {
    "identical":   ["layer/row_conservation", "layer/key_set_equal",
                    "parity/full_outer_diff", "cast/cast_failure_rate",
                    "layer/duplicate_profile_preserved", "layer/batch_immutable"],
    "dedup_of":    ["layer/dedup_key_unique", "layer/dedup_pick_rule_fidelity",
                    "layer/dedup_pick_rule_total", "layer/dedup_value_fidelity",
                    "layer/row_conservation", "layer/no_invented_keys", "layer/no_lost_keys",
                    "layer/reject_reasons_closed", "domain/not_null", "domain/value_in_range"],
    "scd2_of":     SCD_SUITE_TEMPLATES + ["layer/dim_key_coverage", "layer/dim_matches_silver"],
    "conserved":   ["keys/grain_unique", "fanout/join_fanout_guard", "layer/row_conservation",
                    "layer/measure_conservation", "layer/fk_resolves_as_of"],
    "aggregate_of":["layer/group_cardinality", "keys/grain_unique", "layer/aggregate_of"],
    "unvalidated": [],     # generates NO checks, and one coverage row saying so
}
```

That dictionary is the whole of "the agent generates the tests." The agent picks the relation and
fills the parameters; the bundle is a lookup; the SQL is a reviewed template. Roughly 85% of layer
checks are a pure parameter fill with nothing for a reviewer to read but a config diff.

### 6.3 The human confirmations

Three, all raised in the same LangGraph `interrupt()` as SCD grain (`09` §1.4), because they are
the same class of decision — wrong, and the check passes while comparing nothing:

1. **Dedup key** — "is `CUSTOMER_ID` what makes two bronze rows the same customer?"
2. **Pick rule** — "when two rows tie on `SOURCE_UPDATED_AT`, is `SOURCE_ROW_ID DESC` the right
   tiebreaker, or should this be an error?"
3. **Mart `group_by` and non-additive measures** — the aggregate grain.

The interrupt shows the extracted evidence (the actual `QUALIFY` clause from the transform, the
observed duplicate profile, the observed group cardinality) so the expert is confirming against
data, not against a model's assertion.

---

## 7. Mutation injectors for layer checks

Gate 3 (`10`) applies unchanged. Every layer template ships an injector; CI fails if one is
missing.

| Check | Injected bug | Expected code |
|---|---|---|
| L1.1 row conservation | Delete N rows from the bronze clone | `L1_ROW_COUNT_MISMATCH` |
| L1.5 duplicate profile | Dedup one key in the bronze clone | `L1_DUPLICATE_PROFILE_CHANGED` |
| **L2.1 dedup happened** | Insert a second row for one silver key | `L2_DUPLICATE_SURVIVED_DEDUP` |
| L2.2 no invented rows | Insert a silver key absent from bronze | `L2_SILVER_ROW_NOT_IN_BRONZE` |
| L2.3 no lost keys | Delete one silver row with no matching reject | `L2_KEY_LOST_WITHOUT_REASON` |
| L2.4 conservation | Delete N silver rows without a reject entry | `L2_UNEXPLAINED_ROW_LOSS` |
| **L2.5 pick-rule fidelity** | **Replace the surviving row with a different duplicate from the same key** | `L2_WRONG_DUPLICATE_KEPT` |
| **L2.6 pick-rule totality** | **Duplicate a bronze row so it ties on the full ordering** | `L2_PICK_RULE_NOT_TOTAL` |
| L2.7 value fidelity | Alter one business column on a surviving silver row **to a value held by no bronze row for that key** | `L2_SURVIVING_ROW_VALUE_MISMATCH` |
| L2.8 reject reasons | Write a reject row with a reason outside the closed set | `L2_UNKNOWN_REJECT_REASON` |
| L3.D2 dim matches silver | Change a tracked column in silver, not in gold | `SCD_OVERWRITE_INSTEAD_OF_VERSION` |
| L3.F2 fanout | Duplicate a dimension key so the join multiplies | `L3_FANOUT_EXCEEDED` |
| L3.F4 measure conservation | Scale one gold `AMOUNT` by 100 | `L3_MEASURE_MISMATCH` |
| L3.F5 FK as-of | Shift a dimension `VALID_FROM` past an event timestamp | `L3_FK_UNRESOLVED_AS_OF` |
| L3.M3 aggregate | Add 1 to one mart measure | `L3_MEASURE_MISMATCH` |

The L2.5 injector is the important one. It is the only way to prove that "the wrong duplicate
survived" is detectable at all — and it is a defect that *every other check in the suite passes
through*.

---

## 8. Cost and scheduling

| Hop | Where it runs | Typical cost | Cadence |
|---|---|---|---|
| L1 | Lane B, tiered (`07`) | Dollars, budget-capped | On demand; on the PR gate; nightly during cutover |
| L2 | Lane A, Snowflake | Cents — aggregates and one keyed join over a batch | Every load |
| L3 | Lane A, Snowflake | Cents | Every load |

L2 and L3 are scoped to the **completed batch**, not the whole table, which is what keeps them at
"every load" cost. L2.5's full-outer-join runs over one batch's keys, not the dimension's history.

**Ordering within a run matters.** Checks execute L1 → L2 → L3, and a failure at an earlier hop
**suppresses downstream incident creation** for tables derived from it — the results are still
recorded, marked `SUPPRESSED_BY: <incident_id>`. One upstream break must produce one incident at
the earliest failing layer, not thirty tickets down the medallion (risk R6). The layer graph makes
"earliest failing layer" a deterministic computation rather than something the reporting agent has
to infer.

## 9. Coverage output

Layer parity changes what the coverage report has to say:

```
Layer coverage — project orders, run 01JB…

  L1 source → bronze     4 of 4 tables      ✓ parity verified (tier 0, 2026-09-04)
  L2 bronze → silver     4 of 4 tables      ✓ dedup contract confirmed on 4
                                            ⚠ 1 pick rule not total (CUSTOMERS: 12 ties)
  L3 silver → gold       3 of 5 objects     ✗ MART_DAILY_REVENUE group_by unconfirmed
                                            ⚠ 2 measures non-additive, unverified
                                              (AVG_BASKET, UNIQUE_BUYERS)
                                            ✗ MART_CHURN unvalidated — no silver counterpart

  Columns   128 compared, 31 excluded (24 audit, 5 pii-hashed, 2 transform)
```

Every warning line above is a thing the tool *cannot* currently vouch for, stated in the same
report as the green ones. That is the point of the exercise.
