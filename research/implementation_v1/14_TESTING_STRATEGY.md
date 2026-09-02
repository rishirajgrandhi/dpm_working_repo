# 14 — Testing Strategy

The product's claim is "these checks can be trusted." That claim is only as good as this
document. Two things here are load-bearing: **SQL snapshot tests** (templates behave as reviewed)
and **mutation tests** (checks can actually fail).

## 1. Layers

| Layer | Runs where | Speed | Gate on |
|---|---|---|---|
| Unit | Every commit, no warehouse | seconds | PR |
| Frontend unit + component | Every commit, jsdom | seconds | PR |
| API contract | Every commit, TestClient | seconds | PR |
| E2E (Playwright) | Every commit, seeded API | ~2 min | PR |
| SQL snapshot | Every commit, no warehouse | seconds | PR |
| Graph / agent | Every commit, stub LLM | seconds | PR |
| Integration (Postgres) | Every commit, testcontainers | ~1 min | PR |
| Integration (Snowflake) | Every commit, test schema | ~3 min | PR |
| **Mutation** | Every commit, Snowflake scratch | ~5 min | **PR — blocking** |
| Live smoke | Nightly, against the real project | ~10 min | Alert only |

## 2. Unit tests

- `config/` — every invalid YAML shape produces a precise error. Especially: surrogate key inside
  business key, tracked ∩ type-1 non-empty, `lane_b` without `source_table`, secret literals.
- `engine/columns.py` — classification precedence (override > pattern > default), the exclusion
  set, PII hashing, and the exact `columns_compared` / `columns_excluded` output.
- `engine/scope.py` — each batch strategy produces the expected predicate; an unresolvable
  strategy raises rather than falling back to wall-clock.
- `state/incidents.py` — fingerprint stability under key reordering; new row → new fingerprint.
- `state/signoff.py` — expiry when a watched path moves; hard ceiling.
- `parity/canonical.py` — the canonicalization table in `07` §3, one test per type, both engines.
- `config/models.py` `DedupContract` — a single-term pick rule with `must_be_total` raises; an
  unconfirmed dedup key makes the hop non-activatable.
- `engine/hops.py` — every `relation` maps to a bundle; `unvalidated` maps to the empty list.
- `engine/layer_graph.py` — "earliest failing layer" over a fixture DAG, including a diamond.
- `repo/dedup_extract.py` — `QUALIFY ROW_NUMBER()`, `DISTINCT ON`, and a `MERGE` each yield the
  expected key and ordering; an unparseable transform yields `None`, never a guess.

## 3. SQL snapshot tests

Every `(template, parameter set)` pair renders to a golden `.sql` file checked into
`tests/sql_snapshots/`. A diff in a snapshot is a **behaviour change**, and the PR must say so.

```
tests/sql_snapshots/
  scd/one_current_row_per_key__single_key.sql
  scd/one_current_row_per_key__composite_key.sql
  scd/tracked_change_created_version__5_tracked_cols.sql
  layer/dedup_pick_rule_fidelity__two_term_order.sql
  layer/dedup_pick_rule_total__composite_key.sql
  layer/row_conservation__three_named_losses.sql
  layer/aggregate_of__mixed_additive_measures.sql
  parity/full_outer_diff__with_float_tolerance.sql
  ...
```

Each snapshot is also parsed by `sqlglot` in the Snowflake dialect, so a template can never merge
in a state that does not parse.

## 4. Integration tests

**Postgres side** (`testcontainers`): a fixture `orders_prod` database with `customers`, `orders`,
deliberate NULLs, a `numeric(12,2)` column, a `jsonb` column, and a `timestamptz` column — one
instance of every canonicalization case in `07` §3.

**Snowflake side**: a dedicated `DPHM_TEST` database with `BRONZE` / `SILVER` / `GOLD`, seeded
from the same fixture data through a fixture "pipeline" that implements a real
`QUALIFY ROW_NUMBER()` dedup at L2 and a real SCD2 merge at L3. Torn down and reseeded per CI run.
The bronze fixture deliberately contains duplicate keys, a full-ordering tie, and a rejected row,
so the L2 contract has something to actually assert.

Covered end-to-end:

1. `GET /projects/{p}/diagnostics` on the fixture reports every connection healthy.
2. `POST /projects/{p}/onboard` (dry run) proposes the right table types and grain candidates (stub LLM).
3. `POST /projects/{p}/runs` produces the expected 11 SCD results, all PASS.
4. A seeded bug produces exactly one incident with the expected category and fingerprint.
5. The same bug on the next run updates the incident rather than creating a second.
6. A parity run on identical data stops at tier 0 (asserted by query count).
7. A parity run with 3 divergent rows reaches tier 2 and names those 3 rows and their columns.
8. A revoked grant produces `INCONCLUSIVE` + a permission event, not `FAIL`.
9. **The L2 dedup contract**: the fixture bronze table carries deliberate duplicates with a known
   correct winner. All nine L2 checks pass; swapping the winner fails only
   `dedup_pick_rule_fidelity`; adding a full-ordering tie fails only `dedup_pick_rule_total`.
10. An L2 failure marks the derived L3 results `SUPPRESSED_BY` and files exactly one incident.
11. A mart with a non-additive measure reports that measure as unverified in `COVERAGE`.
12. A hop with no inferable dedup contract emits zero checks and one `unvalidated` coverage row.

Test 6 is the one that guards the cost model: if a clean parity run ever issues more than the
tier-0 query count, the budget story is broken.

## 5. Mutation tests (the blocking gate)

Described in `10`. Mechanically:

```python
@pytest.mark.parametrize("case", MUTATION_CATALOGUE)   # one entry per template
def test_check_catches_its_bug(sf, case):
    clone = sf.zero_copy_clone(case.table)             # DPHM_SCRATCH
    try:
        assert run_check(case.check, clone).status == "PASS"      # anti-cheat 1
        case.inject(sf, clone)
        result = run_check(case.check, clone)
        assert result.status == "FAIL"
        assert result.violation_code == case.expected_code        # anti-cheat 2
        assert case.mutated_key in result.failing_keys            # anti-cheat 3
    finally:
        sf.drop(clone)
```

**CI fails on any missing catalogue entry.** A new template without an injector cannot merge —
enforced by a test that asserts `set(templates) == set(MUTATION_CATALOGUE.keys())`.

## 6. Agent tests

- **Stub LLM**: `FakeListChatModel` (or a recorded-response fixture) returns pre-built structured
  outputs. No network in CI.
- **Schema fuzzing**: feed malformed / out-of-vocabulary output to every LLM node and assert the
  fallback edge is taken and `degraded = true` is set.
- **Interrupt test**: the authoring graph stops at `confirm_with_human`; resuming with a
  confirmation produces a manifest; resuming with a *rejection* re-proposes rather than proceeding.
- **Determinism**: the same fixtures + stub outputs produce a byte-identical PR diff.
- **Egress test (CI gate)**: no authoring run writes `ROW_VALUES_SENT = true`.
- **Prompt-change test**: changing a prompt file changes `PROMPT_SHA`, and the golden PR test is
  expected to be updated in the same commit — so a prompt edit is never invisible.

## 6b. Frontend and API tests

- **Generated types are a gate.** CI regenerates the TypeScript client from the OpenAPI schema and
  fails if it differs from what is committed. The API and the UI cannot drift silently.
- **RBAC contract tests**: every mutating endpoint is called as each of the four roles; Engineer
  must be rejected on sign-off and contract confirmation.
- **Component tests**: the coverage panel renders unvalidated objects and unverified measures even
  when every check passed — the "green run still shows what it missed" property, asserted.
- **Playwright E2E**, against a seeded API:
  1. An incident can be acknowledged and the state store records the acting user.
  2. A review-queue item shows its evidence, and answering it resumes the graph.
  3. The Activate button is disabled for a check with a failing gate, and the tooltip names it.
  4. A parity run streams SSE progress and can be aborted mid-tier.
- **No verdict logic in the frontend**: a lint rule bans comparison against thresholds in
  `web/src/lib`, and a test asserts the UI renders a `FAIL` purely from the API's status field.

## 7. Live smoke (nightly, non-blocking)

Runs the real project in shadow mode and alerts the platform team only on:
- a check that has been `INCONCLUSIVE` for 3 consecutive nights,
- a run exceeding its warehouse budget,
- a gate that no longer passes on an active check,
- a `SQL_SHA256` mismatch on an active check.

These are all "the tool itself is degrading" signals, which are invisible to the product's own
alerting because a degrading monitor reports nothing rather than something.

## 8. What we deliberately do not test

- The LLM's judgement quality. It is not deterministic and not the trust boundary — the gates
  and the human review are. We test that its output is *well-formed, cited, and in-vocabulary*.
- Snowflake's or Postgres's own semantics.
- Slack and Jira rendering beyond payload-shape contract tests.

## 9. Coverage targets

| Package | Line coverage | Note |
|---|---|---|
| `config/`, `engine/`, `parity/`, `state/` | ≥ 90% | The trust boundary |
| `agents/` | ≥ 70% | Graph topology and fallbacks, not model behaviour |
| `api/`, `runtime/` | ≥ 80% | Auth, RBAC, and job lifecycle are security-relevant |
| `reporting/` | ≥ 50% | Contract tests suffice |
| `web/` | ≥ 60% of components | Plus the E2E flows above, which matter more than the number |

Mutation-catalogue completeness is 100%, always, and is not a coverage number — it is a merge gate.
