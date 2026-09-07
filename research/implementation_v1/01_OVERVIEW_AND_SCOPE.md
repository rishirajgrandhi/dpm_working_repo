# 01 — Overview and v1 Scope

## 1. What the product does

A pipeline migrates data from **AWS RDS** into **Snowflake**. Two questions need answering:

- **Lane A — "is the pipeline's logic correct?"** Answered with plain SQL inside Snowflake alone.
  Cheap, scheduled, unattended, fully reported to owning developers.
- **Lane B — "do RDS and Snowflake agree?"** Answered by comparing the two systems.
  Expensive, on demand (a button in the app, or the PR gate), results returned to whoever ran it.

The tool **reports on data, it never changes it.** No writes to RDS. In Snowflake it writes only to
its own state schema and a scratch schema it owns.

## 2. Why Lane A carries the weight

The motivating failure — an SCD2 dimension quietly turning into an overwrite — is invisible to
Lane B. The target still matches the source; only the *history* is gone. Lane A catches it.
So v1 invests first in Lane A and the SCD suite, and treats Lane B as a bounded, budgeted
convenience for migration cutover.

## 2b. The pipeline is a medallion, and every hop gets checked

The migration lands as `AWS RDS → BRONZE → SILVER → GOLD`. v1 checks **every hop**, not just the
endpoints — full detail in [`07B`](07B_LAYER_PARITY_MEDALLION.md).

| Hop | Lane | Anchoring relation | Cost |
|---|---|---|---|
| L1 source → bronze | B (cross-engine) | `identical` — bronze is a faithful landing, no cleaning, no dedup | Tiered, budget-capped |
| **L2 bronze → silver** | A | **`dedup_of`** — the initial condition of this hop: silver is bronze deduplicated on a declared key by a total, deterministic pick rule, minus named losses | Cheap |
| L3 silver → gold | A | `scd2_of`, `conserved`, `aggregate_of` by object type | Cheap |

L2 is the hop with an explicit contract, so it carries the most checks (nine). The two that
justify the whole exercise are **"the *right* duplicate survived"** and **"the pick rule resolves
every tie"** — a dedup that keeps *an arbitrary* row per key passes every row-count, key-set, and
conservation check while returning different data on every rerun.

Every check on every hop is deterministic templated SQL. The AI agent's role is to infer the hop
contract (which table feeds which, the dedup key, the pick rule, the declared losses, the measure
map) and fill template parameters; a human confirms the dedup key, the pick rule, and the mart
grain. See `07B` §6.

## 3. In scope for v1

### 3.1 Lane A checks (Snowflake-only SQL)

| Family | Checks | Priority |
|---|---|---|
| SCD suite | 11 checks per SCD2 dimension (see `06`) | **P0** |
| **L2 dedup contract** | 9 checks per bronze→silver hop, incl. pick-rule fidelity and totality (see `07B` §4) | **P0** |
| **L3 layer parity** | Dimension coverage, fact conservation + fanout + as-of FK, mart aggregate recomputation (see `07B` §5) | **P0** |
| Key / grain | Primary key uniqueness, declared grain uniqueness, no all-NULL keys | **P0** |
| Referential | Every FK resolves to a parent; orphan count and sample | P1 |
| Domain | Value in range, value in enum, NOT NULL on business columns | P1 |
| Cast health | Cast-failure rate between raw landing and typed layer | P1 |
| Conservation | rows(in) = rows(out) + named losses, across layers | P1 |
| Fanout | A join did not multiply rows beyond a declared bound | P2 |
| Business rule | Hand-written, human-reviewed predicates | P2 |
| Unvalidated | Declared, never executed; exists so coverage is honest | **P0** |

**The priority letters order work *within* a milestone; they do not map onto one.** The actual
build order (`15`) is:

| Family | Milestone |
|---|---|
| Key / grain, `unvalidated` + the coverage view | M1 → M2 |
| The 11-check SCD suite | **M2** |
| L2 dedup contract, L3 layer parity, and the conservation / fanout / domain / FK templates the hop bundles pull in | **M2B** |
| Cast health and the L1 families | M5 (Lane B) |
| Hand-written business rules | Per project, after M2B — not on a milestone |

M3 and M4 add no check families; they add deterministic reporting and then the reporting agent.

### 3.2 Lane B (RDS ↔ Snowflake) — the L1 hop

A **three-tier, budget-capped** comparison — aggregate fingerprints, then bucketed drill-down,
then a landed row-level diff in a Snowflake scratch schema. Full detail in `07`; its role as the
source→bronze hop in `07B` §3.

### 3.3 Run correctness (applies to every check)

- Four-way column classification: `key`, `business`, `audit` (excluded by default), `derived`.
- Every run records **which columns were compared and which were skipped**, in the result row.
- Row scoping to completed batches only — never read mid-load.
- Explicit numeric scale, UTC timestamps, NULL policy, float tolerance.
- Snowflake identifier case policy fixed once (see `05`), because RDS is lower-case-folding and
  Snowflake is upper-case-folding — the single most common source of spurious "column missing".

### 3.4 AI authoring (LangGraph)

Catalog discovery → table/column classification → manifest proposal → check generation →
four validation gates → **git PR**. Human confirms grain via an explicit interrupt. Never sees
row values, only metadata.

### 3.5 AI reporting (LangGraph)

Deterministic evidence collection first → closed-category classification → grouping related
failures into one incident → ownership routing from `git blame` → Slack + Jira with feedback
buttons.

### 3.6 Operations

State store in Snowflake, sign-off tracking bound to a commit SHA, staleness detection,
coverage reporting, **shadow mode** for Lane A (report to a private channel, file no tickets),
PII handling, and a web app that is the product's entire user surface (`12`) — health, incidents,
the agent review queue, on-demand parity, and sign-offs. There is no CLI.

## 4. Deferred out of v1 (revisit later)

- Anomaly detection, learned thresholds, drift detection — no history yet, too noisy.
- Streaming sources.
- BI / reverse-ETL parity.
- Circuit breaker / dataset quarantine.
- Cross-project or multi-tenant dashboard.
- Any warehouse other than Snowflake; any source other than RDS.
- Continuous (scheduled) Lane B — v1 is on-demand only, by design.

## 5. Out of scope on principle (permanent)

- The tool never mutates source or product data.
- No AI output goes live without human review — the authoring agent's only output channel is a PR.
- AI is never in the data path. The worst case is a bad *suggestion*, never bad *data*.
- No writes of any kind to RDS. The RDS role is read-only, enforced at the role level, and the
  tool fingerprints its own grants at startup.
- No blocking a deploy without team buy-in. The PR gate is non-blocking in v1.
- No requirement that dbt, an orchestrator, or a catalog exist. If they do, they are a signal.

## 5b. v1 definition of done

The SCD suite **and** all three medallion hops running on one real pipeline, on a schedule, with:

- the L2 dedup contract confirmed by a human (key + pick rule) and its nine checks green,
- L1 parity verified at least once against RDS within budget,
- L3 covering every gold object or explicitly reporting it as `unvalidated`,
- every check deterministic, templated, and mutation-tested,
- Slack + Jira alerts deduplicated, filed at the earliest failing layer,
- a per-layer coverage view stating what is *not* checked.

## 6. Success criteria for v1

| Metric | v1 target |
|---|---|
| Mutation-test pass rate on active checks | 100% — non-negotiable, enforced in CI |
| Runs reporting column exclusions | 100% |
| Checks with human-confirmed grain | 100% |
| Lane A precision (incidents confirmed real) | > 70% at launch |
| Root-cause accuracy — predicted category vs. the category a human confirmed on close | > 60% at launch, improving |
| Monitoring cost per pipeline per month | Below the cost of one incident |
| Lane A alerts per engineer per week | < 5 |
| Onboarding cost, project two vs. project one | ≤ 25% |

Deliberately **not** tracked: number of checks, percentage of checks passing, number of tickets
filed, or a headline coverage percentage without its breakdown.
