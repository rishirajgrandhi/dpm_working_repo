# Handover

You are picking up a **Data Pipeline Health Monitor**: a tool that watches an
AWS RDS → Snowflake migration and reports what is wrong with the data, and — just as
importantly — what it could not check.

This document is for your first day. Read it before the specs.

---

## 1. What problem this solves

A team migrates `DIM_CUSTOMER` into Snowflake as a slowly-changing dimension: when a
customer's segment changes, the old row is closed and a new one opened. The history is
the point.

One Tuesday someone simplifies the merge statement. From then on the dimension
**overwrites** instead of versioning. History stops accumulating and silently
disappears.

**Every existing check stays green.** Row counts match — one row per customer, same as
always. The target matches the source; the current values are perfectly correct.
Freshness is fine. The pipeline's own tests pass.

Six months later an analyst notices a cohort report gives a different answer than it did
in March, and the history needed to explain it is the history that got destroyed.

That failure is the product's reason to exist, and it shapes every design decision here.
When something in this codebase looks over-careful, it is usually because of that story.

## 2. Get it running (20 minutes)

Two prerequisites are unusual and worth knowing up front.

```bash
# Python 3.11+ is required. If your system Python is older, do NOT fight it:
pip install uv && uv python install 3.11
uv venv --python 3.11 .venv311
VIRTUAL_ENV=.venv311 uv pip install -e ".[dev]"

# Verify without touching any warehouse
.venv311/bin/python -m pytest tests -q       # 250 tests, ~2s
.venv311/bin/mypy                            # strict
.venv311/bin/lint-imports                     # 6 contracts — read §5 before changing these
```

**No Docker or local PostgreSQL needed.** The integration tests boot a real PostgreSQL
through the `pgserver` package, which bundles the binary. `pytest tests -m integration`
just works.

### Snowflake

You need your own credentials — the ones on the previous machine are not transferable
and should not be. See §7.

```bash
source ~/.dphm/credentials.env            # after you create it, per §7
.venv311/bin/python scripts/verify_snowflake.py   # 6 checks, tells you what is wrong
.venv311/bin/python scripts/seed_fixture.py       # build the test medallion
```

### The app

```bash
source ~/.dphm/credentials.env
.venv311/bin/uvicorn dphm.api.main:app --port 8000 &
npm install --prefix web && npm run dev --prefix web
```

Open the Vite URL. Five tabs. Press **Run checks now**; a run takes ~145s against an
XSMALL warehouse.

## 3. The shape of it, in one page

```
config/      YAML -> Pydantic. Validated at LOAD, never at run.
   |
warehouse/   Connections. Owns the writer allowlist and the destructive-statement guard.
   |
catalog/     Snowflake + Postgres metadata behind one protocol.
   |          Identifier case is normalised HERE, once. (RDS folds lower, Snowflake UPPER.)
engine/      The part that matters:
   |           templates/  23 reviewed .sql.j2 files — the checks themselves
   |           columns.py  four-way classification: key | business | audit | derived
   |           scope.py    which rows are safe to read
   |           render.py   template + params -> SQL + a hash of that SQL
   |           execute.py  run it -> a four-state result
   |           expand.py   config -> concrete parameterised checks
   |           hops.py     relation -> template list. This dict IS "the agent generates
   |                       the tests" — the agent picks a relation and fills parameters.
state/       DPHM_STATE, 16 tables. Forward-only migrations.
runtime/     Orchestrates a run, in hop order (bronze -> silver -> gold).
api/         FastAPI, four roles, 7 endpoints. Submits jobs; never queries inline.
web/         React. Renders what the API returns and computes NOTHING.
```

### The four result states, and why there are four

`PASS` · `FAIL` · `INCONCLUSIVE` (we tried and could not tell) · `UNVALIDATED` (we have
no way to check this yet).

**"We couldn't tell" and "everything's fine" must never share a status.** Coverage counts
the last two as *not covered*. If you find yourself wanting to collapse these, re-read §1.

## 4. What is done, and what is not

| Milestone | State |
|---|---|
| M0 foundations | done |
| M1 check engine | done |
| M2 the 11-check SCD suite | done |
| M2B layer parity — dedup contract, facts, marts | done |
| **M3 reporting — incidents, Slack, Jira** | **not started — start here** |
| M4 reporting agent (LangGraph) | not started |
| M5 Lane B parity (RDS ↔ Snowflake) | not started |
| M6 authoring agent (LangGraph) | not started |

Last run against the live fixture: **36 pass, 0 fail, 1 inconclusive** in 145s. The
inconclusive one is `grain_unique` on an empty table — zero violations over zero rows is
an abstention, and the engine says so rather than reporting green.

### The one thing I would do first

**Build the four validation gates** (`research/implementation_v1/10_VALIDATION_GATES.md`).

No check is activatable today, and that is correct rather than an oversight: activation
requires all four gates and none exist yet.

This is not theoretical. Four templates written during the initial build were broken in
ways that read perfectly and failed only against a real warehouse:

- a stripped newline welded two clauses together (`... = b.CUSTOMER_IDwhere 1=1`)
- the compare list fell back to the dedup key, so pick-rule fidelity compared keys to
  keys and asserted nothing
- the immutability check had no recorded baseline, so it flagged every closed version
- a reject-table anti-join correlated to the inner table, so `NOT EXISTS` excluded every
  row

**Gate 2 alone would have caught all four** — *does it pass on data we believe is good,
and did it actually evaluate any rows*. Gate 3 then proves each check can fail, by
cloning the table, injecting the exact defect, and confirming the right violation code on
the right row.

Until the gates exist, the 37 checks are recorded but unproven. That is the honest state,
and clearing it is the highest-value work available.

## 5. Rules that are load-bearing

Each of these looks like an inconvenience and is not. They are enforced by tests or CI,
so you will meet them rather than read about them.

**1. Everything that decides pass/fail is deterministic SQL.** The two LangGraph agents
(unbuilt) author candidates and explain failures. Neither can decide a verdict. The
`.importlinter` contract forbidding `langchain` outside `agents/` is what keeps this true
as the codebase grows.

**2. The tool reports on data; it never changes it.** No writes to RDS at all — eight
tests fire `INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`/`CREATE`/`DROP`/`ALTER` at a live
connection and assert every one is refused. In Snowflake it writes only to `DPHM_STATE`
and `DPHM_SCRATCH`, enforced by grant *and* by an allowlist in `warehouse/connection.py`.

**3. Every result records what it compared and what it excluded.** `COLUMNS_COMPARED` and
`COLUMNS_EXCLUDED` are `NOT NULL` in the schema, and `CheckResult.__post_init__` raises on
a `PASS` without them. A green result without its exclusions is a lie of omission.

**4. We never guess.** If the completed-batch boundary cannot be resolved, checks return
`INCONCLUSIVE`. Falling back to wall-clock would produce false failures, and false
failures are how a monitoring tool loses its audience.

**5. A check that can never fail is worse than no check.** Hence the gates. Hence
`min_rows_to_evaluate`: a check returning no violations because it evaluated no rows has
abstained.

**6. Grain needs a human.** A wrong grain produces a check that *passes* while comparing
nothing — the most dangerous silent failure. Same for the dedup key and the mart group-by.
Config validators refuse to activate without a recorded name.

## 6. Traps, so you do not rediscover them

Every one of these cost real time.

| Trap | What happens |
|---|---|
| **Snowflake secondary roles** | New users default to `DEFAULT_SECONDARY_ROLES=('ALL')`, making *every* granted role active in *every* session regardless of primary role. "Checks execute as the reader" was simply false until this was disabled. Check `current_secondary_roles()`, do not trust `current_role()`. |
| **`SHOW GRANTS TO ROLE` is not an audit** | It lists only DIRECT grants. Every Snowflake role inherits `PUBLIC`, whose grants accumulate. 659 direct vs **774 effective**, and the hidden ones included a `CREATE SCHEMA` privilege. `state/permissions.py` walks the hierarchy. |
| **Ownership beats grants** | A role that *created* a table owns it, and ownership cannot be revoked by revoking privileges. If a role can still write after you revoke everything, check ownership. |
| **Jinja `trim_blocks`** | Strips the newline after a block tag, welding a line ending in `{% endfor %}` to the next clause. It is off, and a test renders *and parses* every template to keep it off. |
| **Correlated subqueries need an alias** | `where r.X = X` binds the bare column to the INNER table. Templates taking `loss_filter_predicate` alias their source `src`; a test enforces it. |
| **`TRIGGER` is reserved in Snowflake** | The column is `TRIGGER_KIND`. |
| **One connection per query is fatally slow** | 3-5s each. A run went from minutes to 145s by holding one reader session and one writer session. |
| **The diagnostics grant audit is slow** | 44s of a 59s page load. It is now opt-in via `?deep=true`; the panel loads in 3s without it. |

## 7. Credentials — you need your own

The previous machine's key is not transferable and should not be shared. Set up your own:

```bash
mkdir -p ~/.dphm/keys && chmod 700 ~/.dphm ~/.dphm/keys && cd ~/.dphm/keys
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out dphm_dev_key.p8 -nocrypt
openssl rsa -in dphm_dev_key.p8 -pubout -out dphm_dev_key.pub
chmod 600 dphm_dev_key.p8
```

Then have whoever holds `ACCOUNTADMIN` register the public key:

```sql
alter user DPHM_DEV_SVC set rsa_public_key = '<contents of .pub, header/footer removed>';
```

Copy `.env.example` to `~/.dphm/credentials.env` (mode 600, **outside the repo**) and fill
it in. `config/loader.py` refuses to load any YAML containing a credential literal, so
there is no path where a secret ends up committed.

The dev Snowflake environment is `DPHM_TEST` — a throwaway database with a 10-credit
monthly cap. `docs/DEV_ENVIRONMENT.md` explains it; `scripts/provision_dev_roles.py`
rebuilds the roles. **Two access requests exist and must not be conflated**:
`docs/PROVISIONING.md` is read-only production access; `docs/DEV_ENVIRONMENT.md` is full
DDL on a sandbox. Never grant the second against a production database.

## 8. Still blocking, and not ours to answer

From `research/implementation_v1/16_OPEN_QUESTIONS.md`:

- **A real source database.** Everything so far runs against a fixture. Lane B (the
  RDS ↔ Snowflake comparison) cannot be built or tested without one.
- **The "batch finished loading" signal.** Without it, scheduled checks either read
  mid-load or stay `INCONCLUSIVE`. The doc's own note: a vague answer here is worse than
  a "no".
- **A named person who will act on the reports.** The specs call this the one risk not
  handled structurally. Fourteen of twenty risks are solved by a design decision; this one
  needs a willing human, and reporting should not go live without one.
- **Slack workspace + bot token, and a Jira project**, before M3 can do anything visible.

## 9. Where the specs are

`research/implementation_v1/` — 21 documents. Read in this order:

1. `PRODUCT.md` — the whole product in one read. Genuinely start here.
2. `03_ARCHITECTURE.md` and `04_REPO_LAYOUT.md` — components and structure.
3. `07B_LAYER_PARITY_MEDALLION.md` — the longest and best of them. The dedup contract.
4. `10_VALIDATION_GATES.md` — the sharpest. Why the checks can be trusted.
5. `15_BUILD_PLAN.md` — milestones. `16_OPEN_QUESTIONS.md` — what is still unanswered.

**`DOC_CHANGELOG.md` records every correction made to those specs**, with reasoning —
including contradictions between them, a decision that had been reversed without being
written down, and a success metric that was dropped while its data was still being
collected. Read it before treating any spec sentence as final.

`research/PLAN.md` is marked **superseded** and two of its sections describe behaviour the
current design forbids. `research/PARITY_SCOPE.md` and `SCOPE.md` are opportunity-space
research, not requirements — and `PARITY_SCOPE.md` describes a **different environment**
(a legacy Redshift estate, API sources) that was confirmed out of scope. Do not build from
either.

`research/unnamed.png` is a diagram for an unrelated project. Ignore it.
