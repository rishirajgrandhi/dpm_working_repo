# Data Pipeline Health Monitor — v1 Implementation Docs

This directory is the end-to-end implementation specification for the **initial version (v1)**
of the Data Pipeline Health Monitor described in
`../final_idea/Data_Pipeline_Health_Monitor_Design.docx (1).pdf`.

The design doc answers *what* and *why*. These docs answer *what we build first, and how*.

## Fixed premises for v1

| Premise | Value |
|---|---|
| Source system (pre-migration) | **AWS RDS** (PostgreSQL dialect assumed; MySQL noted where it differs) |
| Target warehouse (post-migration) | **Snowflake** |
| Pipeline code | A **git repository** the tool can clone and read |
| Agent framework | **LangChain + LangGraph** (deviation from the design doc's "Anthropic SDK direct" — see `02`) |
| User surface | **A web app — dashboard, review queue, forms. No CLI.** FastAPI + React (deviation from "Streamlit for v0" — see `02`) |
| Model | `claude-sonnet-5` via `langchain-anthropic`, escalating to `claude-opus-5` for grain/business-rule authoring |
| Language | Python 3.11+ |

## The three product inputs

1. **Git repo** — the pipeline code (transform SQL/Python, DDL, orchestration config).
2. **RDS connection** — the migration source of truth.
3. **Snowflake connection** — the migration target, and the tool's own state store.

Everything else (checks, manifests, incidents) is derived from these three.

## Read in this order

**[→ PRODUCT.md](PRODUCT.md) — start here.** The whole product in one read: the problem, the
shape of the solution, where the AI sits, what people see. Written for anyone who has to build,
explain, or fund this. Everything below is detail underneath that page.

| # | Doc | What it settles |
|---|---|---|
| 01 | [Overview and v1 scope](01_OVERVIEW_AND_SCOPE.md) | What ships in v1, what is explicitly cut |
| 02 | [Inputs, assumptions, deviations](02_INPUTS_AND_ASSUMPTIONS.md) | The three inputs in detail; where v1 departs from the design doc and why |
| 03 | [Architecture](03_ARCHITECTURE.md) | Components, control flow, lane split |
| 04 | [Repository layout](04_REPO_LAYOUT.md) | Package structure, module responsibilities |
| 05 | [Configuration spec](05_CONFIG_SPEC.md) | The ~50-line onboarding config; Pydantic models |
| 06 | [Check model and templates](06_CHECK_MODEL_AND_TEMPLATES.md) | Check IR, the SCD suite, Jinja SQL templates |
| 07 | [Lane B: RDS ↔ Snowflake](07_LANE_B_CROSS_ENGINE.md) | The cost-tiered cross-engine comparison |
| 07B | [Layer parity: source → bronze → silver → gold](07B_LAYER_PARITY_MEDALLION.md) | Per-hop check bundles; the bronze→silver **deduplication contract**; how the agent generates them |
| 08 | [State store](08_STATE_STORE.md) | Snowflake schema DDL and lifecycle |
| 09 | [Agents (LangGraph)](09_AGENTS_LANGGRAPH.md) | Authoring graph, reporting graph, tools, HITL |
| 10 | [Validation gates](10_VALIDATION_GATES.md) | The four gates, including mutation testing |
| 11 | [Reporting and incidents](11_REPORTING_AND_INCIDENTS.md) | Dedup, ownership, Slack + Jira |
| 12 | [Application surface](12_APPLICATION_SURFACE.md) | **Everything is a web app** — screens, API, roles, triggers. No CLI |
| 13 | [Security, PII, cost](13_SECURITY_PII_COST.md) | Redaction, egress log, budgets |
| 14 | [Testing strategy](14_TESTING_STRATEGY.md) | Unit, snapshot, mutation, integration |
| 15 | [Build plan](15_BUILD_PLAN.md) | Milestones M0–M6 with exit criteria |
| 16 | [Open questions](16_OPEN_QUESTIONS.md) | Blocking decisions before M0 |

## v1 definition of done

Running on **one real migrated pipeline**, end to end, on a schedule:

1. **The SCD suite** — 11 checks on one real SCD2 dimension, including the check that catches a
   dimension silently degrading into an overwrite.
2. **Layer-by-layer parity across the full medallion** (`07B`):
   - **L1 source → bronze** — RDS ↔ Snowflake, cost-tiered, budget-capped.
   - **L2 bronze → silver** — anchored on the **deduplication contract**: dedup key, a total and
     deterministic pick rule, and named losses. Nine checks, including *the right duplicate
     survived* and *the pick rule resolves every tie*.
   - **L3 silver → gold** — dimensions (SCD suite), facts (conservation, fanout, as-of FK), and
     marts (recomputed aggregates, with non-additive measures declared and reported as unverified).
3. **All checks are deterministic SQL from reviewed templates.** The AI agent infers the layer
   contract and fills parameters; it does not write the checks that decide pass/fail.
4. Column classification and exclusions reported on **every** run.
5. Row scoping to completed batches — never a mid-load read.
6. All four validation gates green, mutation testing especially.
7. Slack + Jira incident reporting with dedup, and one incident at the **earliest failing layer**.
8. A coverage view that states what is *not* checked, per layer.
9. **All of it driven from the web app** — health, incidents, the agent review queue where grain
   and dedup contracts get confirmed, on-demand parity, sign-offs. No user ever opens a terminal.
