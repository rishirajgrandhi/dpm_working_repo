# Data Pipeline Integrity Agent — Implementation Plan

> **⚠️ SUPERSEDED — historical record only. Do not build from this document.**
>
> This was the first-generation plan. It has been replaced in full by
> [`implementation_v1/`](implementation_v1/00_README.md), which changed the premises materially:
>
> | This document | `implementation_v1/` |
> |---|---|
> | Intra-Snowflake only (staging vs prod) | **AWS RDS → Snowflake migration**; cross-engine is Lane B |
> | Typer CLI on cron | **A web app. There is no CLI** (`02` D4) |
> | Anthropic SDK direct | **LangChain + LangGraph** (`02` D1) |
> | Row count + checksum | Templated check families, incl. the 11-check SCD suite and the L2 dedup contract |
> | Jira assigned to the **likely author** (§4.6, §4.7) | Assigned to the **team queue**; the author is only *mentioned* — auto-assignment reads as blame and the evidence is correlational (`11` §3, risk R6) |
> | No validation of the checks themselves | **Four validation gates**, mutation testing a blocking merge gate (`10`) |
>
> The last two rows are reversals of intent, not just elaborations. §4.6/§4.7 below describe
> behaviour the current design explicitly forbids.
>
> What survives from here and is still worth reading: the problem statement (§1), the
> evidence-before-LLM ordering (§4.4), and the incident-dedup state machine (§4.3).

## 1. Problem & Goal

Today, source-vs-target parity checks between pipeline stages are built manually and reactively — issues are discovered only after they've already affected the business.

Goal: build an agent that:
1. Runs data integrity/parity checks on a schedule (daily / cron).
2. When a check fails, performs root cause analysis (RCA) using an LLM over Snowflake metadata + git history.
3. Auto-files a Jira ticket, pre-filled with the RCA summary, assigned to the developer who most likely owns the change (derived from git blame).
4. Is architected so the check engine and connector layer can later be extended to cross-platform parity (RDS vs Snowflake vs other sources), not just intra-Snowflake.

## 2. Scope

**V1 (this plan): narrow end-to-end slice, proven on real data before widening.**

- Source of truth for checks: **Snowflake only** (e.g. staging schema vs prod schema, or raw vs modeled tables).
- Pipeline code being monitored: **custom Python/SQL scripts** (not dbt) checked into git.
- RCA: **LLM-based** (Claude), fed by deterministic signals — not a rule engine, but not free-form guessing either.
- Ownership resolution: **git blame** on the script(s) that produce the failing table, mapped to a Jira account.
- Deliverable: one working check pair, running on a schedule, that on failure produces a real Jira ticket with a credible root-cause writeup and correct assignee.

**Explicitly out of scope for V1** (captured in §9 Future Extensions):
- Cross-platform checks (RDS, BigQuery, S3, etc.)
- dbt-native lineage/ownership
- Automated remediation (the agent files tickets, it does not fix data)
- A UI/dashboard

## 3. Architecture Overview

```
                ┌─────────────────┐
   cron/DAG ──▶ │   Runner (CLI)   │
                └────────┬─────────┘
                         │
        ┌────────────────┼─────────────────────┐
        ▼                                       ▼
┌───────────────┐                     ┌───────────────────┐
│ Check Engine   │──fail──┐            │  State Store       │
│ (Snowflake)    │        │            │  (Snowflake schema:│
└───────────────┘        │            │  runs, incidents)  │
                          ▼            └─────────▲──────────┘
                 ┌─────────────────┐             │
                 │ RCA Signal       │             │
                 │ Collector        │─────────────┘
                 │ - schema diff    │
                 │ - query/load     │
                 │   history        │
                 │ - git blame/diff │
                 └────────┬────────┘
                          ▼
                 ┌─────────────────┐
                 │ Claude RCA       │
                 │ (structured      │
                 │  output)         │
                 └────────┬────────┘
                          ▼
                 ┌─────────────────┐
                 │ Dedup/Incident   │──already open──▶ add Jira comment
                 │ State Machine    │
                 └────────┬────────┘
                          │ new incident
                          ▼
                 ┌─────────────────┐
                 │ Jira Ticket      │
                 │ Filer            │
                 └─────────────────┘
```

## 4. Components

### 4.1 Config
- `config/checks.yaml` — declarative check definitions:
  ```yaml
  - id: orders_staging_vs_prod
    source: RAW.STAGING.ORDERS
    target: ANALYTICS.PROD.ORDERS
    key_columns: [order_id]
    checks: [row_count, checksum]     # extensible: nullness, freshness, distribution
    owning_paths:                     # files whose git history maps to ownership
      - pipelines/orders/load_orders.py
      - pipelines/orders/transform_orders.sql
  ```
- `config/ownership.yaml` — git author → Jira account mapping (email-based), plus a default fallback assignee/queue.

### 4.2 Check Engine (`checks/`)
- Runs comparisons **inside Snowflake** (push-down SQL) to avoid pulling data out: row counts, aggregate checksums (e.g. `HASH_AGG` or checksum over key+business columns), null-rate deltas.
- Each check returns a structured result: `pass/fail`, magnitude of divergence, and the specific rows/columns/aggregates that diverged (bounded sample, not full diff, to control cost).
- Pluggable: new check types are classes implementing a common `Check` interface — this is also the seam where non-Snowflake sources get added later (§9).

### 4.3 State Store
- Reuse Snowflake itself — a small `PIPELINE_MONITORING` schema with tables:
  - `CHECK_RUNS` (check_id, run_ts, status, details)
  - `INCIDENTS` (incident_id, check_id, opened_ts, status, jira_key, last_seen_ts)
- Avoids standing up a separate database. `INCIDENTS` is what drives dedup — a failing check with an already-open incident gets a Jira **comment**, not a new ticket; N consecutive passes auto-resolves the incident.

### 4.4 RCA Signal Collector (`rca/`)
Gathers evidence *before* calling the LLM, so RCA is grounded rather than speculative:
- **Schema diff**: compare current `INFORMATION_SCHEMA.COLUMNS` for source/target against the last known-good snapshot (stored in state).
- **Snowflake operational history**: `QUERY_HISTORY`, `LOAD_HISTORY`, `COPY_HISTORY` for the target table in the window since the last passing run — surfaces load failures, warehouse issues, error codes.
- **Git blame/diff**: for each `owning_paths` entry, pull the commit history in the failure window (`git log --since=<last_good_run>`) and the diff content, plus blame on the affected logic.

### 4.5 LLM RCA (Claude)
- Structured-output call (JSON schema / tool-use) so the result is directly usable, not free text to parse:
  ```json
  {
    "root_cause_hypothesis": "...",
    "confidence": "high|medium|low",
    "evidence_cited": ["..."],
    "suggested_fix": "...",
    "jira_summary": "...",
    "jira_description_markdown": "..."
  }
  ```
- Prompted to only cite evidence actually present in the collected signals — reduces hallucinated causes.
- Low temperature; if confidence is "low", still file the ticket but flag it clearly rather than suppressing it (silence is worse than an uncertain lead for triage).

### 4.6 Ownership Resolution
- For a failing check, resolve `owning_paths` → run `git blame` scoped to the commits touching those files most recently (weighted toward recency over line-count, since a recent bad change is more likely the cause than the original author).
- Map the resulting git author email → Jira account via `ownership.yaml`; unmapped authors fall back to a default team/queue rather than failing silently.

### 4.7 Jira Ticket Filer
- Uses the Jira REST API directly (not the interactive Atlassian MCP, since this runs unattended on a schedule) via a service account/API token.
- Ticket fields: summary, description (RCA markdown + link to the failing check + evidence), assignee (from §4.6), labels (`data-integrity`, `auto-filed`, check_id), and links the `INCIDENTS` row to the created issue key.
- Idempotent: guarded by the state-machine in §4.3.

### 4.8 Runner / Scheduler
- `runner.py` orchestrates: load config → run checks → on failure, collect signals → RCA → dedup check → file/update ticket → record state.
- Scheduling options (pick one for V1, others are drop-in later since the runner is just a CLI):

| Option | Effort | Fit |
|---|---|---|
| Cron on an existing VM/container | Lowest | Good default if no orchestrator exists yet |
| GitHub Actions scheduled workflow | Low | Good if the pipeline repo already lives on GitHub — keeps config/code/schedule co-located |
| Airflow DAG | Medium | Best only if Airflow is already in use elsewhere |
| AWS Lambda + EventBridge | Medium | Best if already AWS-native; watch Lambda's 15-min timeout if checks grow large |

Recommendation for V1: **cron or GitHub Actions** — no new infra, fast to ship the end-to-end slice; revisit once check volume grows.

## 5. Tech Stack

- **Language**: Python 3.11+
- **Snowflake**: `snowflake-connector-python`
- **LLM**: Anthropic Python SDK (Claude), structured output via tool-use
- **Jira**: `requests` against Jira REST API v3 (or `atlassian-python-api`)
- **Git**: `GitPython` or `subprocess` + system `git` for blame/log
- **Config/validation**: `pydantic` + YAML
- **Secrets**: environment variables, backed by whatever secrets manager the deployment target provides (Secrets Manager / GH Actions secrets)

## 6. Proposed Repo Structure

```
data_pipeline_monitoring/
  pyproject.toml
  config/
    checks.yaml
    ownership.yaml
  sql/
    state_schema.sql          # DDL for CHECK_RUNS / INCIDENTS
  src/monitoring/
    config.py
    snowflake_client.py
    checks/
      base.py
      row_count.py
      checksum.py
    rca/
      signal_collector.py
      git_blame.py
      llm_analyzer.py
    ticketing/
      jira_client.py
      dedup.py
    runner.py
    cli.py
  tests/
  Dockerfile
  README.md
```

## 7. Rollout Plan / Milestones

1. **Scaffolding**: repo, config schema, read-only Snowflake service role, secrets wiring.
2. **Check engine v1**: row count + checksum check for one real staging-vs-prod table pair; run manually via CLI; log pass/fail.
3. **State + scheduling**: `CHECK_RUNS`/`INCIDENTS` tables in Snowflake; wire up cron/GitHub Actions.
4. **RCA pipeline**: signal collector (schema diff, query/load history, git blame/diff) + Claude structured RCA call.
5. **Shadow mode**: RCA output posted to Slack (or a Jira draft/comment) for human review — *no auto-filed tickets yet* — to validate RCA quality and avoid false alarms eroding trust.
6. **Live ticket filing**: enable real Jira ticket creation with dedup/incident lifecycle once shadow-mode accuracy is acceptable.
7. **Pilot expansion**: onboard 3–5 more table pairs, tune noise thresholds.
8. **Check catalog growth**: freshness, null-rate drift, distribution checks.

## 8. Risks & Mitigations

- **Noisy/duplicate tickets** → incident state machine (§4.3) dedups and auto-resolves.
- **LLM hallucinated root cause** → RCA is evidence-grounded and cites sources; low-confidence findings are labeled, not suppressed; shadow-mode phase (step 5) validates before auto-filing goes live.
- **Ambiguous git ownership** (multiple recent authors, moved files) → weight blame by recency, fall back to a default queue instead of guessing wrong.
- **Snowflake compute cost of checks** → push-down SQL, bounded sampling for diffs, not full-table pulls.
- **Jira API/permission issues** → dedicated service account with least-privilege project access; failures logged and alerted separately so a broken integration doesn't look like "no issues found."

## 9. Future Extensions

- **Cross-platform parity**: the `Check` interface (§4.2) is already source-agnostic — add a connector abstraction (Snowflake, RDS/Postgres, MySQL, BigQuery, S3) and a `source_type`/`target_type` field in `checks.yaml`. Comparisons that can't be pushed down to a single engine get computed by pulling bounded aggregates from each side rather than raw rows.
- **dbt-native ownership**: if/when pipelines migrate to dbt, ownership resolution can additionally use `meta.owner` in `schema.yml` instead of relying solely on git blame.
- **Orchestrator log integration**: pull Airflow/other scheduler task logs into the RCA signal set if/when such a system is introduced.
- **Slack notifications** alongside Jira for real-time visibility.
- **Dashboard** over the `CHECK_RUNS`/`INCIDENTS` tables for trend visibility.
