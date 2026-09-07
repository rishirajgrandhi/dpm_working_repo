# 10 — The Four Validation Gates

A check that can never fail is worse than no check at all: it manufactures confidence. Before any
generated check may be activated, it must pass four gates. All four are **deterministic** — no LLM
is involved in deciding whether a check is trustworthy.

The gate results are stored on `DPHM_STATE.CHECKS` and re-run whenever `SQL_SHA256` changes.

| Gate | Question | Failure means |
|---|---|---|
| 1. Budget | Does it run within time and cost? | Too expensive to run on schedule |
| 2. Passes on good data | Does it pass on data we believe is correct? | It is wrong, or the data is |
| 3. **Mutation** | Does it fail when we inject the exact bug it exists to catch? | **It can never fail. Reject.** |
| 4. Determinism | Same result twice on frozen data? | Non-deterministic; will produce flapping alerts |

## Gate 1 — Budget

```
Run the check against production with EXPLAIN and then for real, once.
Record: elapsed ms, bytes scanned, Snowflake credits.
Pass if:  elapsed <= check.budget_ms (default 60_000)
      and estimated_usd <= check.budget_usd (default 0.05)
      and the check's share of lane_a_warehouse_seconds_per_run is under its allocation
```

A failing check is not discarded; the repair node is asked to add partition pruning or narrow the
scope predicate, and it is re-gated. After two attempts it lands in the PR as `draft` with the
measured cost, so a human can decide whether it is worth a bigger warehouse.

## Gate 2 — Passes on known-good data

```
Run against a "golden" scope: a batch range a human has marked as believed-correct
(a row in `DPHM_STATE.GOLDEN_WINDOWS`, set during onboarding — see `08`).
No golden window for the table means the gate cannot run, so the check stays `draft`
and the gate result records `NO_GOLDEN_WINDOW` rather than passing by default.
An `INFERRED` window (the Q12 fallback: the most recent 7 incident-free days) is
allowed, and every gate result says which kind it used.
Pass if:  0 violations returned, AND evaluated_row_count >= tolerance.min_rows_to_evaluate
```

The second clause is essential. A check that returns zero rows because it evaluated zero rows has
not passed — it has abstained. Without this clause, an empty-table check and a wrong-grain check
both look green, and both are the failure this product exists to prevent.

## Gate 3 — Mutation testing (non-negotiable)

**Every check family ships with a bug injector that produces exactly the defect the check
claims to catch.** This is the foundation of trust in the whole system and the one metric with a
100% target.

### Mechanics

```
1. CREATE TABLE DPHM_SCRATCH.MUT_<check_id>_<ulid> CLONE <table>;   -- Snowflake zero-copy clone
2. Apply the mutation to the clone (a small, targeted UPDATE/INSERT/DELETE).
3. Render the check against the clone (same params, same column rules, same scope).
4. Assert: the check returns >= 1 violation, and the violation_code is the expected one.
5. DROP TABLE ... in a finally block.
```

Zero-copy clone makes this cheap even on large tables — no data movement, and the mutation
touches only the rows it rewrites. Clones live in `DPHM_SCRATCH` and are swept nightly.

### Injector catalogue (SCD suite)

| Check | Injected bug | Expected code |
|---|---|---|
| 1. one current row per key | Set `IS_CURRENT = true` on a second version of one key | `SCD_MULTIPLE_CURRENT` |
| 2. no overlapping ranges | Move one `VALID_FROM` back past the previous `VALID_TO` | `SCD_OVERLAPPING_RANGE` |
| 3. no gaps | Push one `VALID_FROM` forward by a day | `SCD_GAP_IN_HISTORY` |
| 4. well-formed ranges | Swap one row's `VALID_FROM` and `VALID_TO` | `SCD_MALFORMED_RANGE` |
| 5. flag agrees with range | Set `IS_CURRENT = false` on the open-ended row | `SCD_FLAG_RANGE_MISMATCH` |
| 6. no spurious versions | Duplicate the current row with a new `VALID_FROM`, no tracked change | `SCD_SPURIOUS_VERSION` |
| 7. **tracked change created version** | **Update a tracked column on the current row in place, opening no new version** | `SCD_OVERWRITE_INSTEAD_OF_VERSION` |
| 8. type-1 consistency | Change a type-1 column on the current row only | `SCD_TYPE1_INCONSISTENT` |
| 9. surrogate key unique | Insert a duplicate `CUSTOMER_SK` | `SCD_SK_DUPLICATE` |
| 10. FK resolves | Delete one parent version | `FK_UNRESOLVED` |
| 11. closed versions immutable | Edit a business column on a closed version | `SCD_CLOSED_VERSION_MUTATED` |

Injector 7 is a literal reproduction of the motivating incident. If it ever stops failing the
check, the product's core claim is broken, and CI must go red.

### Injectors for other families

| Family | Injection |
|---|---|
| `keys/grain_unique` | Duplicate one row at the declared grain |
| `ref/fk_resolves` | Orphan one child row |
| `domain/value_in_range` | Push one value one unit outside the bound |
| `domain/value_in_enum` | Set one value to a string not in the enum |
| `cast/cast_failure_rate` | Write a non-castable string into the raw column |
| `conservation/row_conservation` | Delete N rows from the output layer |
| `fanout/join_fanout_guard` | Duplicate a parent key to multiply the join |
| `layer/*` (all medallion hop checks) | One injector each — full catalogue in `07B` §7 |
| `business/custom` | **Author-supplied injector required** — no injector, no activation |

Two layer injectors carry the same weight as SCD injector 7, and for the same reason — they are
the only proof that a defect *every other check passes through* is detectable:

- **L2.5** — replace the surviving silver row with a different duplicate of the same key. Row
  counts, key sets, and conservation all stay green; only the pick-rule check fires.
- **L2.6** — duplicate a bronze row so it ties on the full pick ordering. Nothing is wrong with
  the *data*; the *contract* is under-specified, and the pipeline is silently non-reproducible.

The last row is the policy that makes the gate hold: a hand-written business rule cannot be
activated unless its author also writes the bug that it catches. That is a small amount of work
at exactly the moment the author understands the rule best.

### Anti-cheat

The mutation runner asserts three things beyond "it failed":

1. The check **passed** on the same clone *before* the mutation (so it is not failing for an
   unrelated reason).
2. The returned `violation_code` matches the expected one (so it is not failing by accident).
3. The failing key set **includes the mutated key** (so it is detecting *that* bug, not noise).

## Gate 4 — Determinism

```
Snapshot the table with a zero-copy clone (frozen data).
Run the check twice against the clone.
Pass if: identical failing_fingerprint and identical failing_row_count both times.
```

Catches `CURRENT_TIMESTAMP` in a predicate, `LIMIT` without `ORDER BY`, sampling, and unstable
window functions with ties. A check that fails gate 4 will eventually flap, and a flapping check
trains its audience to ignore it.

Some source queries are non-deterministic by nature. Those are allowed to declare
`determinism: acknowledged_nondeterministic`, which **skips gate 4 and permanently downgrades the
check's severity**, labelling it as such in every report. The tool says so rather than flagging a
false failure.

## Enforcement points

| Where | Enforcement |
|---|---|
| Authoring graph | `run_gates` node; failures route to repair, then to `draft` |
| App / API | `POST /checks/{id}/activate` refuses a check with any gate false; the Activate button is disabled with a tooltip naming the failed gate, so the refusal is visible before the click |
| CI (pipeline repo PR) | Every check in the diff is re-gated; the PR cannot merge if gate 3 fails |
| CI (dphm repo) | Every template's injector runs against the fixture warehouse on every commit |
| Scheduled run | A check whose `SQL_SHA256` no longer matches its validated hash is skipped and reported as `UNVALIDATED`, never run blind |

## Reporting on the gates

Every run's summary includes:

```
Checks:          41 active, 3 draft (failed gates), 2 unvalidated
Mutation:        41/41 verified                    ← must be 100%
Grain confirmed: 12/13 tables (MART_DAILY_REVENUE pending)
Contracts:       4/4 dedup contracts confirmed; 1 pick rule not total (CUSTOMERS)
Columns:         128 compared, 31 excluded (24 audit, 5 pii-hashed, 2 transform)
Coverage:        L1 4/4 · L2 4/4 · L3 3/5 · 4 objects unvalidated
```

That last pair of lines is the honest part of the report, and it appears whether the run is green
or red. A green run with an unstated exclusion list is a lie of omission.
