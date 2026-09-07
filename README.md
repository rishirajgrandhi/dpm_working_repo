# Data Pipeline Health Monitor (dphm)

Monitors an **AWS RDS → Snowflake** migration. Reports on data; never changes it.

The specification lives in [`research/implementation_v1/`](research/implementation_v1/00_README.md)
— start with [`PRODUCT.md`](research/implementation_v1/PRODUCT.md). This README covers
only how to run what exists.

**New to this repo? Read [HANDOVER.md](HANDOVER.md) first.** It covers the problem in one
page, a verified 20-minute setup, the rules that are load-bearing and why, and the traps
that cost real time.

## Status

Running against a live Snowflake account with a seeded medallion fixture.

| Milestone | State |
|---|---|
| M0 foundations — config, state store, API, web shell | **done** |
| M1 check engine — render, scope, columns, execute | **done** |
| M2 the SCD suite — 11 checks per dimension | **done** |
| M2B layer parity — L2 dedup contract, L3 facts and marts | **done** |
| M3 reporting — incidents, Slack, Jira | not started |
| M4 reporting agent | not started |
| M5 Lane B parity (RDS ↔ Snowflake) | not started |
| M6 authoring agent | not started |

Last run against the live fixture: **36 pass, 0 fail, 1 inconclusive** in 145s.
The inconclusive one is `grain_unique` on an empty table — zero violations over zero
rows is an abstention, not a pass.

### The gap that matters

**No check is activatable, and that is correct.** Activation requires all four
[validation gates](research/implementation_v1/10_VALIDATION_GATES.md), and the gates are
not built yet. Until a check has been *proven able to fail*, switching it on would
manufacture confidence.

This is not hypothetical. Several templates written today were broken in ways that read
correctly and failed only against a real warehouse: a stripped newline welding two
clauses together, a compare list that compared keys to keys, a hash check with no
recorded baseline, a subquery correlating to the wrong scope. Gate 2 alone — *does it
pass on good data, **and did it evaluate any rows*** — would have caught every one.

## What is built

```
src/dphm/
  config/      Pydantic models + loader. Validation at load, not at run.
               A credential literal in a YAML file is a load-time error.
  warehouse/   Connections + the DPHM_WRITER allowlist + the destructive-statement guard.
  catalog/     Snowflake and Postgres metadata behind one protocol.
               Identifier case is normalised here, once.
  engine/      23 reviewed SQL templates, column classification, batch scoping,
               rendering with a SQL hash, execution into a four-state result.
  state/       DPHM_STATE: 16 tables, forward-only migrations.
  runtime/     Lane A run orchestration, in hop order.
  api/         FastAPI, four roles, 7 endpoints.
  web/         React SPA: overview, results, coverage, checks, settings.
```

**23 SQL templates**: the 11-check SCD suite (including `tracked_change_created_version`,
the one the product exists for), the L2 dedup contract with pick-rule fidelity and
totality, conservation with named losses, as-of foreign keys, and mart recomputation.

Everything that decides pass/fail is deterministic SQL. No model is in that path.

## Seeing it

```bash
source ~/.dphm/credentials.env
.venv311/bin/uvicorn dphm.api.main:app --port 8000 &
npm run dev --prefix web
```

Open the Vite URL. Five tabs: **Overview** (tiles, medallion, "what we are not checking"),
**Results** (click a row for compared/excluded columns and the scope predicate),
**Coverage**, **Checks** (gate badges; Activate disabled with the reason), **Settings**
(connection health). "Run checks now" executes against the fixture.

## Access: two separate requests

| Document | Grants | For |
|---|---|---|
| [docs/PROVISIONING.md](docs/PROVISIONING.md) | **Read-only.** Cannot write to RDS; in Snowflake writes only to `DPHM_STATE` and `DPHM_SCRATCH` | What the finished tool uses against your real pipeline |
| [docs/DEV_ENVIRONMENT.md](docs/DEV_ENVIRONMENT.md) | **Full DDL on a throwaway `DPHM_TEST` database** | Building and testing: seeding fixtures, and the mutation tests that corrupt data on purpose |

Never grant the second against a production database. The fixtures drop and recreate
everything they touch.

## Requirements

- **Python 3.11+** (the codebase uses 3.10+ typing syntax). No system Python? `uv python
  install 3.11` fetches a standalone toolchain.
- Node 22+ for the web build
- **No Docker or local PostgreSQL needed for tests.** The integration suite boots a real
  PostgreSQL via the `pgserver` package.

## Local development

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# The UI, with no warehouse attached: dev auth + skipped migrations.
# Both flags refuse to activate when DPHM_ENV=production.
export DPHM_DEV_AUTH=1 DPHM_SKIP_MIGRATIONS=1
uvicorn dphm.api.main:app --reload

# In another shell
npm install --prefix web && npm run dev --prefix web
```

Then open the Vite URL. The Settings screen is the M0 deliverable.

## Verification

```bash
ruff check src tests          # lint
ruff format --check src tests # formatting
mypy                          # --strict, configured in pyproject.toml
lint-imports                  # the module dependency contracts (04 §2)
pytest tests -q               # 93 unit tests
```

`lint-imports` is the one worth understanding. It enforces that **`langchain` and
`langgraph` may only be imported under `agents/`** — the trust boundary between the
deterministic core and the LLM. Everything that decides pass/fail is deterministic SQL,
and this contract is what keeps it that way as the codebase grows.

## Environment variables

Secrets come **only** from env vars backed by the platform secret manager. A secret
literal in a YAML file is a load-time error, not a warning
([13 §3](research/implementation_v1/13_SECURITY_PII_COST.md)).

| Variable | Purpose |
|---|---|
| `DPHM_SF_ACCOUNT`, `DPHM_SF_USER`, `DPHM_SF_KEY_PATH` | Snowflake (key-pair auth; no passwords) |
| `DPHM_RDS_HOST`, `DPHM_RDS_USER`, `DPHM_RDS_PASSWORD` | RDS, read-only |
| `DPHM_GIT_TOKEN` | Pipeline repo, read-only |
| `ANTHROPIC_API_KEY` | Optional. Absent → runs still execute, incidents get `UNCLASSIFIED` |
| `DPHM_ROLE_MAP` | `group:role` pairs, e.g. `grp-owners:data_owner` |
| `DPHM_SESSION_SECRET` | Session cookie signing |
| `DPHM_PROJECTS_DIR` | Default `projects/` |
| `DPHM_ENV` | `production` disables every dev bypass |

Config precedence: API parameter ▸ `DPHM_*` env var ▸ `projects/<name>/*.yaml` ▸ defaults.

## Deployment

One Docker image: the SPA is built and served by the same container.

```bash
docker build -t dphm:local .
```

Migrations run at startup and **the service refuses to serve if they fail** — a
half-migrated state store produces plausible-looking wrong answers.

## Before this can run for real

Four questions block sign-off, tracked in
[`16_OPEN_QUESTIONS.md`](research/implementation_v1/16_OPEN_QUESTIONS.md):

- **Q1** — `DPHM_READER` / `DPHM_WRITER` on Snowflake. Without these, nothing works.
- **Q6** — the reliable "batch finished loading" signal. Without it, checks return
  `INCONCLUSIVE` rather than guessing, because a false failure is how a monitoring tool
  loses its audience.
- **Q7** — a named real SCD2 dimension for the M2 pilot.
- **Q10** — a named person who will act on Lane A reports. This is risk R8, the one risk
  the design doc admits is not handled structurally.
