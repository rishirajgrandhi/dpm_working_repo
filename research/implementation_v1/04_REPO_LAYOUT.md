# 04 — Repository Layout

## 1. Tree

```
dphm/
├── pyproject.toml                 # packaging, deps, tool config (ruff, mypy, pytest, import-linter)
├── Dockerfile                     # multi-stage: build the SPA, then serve it from the API container
├── README.md
├── projects/                      # per-project onboarding config (the ~50 lines)
│   └── orders/
│       ├── project.yaml           # connections, repo, ownership, defaults
│       ├── manifest.yaml          # tables, classifications, grain (human-confirmed)
│       ├── layers.yaml            # medallion hops: source→bronze→silver→gold, dedup contract (07B)
│       ├── column_rules.yaml      # four-way classification + overrides
│       ├── transforms.yaml        # declared RDS→Snowflake divergences (Lane B)
│       └── checks/                # generated + hand-written check definitions
│           ├── scd/dim_customer.yaml
│           ├── layer/l2_customers.yaml
│           ├── keys/fct_order.yaml
│           └── business/margin_nonnegative.yaml
├── src/dphm/
│   ├── __init__.py
│   ├── api/
│   │   ├── main.py                # FastAPI app; the only public interface
│   │   ├── auth.py                # OIDC, session cookie, RBAC dependencies
│   │   ├── routes/
│   │   │   ├── projects.py  runs.py  parity.py  incidents.py  checks.py
│   │   │   ├── coverage.py  layers.py  onboarding.py  review_queue.py
│   │   │   ├── diagnostics.py     # the "doctor" panel
│   │   │   └── hooks.py           # load-complete, GitHub, Slack — signed, machine-only
│   │   ├── schemas.py             # request/response models → OpenAPI → generated TS types
│   │   └── sse.py                 # live run progress
│   ├── runtime/
│   │   ├── scheduler.py           # per-project Lane A cadence
│   │   ├── worker.py              # job pool; executes runs, parity, agent graphs
│   │   ├── jobs.py                # durable job rows in DPHM_STATE.JOBS
│   │   └── locks.py               # one Lane A run per project
│   ├── config/
│   │   ├── models.py              # Pydantic v2: Project, Manifest, ColumnRules, CheckDef, Transforms
│   │   ├── loader.py              # YAML → models, env interpolation, cross-file validation
│   │   └── defaults.py            # audit-column patterns, tolerances, scale defaults
│   ├── catalog/
│   │   ├── base.py                # TableMeta, ColumnMeta, CatalogReader protocol
│   │   ├── snowflake.py           # INFORMATION_SCHEMA + ACCOUNT_USAGE reader
│   │   └── postgres.py            # information_schema + pg_catalog reader
│   ├── repo/
│   │   ├── clone.py               # shallow clone / refresh, commit SHA resolution
│   │   ├── sql_parse.py           # sqlglot: tables, joins, group-by → grain candidates
│   │   ├── dedup_extract.py       # QUALIFY / ROW_NUMBER / DISTINCT ON → dedup key + pick rule (07B §6.1)
│   │   ├── py_parse.py            # ast/libcst: pandas + SQL-string extraction
│   │   ├── ownership.py           # CODEOWNERS, git blame → team + likely author
│   │   └── history.py             # git log windows for evidence
│   ├── engine/
│   │   ├── dialect/
│   │   │   ├── base.py            # Dialect protocol: quoting, casting, hashing, tolerance
│   │   │   ├── snowflake.py
│   │   │   └── postgres.py
│   │   ├── templates/             # Jinja2 .sql.j2 — the reviewed, versioned check bodies
│   │   │   ├── scd/ (11 files)
│   │   │   ├── layer/             # medallion hop checks: L1/L2 dedup/L3 (07B)
│   │   │   ├── keys/  ref/  domain/  cast/  conservation/  fanout/
│   │   │   └── parity/
│   │   ├── hops.py                # HOP_CHECK_BUNDLES: relation → template list; expand_hop()
│   │   ├── layer_graph.py         # the medallion DAG; "earliest failing layer" resolution
│   │   ├── render.py              # params + template → SQL, with a rendered-SQL hash
│   │   ├── scope.py               # completed-batch predicate resolution
│   │   ├── columns.py             # four-way classification, exclusion set, comparison projection
│   │   ├── execute.py             # bounded-concurrency execution, timeouts, row caps
│   │   └── result.py              # CheckResult model incl. compared/excluded columns
│   ├── parity/
│   │   ├── canonical.py           # per-type canonicalization SQL for both engines
│   │   ├── tier0_aggregate.py     # counts, sums, min/max, bucket fingerprints
│   │   ├── tier1_bucket.py        # narrowing within disagreeing buckets
│   │   ├── tier2_land.py          # land suspect key ranges into DPHM_SCRATCH
│   │   ├── diff.py                # full-outer-join diff SQL, column attribution
│   │   └── budget.py              # cost accounting and abort switch
│   ├── state/
│   │   ├── ddl.sql                # DPHM_STATE schema (see 08)
│   │   ├── migrate.py             # forward-only numbered migrations
│   │   ├── repo.py                # typed read/write API over the state tables
│   │   ├── incidents.py           # fingerprinting, dedup, lifecycle
│   │   ├── signoff.py             # commit-bound sign-offs and expiry
│   │   └── permissions.py         # grant fingerprint at startup
│   ├── evidence/
│   │   ├── collect.py             # orchestrates the collectors below
│   │   ├── schema_diff.py  load_history.py  git_history.py  prior_incidents.py
│   │   └── redact.py              # PII redaction before anything leaves the perimeter
│   ├── agents/
│   │   ├── llm.py                 # ChatAnthropic factory: model, temp=0, retries, cost callback
│   │   ├── prompts/               # versioned .md prompts; SHA recorded per run
│   │   ├── schemas.py             # Pydantic output models for every LLM node
│   │   ├── authoring/
│   │   │   ├── graph.py           # StateGraph wiring
│   │   │   ├── state.py           # AuthoringState TypedDict
│   │   │   ├── nodes.py           # discover, classify_tables, classify_columns, propose, generate
│   │   │   ├── layer_nodes.py     # infer_layer_map, infer_dedup_contract, classify_losses,
│   │   │   │                      #   infer_measure_map (07B §6.2)
│   │   │   ├── hitl.py            # interrupt() for grain confirmation
│   │   │   └── gates.py           # the four validation gates as graph nodes
│   │   └── reporting/
│   │       ├── graph.py  state.py  nodes.py   # classify, group, route, narrate
│   ├── reporting/
│   │   ├── slack.py               # Block Kit, buttons, thread updates
│   │   ├── jira.py                # REST v3, create/update/transition
│   │   ├── github.py              # PR check + comment
│   │   └── render.py              # one incident model → all channels
│   └── util/
│       ├── logging.py             # structlog JSON, run_id in every line
│       ├── secrets.py             # env-var backed, platform secret manager
│       └── ids.py                 # deterministic check_id / incident fingerprint hashing
├── web/                           # React SPA — built and served by the same container
│   ├── src/
│   │   ├── routes/                # Overview · Pipeline · Incidents · ReviewQueue ·
│   │   │                          #   Coverage · Checks · Runs · Parity · Onboarding · Settings
│   │   ├── components/            # MedallionGraph, CoveragePanel, EvidenceTimeline,
│   │   │                          #   GateBadges, ContractCard, ResultTable (virtualized)
│   │   ├── api/                   # generated client + types from the OpenAPI schema
│   │   └── lib/                   # formatting only — NO verdict or threshold logic
│   ├── index.html  vite.config.ts  tailwind.config.ts
│   └── tests/                     # component tests + Playwright e2e
└── tests/
    ├── unit/                      # config, columns, scope, dialect, fingerprints
    ├── sql_snapshots/             # rendered SQL golden files per template + params
    ├── mutation/                  # bug injection into DPHM_SCRATCH; gate 3
    ├── integration/               # docker-compose postgres + Snowflake test schema
    └── agents/                    # graph tests with a recorded/stub LLM
```

## 2. Module dependency rules (enforced in CI by `import-linter`)

```
web (TypeScript) ──HTTP──▶ api ──▶ runtime ──▶ agents ──▶ evidence ──▶ state ──▶ catalog/repo
                            │         │           │                      ▲
                            └─────────┴──▶ engine ┴──▶ parity ───────────┘
                                             │
                                             └──▶ config, util   (leaf layers, import nothing above)
```

Hard rules:

1. `langchain*` and `langgraph*` may be imported **only** under `src/dphm/agents/`.
2. `engine/`, `parity/`, `state/`, `catalog/`, `repo/` import no agent code and no reporters.
3. `reporting/` imports models only — it never queries a warehouse.
4. `api/` may not import `engine/`, `parity/` or `agents/` directly — it submits jobs through
   `runtime/`. This keeps request handlers from running warehouse queries inline.
5. `web/` is a separate build with no Python imports at all, and its API types are **generated**
   from the OpenAPI schema so the two cannot drift silently.
6. No module outside `state/` and `parity/tier2_land.py` may use the `DPHM_WRITER` role.

Rule 6 is asserted at runtime too: the connection factory returns a reader connection by default
and raises unless a writer connection is requested from an allow-listed module path.

## 3. Dependencies

| Purpose | Package | Note |
|---|---|---|
| Config | `pydantic>=2.7`, `pyyaml` | Config is the product; validated at load |
| API | `fastapi`, `uvicorn`, `pydantic-settings` | The only public interface; OpenAPI generates the frontend's types |
| Auth | `authlib` (OIDC) , `itsdangerous` | SSO; no local accounts |
| Scheduling | `apscheduler` | In-service cadence; no crontab to maintain |
| Snowflake | `snowflake-connector-python` | Native driver, no ORM |
| Postgres | `psycopg[binary]>=3` | `psycopg2` if the platform requires it |
| SQL parsing / generation | `sqlglot` | Dialect-aware parsing and transpilation |
| Templating | `jinja2` | Templates read like reviewable SQL |
| Git | `GitPython` | Clone, log, blame |
| Agents | `langgraph`, `langchain-core`, `langchain-anthropic` | Pinned; see below |
| Checkpointing | `langgraph-checkpoint-sqlite` | Local resumable onboarding runs |
| Reporting | `slack-sdk`, `httpx` (Jira/GitHub REST) | |
| Frontend | React 18, TypeScript, Vite, TanStack Query, Tailwind, Radix, Recharts | The entire user surface (`12`) |
| Logging | `structlog` | JSON, run-scoped |
| Testing | `pytest`, `pytest-snapshot`, `testcontainers` | |
| Lint / types | `ruff`, `mypy --strict`, `import-linter` | |

**Pinning policy:** `langchain*` and `langgraph` are pinned to exact versions and upgraded
deliberately, in their own PR, with the agent test suite as the gate. They are the fastest-moving
dependency in the stack and the one furthest from the product's core guarantees.

## 4. Configuration precedence

`API request parameter` ▸ `env var (DPHM_*)` ▸ `projects/<name>/*.yaml` ▸ `config/defaults.py`

Secrets come only from env vars backed by the platform secret manager. A secret literal appearing
in a YAML file is a load-time error, not a warning.
