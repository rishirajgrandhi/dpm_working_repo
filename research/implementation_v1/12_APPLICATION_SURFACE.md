# 12 — Application Surface (Web App, API, Runtime)

**There is no user-facing CLI.** Every human action happens in the web app. The only non-UI
entrypoints are machine-to-machine: a scheduler inside the service, and signed webhooks from the
load pipeline and from GitHub. Nobody types a command to use this product.

## 1. Shape of the system

```
   ┌─────────────────────────────────────────────────────────┐
   │  BROWSER — React SPA                                     │
   │  Overview · Pipeline · Incidents · Review Queue ·         │
   │  Coverage · Checks · Runs & Cost · Parity · Settings      │
   └──────────────────┬──────────────────────────────────────┘
                      │ HTTPS · JSON · SSE for live run progress
   ┌──────────────────▼──────────────────────────────────────┐
   │  API — FastAPI                                           │
   │  auth (OIDC) · RBAC · validation · job submission        │
   ├──────────────────────────────────────────────────────────┤
   │  SCHEDULER            │  WORKER POOL                      │
   │  owns Lane A cadence  │  executes runs, parity, authoring │
   │  per project          │  graphs; streams progress         │
   ├──────────────────────────────────────────────────────────┤
   │  engine · parity · agents · evidence · state · reporting  │
   └──────────────────┬──────────────────────────────────────┘
                      │
        Snowflake (checks + state) · AWS RDS (read-only) · git · Slack · Jira
```

One deployable service. The React build is served as static assets by the same container, so
there is one artifact, one URL, one thing to deploy.

## 2. Screens

### 2.1 Overview — the landing page

The question it answers: *is this pipeline healthy, and what does "healthy" actually cover?*

```
┌──────────────────────────────────────────────────────────────────────┐
│  orders                                    ● SHADOW MODE  (day 9/14) │
├──────────────────────────────────────────────────────────────────────┤
│  ┌────────────┬────────────┬────────────┬────────────┐               │
│  │ 2 OPEN     │ 41 CHECKS  │ COVERAGE   │ $18 / mo   │               │
│  │ incidents  │ 41 verified│ 13 of 17   │ monitoring │               │
│  └────────────┴────────────┴────────────┴────────────┘               │
│                                                                       │
│  MEDALLION HEALTH                             last run 04:22, 6m ago │
│   RDS ──L1──▶ BRONZE ──L2──▶ SILVER ──L3──▶ GOLD                     │
│        ✓ 4/4      ✓ 4/4         ⚠ 1 open       ✗ 1 open, 2 uncovered │
│                                                                       │
│  WHAT WE ARE NOT CHECKING                                    3 items │
│   · MART_CHURN — no silver counterpart (unvalidated)                 │
│   · MART_DAILY_REVENUE — grain unconfirmed, checks inactive          │
│   · AVG_BASKET, UNIQUE_BUYERS — non-additive, cannot be recomputed   │
└──────────────────────────────────────────────────────────────────────┘
```

The "what we are not checking" panel is **not collapsible and not below the fold**. Coverage never
reaches 100%, and a dashboard that shows only green is the failure this product exists to end.

### 2.2 Pipeline — the medallion view

The hop DAG, rendered live. Click a hop to see its contract, its checks, and its last result;
click a table to see its columns with the four-way classification and which are excluded and why.

This is where the L2 dedup contract lives visually: the dedup key, the pick rule with its ordering
terms, the declared losses, and a **"ties: 12"** badge when the pick rule is not total.

### 2.3 Incidents

List → detail. The detail view is the Slack message with more room:

- The violation, the failing keys, sample rows (PII shown as a hash prefix).
- The **evidence bundle** — commits, schema diffs, load history — as a timeline.
- The AI classification and narrative, in a **visually distinct block labelled AI-generated**,
  with its cited evidence items linked back to the timeline.
- Downstream results **suppressed by** this incident, listed and expandable.
- Actions: Acknowledge · False positive · Snooze · Sign off (with reason, watched paths, expiry).

### 2.4 Review Queue — where the agent's work lands

The single most important screen, and the reason a dashboard beats a CLI for this product.

Everything the authoring agent proposes waits here for a human:

| Item type | What the reviewer sees | What they do |
|---|---|---|
| **Grain confirmation** | Proposed grain, the `GROUP BY`/PK it was inferred from, observed cardinality | Confirm / correct |
| **SCD2 role mapping** | Business key vs surrogate key, tracked vs type-1 columns | Confirm / correct |
| **Dedup contract** | The actual `QUALIFY` clause from the transform, the observed duplicate profile, the proposed pick rule and tiebreaker | Confirm / correct / mark as "ties are an error" |
| **Mart grain + measures** | `group_by`, additive vs non-additive measures, observed group cardinality | Confirm / correct |
| **Declared loss** | A `WHERE` clause the agent thinks is a business filter | Confirm as filter / flag as bug |
| **Generated checks** | The rendered SQL, the four gate results, the mutation injector that proved it can fail | Approve → opens a PR |

Each item shows **the evidence it was inferred from**, so the expert confirms against data rather
than against a model's assertion. This is the LangGraph `interrupt()` (`09` §1.4) surfaced as a UI
queue: the graph is checkpointed and paused, the reviewer answers days later, the graph resumes.

That is the concrete win over a terminal. The domain expert who owns the grain answer is not a
person who will run `dphm onboard --resume <thread_id>`. They will click two buttons in a link
someone sent them.

### 2.5 Coverage

The per-layer coverage table from `07B` §9, filterable, with a CSV export. Shows checked,
unvalidated, unconfirmed-grain, unconfirmed-contract, and unverified-measure counts per hop.

### 2.6 Checks

The registry. Per check: template, parameters, rendered SQL, the four gate results with timestamps,
run history sparkline, cost, owner, status. Actions (RBAC-gated): re-validate, activate, retire,
snooze.

**Activate is disabled with a tooltip** unless all four gates pass — the same rule as the old CLI
refusal, but visible before the click rather than after it.

### 2.7 Runs & Cost

Every run, its status, duration, warehouse seconds, USD, and the scope predicate it used. Drill
into a run to see per-check results. Cost is charted per project per month against the budget.

### 2.8 Parity (Lane B, on demand)

A form, not a command: pick tables, set a budget (pre-filled from config, can only be lowered),
run. Progress streams live via SSE — tier 0 aggregate comparison, bucket narrowing, landing,
diff — so the user can watch an expensive operation and abort it.

Results show differing rows and the causing column, the tiers reached, cost spent against budget,
and `COMPLETE` vs `PARTIAL`.

### 2.9 Onboarding wizard

Five steps, each resumable: connections → repo → column rules → run discovery → review queue.
Ends with a PR containing `manifest.yaml`, `layers.yaml`, and the generated checks.

### 2.10 Settings

Connections with a **live health check** (the old `doctor`, as a panel): RDS reachable, Snowflake
roles and grants, state schema migration level, git reachable, LLM reachable. Green ticks or a
precise error. Also: ownership, channels, budgets, go-live toggle, and the permission-fingerprint
history.

## 3. API surface

`/api/v1`, JSON, OIDC-authenticated. The SPA is the only intended consumer, but the API is
documented (FastAPI generates OpenAPI) because orchestrators may want to trigger runs.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/projects` · `/projects/{p}` | Project list and detail |
| `GET` | `/projects/{p}/health` | Overview payload |
| `GET` | `/projects/{p}/layers` | Medallion DAG with per-hop status and contracts |
| `POST` | `/projects/{p}/runs` | Submit a Lane A run (optional `batch_id`, `hop`, `tags`) |
| `GET` | `/runs/{id}` · `/runs/{id}/results` | Run status and results |
| `GET` | `/runs/{id}/events` | **SSE** — live progress |
| `POST` | `/projects/{p}/parity` | Submit a Lane B run |
| `GET` | `/projects/{p}/incidents` · `GET /incidents/{id}` | Incidents |
| `POST` | `/incidents/{id}/ack` · `/false-positive` · `/snooze` · `/resolve` | Incident actions |
| `POST` | `/checks/{id}/signoff` | Sign-off, with reason + watched paths + expiry |
| `GET` | `/projects/{p}/checks` · `POST /checks/{id}/validate` · `/activate` · `/retire` | Check registry |
| `GET` | `/projects/{p}/coverage` | Per-layer coverage |
| `POST` | `/projects/{p}/onboard` | Start an authoring graph run |
| `GET` | `/projects/{p}/review-queue` | Pending agent proposals (paused graph interrupts) |
| `POST` | `/review-queue/{item}/respond` | Answer an interrupt → resumes the graph |
| `GET` | `/projects/{p}/diagnostics` | The doctor panel |
| `POST` | `/hooks/load-complete` | **Signed webhook** from the load pipeline |
| `POST` | `/hooks/github` | **GitHub App webhook** — PR opened / synchronized |
| `POST` | `/hooks/slack` | Slack interactivity (button actions) |

### 3.1 The line between an API write and a git PR

This matters, because the design doc's core safety property is "no AI output goes live without
review, and every AI output lands as a reviewable artifact."

| Action | Where it goes |
|---|---|
| **Structural** — manifest, layer contract, column rules, check definitions, transforms | **A git PR.** The app opens or updates it; a human merges. Never written directly |
| **Operational** — acknowledge, snooze, sign off, activate a check, resolve an incident, trigger a run | **The state store**, immediately, attributed to the signed-in user |

A reviewer clicking "Confirm grain" in the Review Queue does *not* write the manifest. It resumes
the graph, which regenerates the artifacts and opens a PR. The confirmation is recorded with their
identity so `grain_confirmed_by` is a real person, not a service account.

## 4. Triggers — how anything runs without a human

| Trigger | Mechanism |
|---|---|
| **Scheduled Lane A** | An in-service scheduler holds each project's cadence. Configured in Settings, not in crontab |
| **Post-load (preferred)** | The load job `POST`s to `/hooks/load-complete` with its `batch_id` and an HMAC signature. This eliminates the mid-load race rather than mitigating it (risk R2) |
| **PR gate (Lane B)** | A GitHub App webhook on `pull_request`. The app derives the affected tables from the diff, runs a budgeted parity, and posts a comment plus a **neutral** check status |
| **Slack buttons** | `/hooks/slack` — acknowledge, false positive, snooze without opening the app |

The post-load hook is a one-line `curl` in the load job. That is the only place anything
command-shaped survives, and it is a machine calling a machine.

## 5. Runtime and concurrency

- **One container**: FastAPI + scheduler + an in-process worker pool, plus the built SPA.
- Jobs are durable rows in `DPHM_STATE.JOBS`; the in-process pool is the dispatcher.
- **One Lane A run per project at a time**, enforced by an advisory lock row. A duplicate
  submission returns the in-flight run rather than doubling warehouse cost.
- Checks execute in hop order (L1 → L2 → L3) with bounded concurrency (default 4) on a dedicated
  `DPHM_WH` XS warehouse, so monitoring cost is separately attributable and can never starve
  production queries.
- Every query carries `QUERY_TAG = 'dphm:{run_id}:{check_id}'`.
- Statement timeouts on both engines; row caps on every RDS read.

**Known v1 limitation, stated plainly:** the in-process worker pool means one replica. Horizontal
scale-out needs a real broker (Redis or SQS) and is deliberately deferred — a single project on a
single pipeline does not justify the infrastructure, and the job table is already durable, so the
change is a dispatcher swap rather than a redesign.

## 6. Auth and RBAC

SSO via the company OIDC provider. Session in an httpOnly, SameSite=Lax cookie. No local accounts.

| Role | Can |
|---|---|
| **Viewer** | See everything: health, incidents, coverage, checks, cost |
| **Engineer** | Viewer + trigger runs, trigger parity, acknowledge and snooze incidents |
| **Data Owner** | Engineer + **confirm grain / dedup contracts**, sign off on differences, approve generated checks |
| **Admin** | Data Owner + connections, budgets, schedules, go-live toggle |

**Sign-off requires Data Owner, not Engineer.** If the monitoring team can sign off on its own
alerts, the board gets rubber-stamped green — the exact adoption failure the design doc warns
about (risk C1).

Every mutating action is written with the acting user's identity. `grain_confirmed_by` and
`signed_by` are people, and the audit trail says so.

## 7. Frontend implementation

| Concern | Choice |
|---|---|
| Framework | React 18 + TypeScript, Vite |
| Data | TanStack Query — polling for lists, SSE for live run progress |
| Routing | React Router |
| Styling | Tailwind + a small component set (Radix primitives) |
| Charts | Recharts for cost and run-history trends |
| Graph | A small custom SVG renderer for the medallion DAG — it is a fixed 4-node chain per table, not a general graph, so a graph library is not worth the weight |
| Types | Generated from the FastAPI OpenAPI schema, so the API and the UI cannot drift silently |
| Tables | Virtualized for result and coverage tables — a failing check can return thousands of keys |

**The frontend computes nothing.** It renders what the API returns. No thresholds, no pass/fail
logic, no aggregation of results in the browser. Every verdict is computed once, in SQL, and
recorded in the state store — which is what makes the dashboard, Slack, Jira, and the PR comment
all say the same thing.

## 8. Deployment

- One Docker image, one service, behind the company's ingress with SSO in front.
- Config by mounted `projects/` directory (read from git) plus env vars.
- State migrations run at startup and refuse to serve if they fail.
- Secrets from the platform secret manager, per-project isolated.
- Non-root, no writable filesystem outside `/tmp`.
- `structlog` JSON to stdout, `run_id` on every line. The state store is the durable record.

**Maintenance entrypoints** exist for migrations and the nightly scratch sweeper, run as
scheduled jobs inside the service. They are not a user surface and are not documented as one.
