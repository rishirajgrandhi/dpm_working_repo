# 02 — Inputs, Assumptions, and Deviations from the Design Doc

## 1. The three inputs

### Input 1 — The git repository (pipeline code)

**What we take:** a clone URL (HTTPS or SSH), a default branch, and a read-only deploy key or PAT.

**What we read from it:**

| Artifact | Used for |
|---|---|
| DDL / migration files | Declared schema, keys, SCD column names |
| Transform SQL or Python | Grain inference, join structure, fanout risk, tracked-column sets |
| Orchestration config (Airflow DAG, cron, shell) | Batch identity, load-completion signal, schedule alignment |
| `CODEOWNERS`, `git log`, `git blame` | Ownership routing and likely-author attribution |
| Existing dbt project, if present | Free lineage + tests as a **signal**, never a dependency |

**How we read it:** a shallow clone into a working dir, refreshed per run. Parsing is
deterministic (`sqlglot` for SQL, `ast`/`libcst` for Python, `GitPython` for history). The LLM
sees **excerpts selected by the deterministic layer**, never the whole repo dumped into a prompt.

**Binding:** every generated check records `source_commit_sha`. A sign-off is valid only for the
SHA it was granted against — this is how exceptions expire when code changes.

### Input 2 — AWS RDS connection (migration source)

- **Assumed engine:** PostgreSQL 13+. MySQL 8 is supported by the dialect layer; differences are
  flagged inline in `06`/`07`.
- **Access:** a **read-only** role. `SELECT` on the migrated schemas, `SELECT` on
  `information_schema` and `pg_catalog`. No `CREATE`, no temp tables, no writes — Lane B does all
  its scratch work on the Snowflake side.
- **Connectivity:** the runner must sit inside the VPC or reach RDS through a bastion / VPC
  endpoint. Assume no public exposure.
- **Used for:** Lane B only. Lane A never touches RDS.
- **Load posture:** every RDS query is issued with a statement timeout and a row cap, and
  Lane B has a hard per-run budget (see `13`). RDS is a production OLTP box; the tool must not
  be capable of hurting it.

### Input 3 — Snowflake connection (target warehouse + state store)

Two roles, deliberately separated:

| Role | Grants | Used by |
|---|---|---|
| `DPHM_READER` | `SELECT` on all monitored databases/schemas; `USAGE` on warehouse; read on `SNOWFLAKE.ACCOUNT_USAGE` | Every check |
| `DPHM_WRITER` | `ALL` on `DPHM_STATE` and `DPHM_SCRATCH` schemas only | State store, Lane B landing, mutation tests |

Checks execute as `DPHM_READER`. Only the state-store layer and the Lane B lander assume
`DPHM_WRITER`, and they may only touch those two schemas — enforced by grant, asserted at
startup, and re-asserted in CI.

## 2. Deviations from the design document

These are the five places v1 knowingly departs from the design doc. Each is a deliberate call.

### D1 — Agent framework: LangChain / LangGraph instead of the raw Anthropic SDK

The design doc specifies "Claude via the Anthropic SDK, structured output via tool use."
v1 uses **LangChain + LangGraph** instead, because the two agents in this product are not
single-call prompts:

- The **authoring agent** is a long multi-stage pipeline with a mandatory human interrupt at
  grain confirmation, and it must be resumable — an onboarding run spans hours and a domain
  expert's 30 minutes. LangGraph's checkpointer plus `interrupt()` gives us pause/resume directly.
- The **reporting agent** is a fan-in/fan-out graph over N failing checks with per-node retry and
  a strict "evidence before inference" ordering that is much easier to *enforce* as a graph
  topology than as prompt discipline.

What does **not** change: the model is still Claude (`langchain-anthropic` → `ChatAnthropic`),
structured output is still tool-use-backed (`llm.with_structured_output(Model)` over Pydantic v2),
and every LLM output is still a reviewable artifact. LangGraph is orchestration, not intelligence.

**Constraint we impose on ourselves:** no LangChain abstraction touches SQL generation or
execution. `sqlglot`, Jinja2, and the native drivers stay ours. LangChain is confined to
`agents/` and never appears in `engine/`.

### D2 — Cross-engine comparison re-enters scope

The design doc's assumption **A2** ("both sources being compared live in the same engine") is
**false for this deployment**: the whole point is RDS → Snowflake. The doc itself notes the
consequence — "cross-engine work re-enters scope."

v1's answer is to *contain* it rather than build a general cross-engine diff engine:

- Lane A, which is where the value is, remains 100% single-engine Snowflake SQL. Unchanged.
- Lane B uses a **cost-tiered pushdown-then-land** strategy: compute canonical aggregates on each
  engine independently and compare small result sets in Python; only when a bucket disagrees do
  we land that bucket's rows into `DPHM_SCRATCH` in Snowflake and run the design doc's original
  full-outer-join SQL against them.
- Hashing therefore **does** come back, but only as a bucket fingerprint over a canonicalized
  projection, plus the design doc's original use (closed SCD versions never change). Full spec
  and canonicalization rules in `07`.

### D5 — We *build* the cross-engine comparison; the design doc said *borrow*

D2 above records that assumption A2 is false. It does not record the second half of what the
design doc says about that case, and the omission matters. The full A2 consequence reads:

> "Cross-engine work re-enters scope — hashing, canonicalization, **a borrowed diff engine**.
> Substantially more work."

And §3's build-vs-borrow paragraph is explicit:

> "We **skip cross-engine hash diffing entirely**, and would **borrow an existing tool** if a
> second engine ever shows up."

§4.2 then lists "cross-engine comparison and all hashing logic" as **deferred**, "only if a
genuinely different engine becomes a source, and even then we'd borrow rather than build."

v1 does the opposite on both counts: it brings the deferred item into scope as M5, and it builds
the three-tier comparison in-house (`07`). The reasoning for building is sound as far as it goes —
we need per-column attribution against a *declared transform set*, and a general diff tool does
not know about `transforms.yaml` — but it is a reversal of a recorded position on the most
expensive component in v1, and `../SCOPE.md` §13 independently recommends borrowing here
("reimplementing hierarchical hashing across dialects is weeks of subtle work").

**This deviation was not previously written down.** It is tracked as **Q0e in `16`** rather than
being treated as settled, because unlike D1–D4 it has not been argued anywhere and it is the one
whose reversal would visibly shrink the plan.

### D3 — Migration-shaped semantics

Because the pipeline is a migration, Lane B's default relationship is **exact match on business
columns**, not "filtered subset." Deliberate divergences (a column dropped, a type widened, rows
filtered at cutover) are declared in config as **transform rules**, reviewed once, and bound to a
commit SHA so they expire when the transform changes.

### D4 — The product is a web app. There is no CLI

The design doc specifies "Typer CLI — cron, hooks, CI, and orchestrators can all call it
directly" plus "Streamlit for v0; FastAPI + React later." v1 goes straight to **FastAPI + React,
and drops the user-facing CLI entirely.**

Why the reordering is right for *this* product rather than being gold-plating:

- **The review queue is the product's critical path.** Onboarding depends on a domain expert
  confirming a grain, a dedup key, and a pick rule (assumptions B2, B3, C3). That person is not
  someone who will be talked through `dphm onboard --resume <thread_id>` in a terminal. Two
  buttons behind a link is the difference between getting those 30 minutes and not.
- **The honest-coverage story needs a surface.** "Here is what we did *not* check" is a panel,
  next to the green ticks, that nobody can collapse. In a CLI it scrolls past.
- **Sign-off must be attributable to a real person.** SSO gives us that for free;
  `--by someone@acme.com` on a shared runner does not.
- Streamlit was chosen when the dashboard was a thin read-only viewer. Once the dashboard is the
  *entire* surface — forms, approvals, RBAC, live progress — Streamlit becomes the wrong tool and
  a rewrite we'd have to do anyway.

What does **not** change: the runner is still one stateless service, still one Docker image, and
machines still trigger it without a human — but through **signed webhooks and an internal
scheduler** rather than a shell command. The post-load hook is a one-line `curl` in the load job:
a machine calling a machine. Full surface in `12`.

**Cost of this call:** more code than a CLI, and an auth/hosting dependency (Q21–Q23 in `16`).
It also means M3 cannot ship without a usable UI, which is why the build plan pulls a thin
vertical slice of the app forward into M1.

## 3. Assumption register for v1

Bold rows must be confirmed **before M0** — they are tracked in `16`.

| # | Assumption | If wrong |
|---|---|---|
| **A1** | We get read-only roles on both RDS and Snowflake, plus write on two Snowflake schemas | Nothing works without the read roles; without write schemas, state moves to a new database and adds a security review to the critical path |
| A2 | Lane A is single-engine (Snowflake). Cross-engine is confined to Lane B | Already handled — see D2 |
| **A3** | Pipeline code is in git and readable | We lose commit-bound sign-offs, ownership resolution, and staleness detection. Authoring degrades to schema-only |
| **A4** | There is a reliable "batch finished loading" signal (a control table, an audit column, or a post-run hook) | Row scoping falls back to wall-clock guessing; scheduled checks read mid-load and report false failures |
| A5 | `SNOWFLAKE.ACCOUNT_USAGE` is readable and the Anthropic API is reachable from the runner | Root-cause analysis loses its best signals; authoring falls back to templates alone |
| A6 | No orchestrator is assumed | No downside. If Airflow exists it becomes a hook point and a lineage signal |
| B1 | Audit/ETL columns are identifiable by naming convention or declaration | Every row looks changed — the single most common cause of a useless first run |
| **B2** | Migrated dimensions have a recognisable SCD2 shape with a business key distinct from the surrogate key | The SCD suite cannot be generated automatically. Most common misclassification; requires human confirmation |
| **B3** | Grain is inferable from code + schema, with a human confirming | A wrong grain yields a check that passes while comparing nothing |
| B4 | Some RDS↔Snowflake differences are deliberate and undocumented | Safe to assume. Shadow mode exists to surface them before go-live |
| B5 | RDS can absorb Lane B's read load during a defined window | Lane B moves to a read replica, or to off-hours only |
| C1 | Each pipeline has a named owner who will act on Lane A reports | Lane A is noise; sign-offs get rubber-stamped to keep the dashboard green |
| C2 | We can add a **non-blocking** PR check to the pipeline repo | We lose intent capture at the moment the author still remembers why |
| **C3** | 30–60 minutes of domain-expert time per project, plus a two-week shadow period | Onboarding ships with the wrong grain, or goes live into a noisy baseline |

## 4. Type and semantic mismatches we must handle (RDS → Snowflake)

The canonicalization layer in `07` exists because of these. Decided once, in config, not per check.

| Concern | PostgreSQL | Snowflake | v1 policy |
|---|---|---|---|
| Identifier case | folds to lower | folds to UPPER | Compare on a normalized upper-case name; store both |
| `numeric` scale | arbitrary precision | `NUMBER(p,s)` | Cast both to a declared `NUMBER(38,s)`; `s` per column, default 6 |
| Float | `double precision` | `FLOAT` | Compare with absolute+relative tolerance, never `=` |
| Timestamps | `timestamptz` / `timestamp` | `TIMESTAMP_NTZ` / `_TZ` | Convert both to UTC `TIMESTAMP_NTZ` at second granularity unless the column declares finer |
| Boolean | native | native | Direct |
| Text | unbounded | `VARCHAR` | Compare exact; optional `TRIM` / case-fold per column, declared |
| JSON | `json`/`jsonb` | `VARIANT` | Compare canonical serialization (sorted keys, no whitespace) |
| UUID | native | `VARCHAR(36)` | Lower-case string form both sides |
| NULL vs empty string | distinct | distinct | `IS DISTINCT FROM` semantics everywhere; policy declared per column |
| Collation / sort | locale-dependent | UTF-8 binary | Never rely on ordering for comparison; always key-join |
