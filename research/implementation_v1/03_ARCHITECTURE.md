# 03 — Architecture

## 1. System context

```
                                            ┌──────────────────────┐
                                            │  BROWSER — React SPA │  ← the only human surface
                                            └──────────┬───────────┘
                                                       │ HTTPS + SSE
                 ┌─────────────────────────────────────▼────────┐
   git repo ────▶│        Data Pipeline Health Monitor          │
  (pipeline)     │        (one service, one Docker image)       │
                 │                                              │──▶ Slack (Block Kit)
   AWS RDS ─────▶│  api · scheduler · workers                   │──▶ Jira (REST v3)
  (source)       │  engine · parity · agents(LangGraph)         │──▶ GitHub PR (checks + comments)
                 │  evidence · state · reporting                │
  Snowflake ◀───▶│                                              │◀── webhooks: load-complete,
 (target+state)  └──────────────────────────────────────────────┘      GitHub PR, Slack buttons
```

The tool is a **single service**. All durable state lives in Snowflake (`DPHM_STATE`), so the
service itself holds nothing that matters across restarts.

**There is no user-facing CLI.** Humans use the web app; machines call signed webhooks or the
internal scheduler. See `12` for the full surface and `02` D4 for why.

## 2. Components

| Component | Package | Responsibility | Deterministic? |
|---|---|---|---|
| **Config loader** | `config/` | Parse + validate YAML into Pydantic v2 models; fail loudly at load time | Yes |
| **Catalog reader** | `catalog/` | Read `information_schema` on both engines; normalize into one `TableMeta` model | Yes |
| **Repo reader** | `repo/` | Clone, parse SQL/Python, extract grain candidates, lineage hints, ownership, blame | Yes |
| **Check engine** | `engine/` | Turn (template + params) into SQL, execute, materialize a `CheckResult` | Yes |
| **Lane B comparer** | `parity/` | Tiered RDS↔Snowflake comparison, canonicalization, landing, diff | Yes |
| **State store** | `state/` | Read/write runs, results, incidents, sign-offs, coverage, egress log | Yes |
| **Evidence collector** | `evidence/` | Gather schema diffs, load history, git history, prior incidents — **before any LLM call** | Yes |
| **Authoring agent** | `agents/authoring/` | LangGraph: discover → classify → propose manifest → generate checks → validate → PR | LLM-assisted |
| **Reporting agent** | `agents/reporting/` | LangGraph: classify failures into fixed categories, group, route | LLM-assisted |
| **Reporters** | `reporting/` | Slack, Jira, GitHub PR comment, stdout/JSON | Yes |
| **API** | `api/` | FastAPI: auth, RBAC, validation, job submission. The only public interface | Yes |
| **Scheduler + workers** | `runtime/` | Owns Lane A cadence; executes runs and graphs; streams progress | Yes |
| **Web app** | `web/` | React SPA. Renders what the API returns and **computes nothing** | Yes |

**The determinism boundary is the product's core safety property.** Everything that decides
pass/fail is deterministic SQL. The LLM only *authors candidates* (which a human merges) and
*explains failures* (which a human reads). Neither path can change a verdict.

## 2b. The medallion overlay

The lanes describe *cost*; the medallion describes *what is being compared*. They compose:

| Hop | Lane | Anchoring relation | Detail |
|---|---|---|---|
| source → bronze | B | `identical` | `07`, `07B` §3 |
| bronze → silver | A | **`dedup_of`** | `07B` §4 |
| silver → gold | A | `scd2_of` / `conserved` / `aggregate_of` | `07B` §5 |

Two components exist only because of this overlay: `engine/hops.py` (a `relation` → template
bundle lookup — this *is* "the agent generates the tests") and `engine/layer_graph.py` (the hop
DAG, which makes "the earliest failing layer" a computation rather than something the reporting
agent has to infer).

## 3. Two lanes, one engine

| | Lane A — single source | Lane B — two source |
|---|---|---|
| Question | Is the pipeline logic correct? | Do RDS and Snowflake agree? |
| Data | Snowflake only, one or more layers | RDS vs Snowflake |
| Cost | Cheap SQL | The expensive one — budget-capped |
| Trigger | Internal scheduler, or the post-load webhook | On demand: a button in the app, or the PR gate |
| Reporting | Full: Slack + Jira, deduplicated, owner-routed | Returned to whoever ran it |
| Extra machinery | Dedup, sign-offs, ownership, shadow mode | None — a human is already watching |

A check declares its own `lane` and `trigger`. One engine, two speeds.

## 4. Control flow — Lane A scheduled run

```
scheduler tick, post-load webhook, or a "Run now" click in the app
   │
   ├─ 1. load config              (config/)         fail fast on invalid YAML
   ├─ 2. fingerprint permissions  (state/)          compare grants to last run; a change is
   │                                                 reported as a permission event, never as data loss
   ├─ 3. open run                 (state/)          insert RUNS row, status=RUNNING
   ├─ 4. resolve scope            (engine/scope)    which batches are complete? build the predicate
   ├─ 5. select checks            (state/)          active, not snoozed, matching lane+tag
   ├─ 6. render + execute         (engine/)         Jinja → SQL → Snowflake, bounded concurrency,
   │                                                 IN HOP ORDER: L1 → L2 → L3
   │        └─ per check: CHECK_RESULTS row incl. columns_compared / columns_excluded
   ├─ 7. dedup + layer suppress   (state/incidents) fingerprint failing rows; match open incidents;
   │                              (engine/layer_graph) mark downstream results SUPPRESSED_BY the
   │                                                 earliest failing layer's incident
   ├─ 8. collect evidence         (evidence/)       schema diff, load history, git log, prior incidents
   ├─ 9. reporting agent          (agents/reporting) classify → group → route     [LLM]
   ├─10. report                   (reporting/)      Slack + Jira, or shadow channel only
   └─11. close run                (state/)          status, cost, coverage snapshot
```

Steps 1–8 and 10–11 are deterministic. Step 9 is the only LLM call, and it runs **after** all
evidence exists. If step 9 fails or times out, the run still reports — with raw evidence and no
narrative. **A degraded report always beats no report.**

## 5. Control flow — onboarding / authoring run

```
Onboarding wizard → POST /projects/orders/onboard
   │
   ├─ catalog discovery        both engines, metadata only, no row values
   ├─ repo ingest              shallow clone, parse, grain candidates, ownership
   ├─ table classification     [LLM] raw | scd1 | scd2 | fact | mart | reference
   ├─ column classification    [LLM] key | business | audit | derived   (+ deterministic prior from naming rules)
   ├─ HUMAN INTERRUPT ─────▶   grain, SCD2 roles, dedup contract, mart grain
   │                           surfaced as items in the app's REVIEW QUEUE (12 §2.4);
   │                           the graph is checkpointed and can wait days (see 09)
   ├─ manifest proposal        deterministic expansion of table type → check set
   ├─ check generation         [LLM] fills template params; writes SQL only for business rules
   ├─ VALIDATION GATES         budget · passes-on-good · mutation · determinism   (see 10)
   └─ git PR                   manifest YAML + generated checks + a coverage delta table
```

Nothing from this flow goes live. The PR is the only output.

## 6. Control flow — Lane B parity run

```
Parity screen → POST /projects/orders/parity  {tables, budget}   — progress streams via SSE
   │
   ├─ tier 0: canonical aggregates on each engine independently  (counts, sums, min/max, bucket fingerprints)
   ├─ compare small result sets in Python                        → if all buckets agree: PASS, stop
   ├─ tier 1: drill into disagreeing buckets, narrower fingerprints
   ├─ tier 2: land only the suspect key ranges into DPHM_SCRATCH, run full-outer-join diff in Snowflake
   └─ return differing rows + the column that caused each difference; log cost; drop scratch tables
```

Detail and the budget abort switch in `07`.

## 7. Where LangGraph sits

```
  deterministic Python                 LangGraph                     deterministic Python
  ────────────────────                ───────────                    ────────────────────
  catalog + repo + evidence   ──▶   StateGraph nodes            ──▶  YAML / SQL artifacts,
  (typed Pydantic objects)          (LLM nodes emit only             Slack blocks, Jira issues,
                                     Pydantic-typed output)          git PR
```

Rules, enforced by code review and by a CI import-linter rule:

1. `engine/`, `parity/`, `state/`, `catalog/`, `repo/` **must not import** `langchain*`.
2. Every LLM node returns a Pydantic model via `with_structured_output`. No free-text parsing.
3. Every LLM node has a deterministic fallback path; the graph must complete without the LLM.
4. All prompts live in `agents/prompts/*.md`, versioned, and their SHA is recorded on every run
   so we can attribute a behaviour change to a prompt change.

## 8. Failure and degradation posture

| Failure | Behaviour |
|---|---|
| Snowflake unreachable | Run aborts, `RUNS.status = ERROR`, alert to ops channel. No partial verdicts. |
| RDS unreachable | Lane A unaffected. Lane B fails with a clear message. |
| Anthropic API down / rate-limited | Checks still execute and report; incidents get raw evidence, `classification = UNCLASSIFIED`. |
| Slack or Jira down | Results persist in state; reporter retries with backoff, then queues for the next run. Never blocks the run. |
| Budget exceeded mid Lane B | Abort at the tier boundary, return partial findings clearly labelled `PARTIAL`. |
| Grants changed since last run | Emit a `permission_change` event and mark affected checks `INCONCLUSIVE`, not `FAIL`. |
| A check times out | `INCONCLUSIVE`, counted against coverage, never silently green. |

There are four result states, and the distinction matters:
`PASS` · `FAIL` · `INCONCLUSIVE` (we tried and could not tell) · `UNVALIDATED` (we have no way to
check this yet). Coverage reporting treats the last two as *not covered*.
