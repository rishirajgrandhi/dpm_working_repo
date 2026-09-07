# 09 — Agents (LangChain / LangGraph)

Two graphs, both in `src/dphm/agents/`. Neither can decide a pass/fail verdict.

| Graph | When | Output | Can it change data or verdicts? |
|---|---|---|---|
| **Authoring** | Onboarding, and on schema/code change | A git PR | No — a human merges |
| **Reporting** | End of a Lane A run, after evidence exists | Incident classification + narrative | No — the SQL already decided |

## 0. Shared foundations

### 0.1 LLM factory

```python
# agents/llm.py
from langchain_anthropic import ChatAnthropic
from dphm.state.repo import log_egress

def make_llm(cfg, *, escalated: bool = False, run_id: str, node: str):
    return ChatAnthropic(
        model=cfg.llm.model_escalated if escalated else cfg.llm.model,  # claude-opus-5 / claude-sonnet-5
        temperature=0,
        max_retries=cfg.llm.max_retries,
        timeout=90,
        callbacks=[CostAndEgressCallback(run_id=run_id, node=node)],
    )
```

`CostAndEgressCallback` writes one `LLM_EGRESS_LOG` row per call: node, prompt SHA, model, token
counts, USD, which *kinds* of payload were sent, and whether any row values were included. It
also enforces `budgets.llm_usd_per_run` and raises a `BudgetExceeded` that the graph catches and
routes to the deterministic fallback.

### 0.2 Structured output only

Every LLM node is:

```python
structured = make_llm(cfg, run_id=run_id, node="classify_tables").with_structured_output(
    TableClassificationBatch,      # Pydantic v2 model, agents/schemas.py
    include_raw=False,
)
result: TableClassificationBatch = structured.invoke(prompt)
```

There is **no free-text parsing anywhere in the codebase.** If a node cannot produce a valid
model after `max_retries`, the graph takes its fallback edge and the run continues degraded.

### 0.3 Prompts are versioned artifacts

`agents/prompts/*.md`, loaded by name, SHA recorded on `RUNS.PROMPT_SHA` and every egress row.
A change in behaviour can always be attributed to a prompt change, a model change, or a code
change — never to "the model felt different today."

### 0.4 What the LLM is never given

- Row values during **authoring**. Metadata only: table names, column names, types, nullability,
  cardinality estimates, and code excerpts. This is an absolute rule (design doc §5).
- Unredacted values during **reporting**. Samples pass through `evidence/redact.py` first;
  PII-tagged columns are replaced with a hash prefix and a type descriptor.
- Credentials, connection strings, or the contents of `.env`.

---

## 1. Authoring graph

### 1.1 Shape

```
                    ┌──────────────────┐
   START ──────────▶│ discover_catalog │  deterministic: both engines, metadata only
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │  ingest_repo     │  deterministic: clone, sqlglot parse, grain candidates,
                    └────────┬─────────┘                 ownership, lineage
                             ▼
                    ┌──────────────────┐
                    │ classify_tables  │  [LLM] raw|scd1|scd2|fact|mart|reference + confidence
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │ classify_columns │  [LLM] key|business|audit|derived
                    └────────┬─────────┘        (seeded by deterministic pattern priors)
                             ▼
                    ┌──────────────────┐
                    │  propose_grain   │  [LLM, escalated model] grain + SCD2 role mapping
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │ infer_layer_map  │  [LLM] medallion DAG: which bronze feeds which silver,
                    └────────┬─────────┘        which silver feeds which gold     (07B §6)
                             ▼
                    ┌──────────────────┐
                    │infer_dedup_      │  [LLM, escalated] dedup key + pick rule, SEEDED by the
                    │      contract    │        QUALIFY / ROW_NUMBER / DISTINCT ON read out of
                    └────────┬─────────┘        the transform SQL by repo/dedup_extract.py
                             ▼
                    ┌──────────────────┐
                    │ classify_losses  │  [LLM] is each reducing WHERE a business filter or a bug?
                    │ infer_measure_map│  [LLM] additive vs non-additive mart measures
                    └────────┬─────────┘
                             ▼
                    ╔══════════════════╗
                    ║ confirm_with_human ║  interrupt() — HARD STOP (B2, B3, R4)
                    ║                    ║  grain · dedup key · pick rule · mart group_by
                    ╚════════┬═════════╝
                             ▼
                    ┌──────────────────┐
                    │ expand_manifest  │  deterministic: table_type → check families,
                    │                  │  AND hop relation → HOP_CHECK_BUNDLES lookup (07B §6.2)
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │ generate_checks  │  [LLM] fills TEMPLATE PARAMS; free SQL only for
                    └────────┬─────────┘        business rules, and those get full code review
                             ▼
                ┌────────────────────────┐
                │  validation gates      │  gate1 budget → gate2 passes_on_good
                │  (deterministic)       │  → gate3 mutation → gate4 determinism
                └────────┬───────────────┘
                    fail │            pass
                         ▼              ▼
                 ┌──────────────┐   ┌──────────┐
                 │ repair_check │──▶│ open_pr  │  deterministic: YAML + SQL + coverage delta
                 └──────────────┘   └──────────┘
                  (max 2 attempts,        │
                   then mark draft)      END
```

### 1.2 State

```python
# agents/authoring/state.py
from typing import Annotated, TypedDict
from operator import add

class AuthoringState(TypedDict):
    run_id: str
    project: str
    config: ProjectConfig                    # frozen
    # deterministic inputs
    catalog: dict[str, TableMeta]            # both engines, normalized
    repo_facts: RepoFacts                    # grain candidates, lineage, owners, commit sha
    # LLM proposals
    table_types: dict[str, TableClassification]
    column_classes: dict[str, list[ColumnClassification]]
    grain_proposals: dict[str, GrainProposal]
    hop_proposals: list[HopProposal]              # medallion DAG (07B)
    dedup_contracts: dict[str, DedupContract]     # per bronze→silver hop
    declared_losses: dict[str, list[DeclaredLoss]]
    measure_maps: dict[str, MeasureMap]
    # human
    human_confirmations: dict[str, GrainConfirmation]   # grain · dedup key · pick rule · group_by
    # output
    manifest: Manifest | None
    layers: LayerContract | None                  # → layers.yaml
    generated_checks: Annotated[list[CheckDef], add]
    gate_results: Annotated[list[GateResult], add]
    repair_attempts: dict[str, int]
    errors: Annotated[list[str], add]
    degraded: bool                           # true if any LLM node fell back
```

### 1.3 Wiring

```python
# agents/authoring/graph.py
from langgraph.graph import StateGraph, START, END
from dphm.state.checkpointer import SnowflakeSaver   # DPHM_STATE-backed; see note below

def build_authoring_graph(cfg) -> StateGraph:
    g = StateGraph(AuthoringState)

    g.add_node("discover_catalog", discover_catalog)      # deterministic
    g.add_node("ingest_repo",      ingest_repo)           # deterministic
    g.add_node("classify_tables",  classify_tables)       # LLM
    g.add_node("classify_columns", classify_columns)      # LLM
    g.add_node("propose_grain",    propose_grain)         # LLM (escalated)
    # layer-contract nodes (07B §6.2) — these produce layers.yaml
    g.add_node("infer_layer_map",       infer_layer_map)        # LLM
    g.add_node("infer_dedup_contract",  infer_dedup_contract)   # LLM (escalated), seeded by
                                                                #   repo/dedup_extract.py
    g.add_node("classify_losses",       classify_losses)        # LLM
    g.add_node("infer_measure_map",     infer_measure_map)      # LLM
    g.add_node("confirm_with_human", confirm_with_human)  # interrupt
    g.add_node("expand_manifest",  expand_manifest)       # deterministic
    g.add_node("generate_checks",  generate_checks)       # LLM
    g.add_node("run_gates",        run_gates)             # deterministic
    g.add_node("repair_check",     repair_check)          # LLM
    g.add_node("open_pr",          open_pr)               # deterministic

    g.add_edge(START, "discover_catalog")
    g.add_edge("discover_catalog", "ingest_repo")
    g.add_edge("ingest_repo", "classify_tables")
    g.add_edge("classify_tables", "classify_columns")
    g.add_edge("classify_columns", "propose_grain")
    g.add_edge("propose_grain", "infer_layer_map")
    g.add_edge("infer_layer_map", "infer_dedup_contract")
    g.add_edge("infer_dedup_contract", "classify_losses")
    g.add_edge("classify_losses", "infer_measure_map")
    # one interrupt raises every confirmation at once — grain, dedup key, pick rule, mart grain
    g.add_edge("infer_measure_map", "confirm_with_human")
    g.add_edge("confirm_with_human", "expand_manifest")
    g.add_edge("expand_manifest", "generate_checks")
    g.add_edge("generate_checks", "run_gates")
    g.add_conditional_edges("run_gates", route_after_gates,
                            {"repair": "repair_check", "pr": "open_pr"})
    g.add_edge("repair_check", "run_gates")
    g.add_edge("open_pr", END)

    return g.compile(checkpointer=SnowflakeSaver(cfg))
```

**The checkpointer is backed by `DPHM_STATE`, not local SQLite.** An onboarding run pauses at the
human interrupt for days, and the service may restart or be redeployed in that window. A
checkpoint on a container's filesystem would lose the run. This keeps the "no new infrastructure"
principle intact — it is one more table in the schema we already own.

`route_after_gates` sends a check to `repair_check` at most twice; after that the check is written
into the PR with `status: draft` and a note explaining which gate it failed. **A check that
cannot pass its gates is never silently dropped** — a silently dropped check is invisible missing
coverage, which is the exact failure mode this product exists to eliminate.

### 1.4 The human interrupt

```python
# agents/authoring/hitl.py
from langgraph.types import interrupt

def confirm_with_human(state: AuthoringState) -> dict:
    pending = [
        p for name, p in state["grain_proposals"].items()
        if name not in state["human_confirmations"]
    ]
    if not pending:
        return {}
    answer = interrupt({
        "kind": "confirm_grain",
        "project": state["project"],
        "questions": [
            {
                "table": p.table,
                "proposed_grain": p.grain,
                "proposed_table_type": p.table_type,
                "confidence": p.confidence,
                "evidence": p.evidence,          # the GROUP BY / DISTINCT ON / PK we inferred from
                "scd2_roles": p.scd2_roles,      # business key vs surrogate key — the B2 question
                "why_it_matters": (
                    "A wrong grain produces a check that passes while comparing nothing."
                ),
            }
            for p in pending
        ],
    })
    return {"human_confirmations": answer["confirmations"]}
```

The same node raises the layer-contract confirmations from `07B` §6.3 — `kind:
"confirm_dedup_contract"` (dedup key + pick rule, shown alongside the actual `QUALIFY` clause and
the observed duplicate profile) and `kind: "confirm_mart_grain"` — because they are the same class
of decision: wrong, and the check passes while comparing nothing.

Each pending question is written to `DPHM_STATE.REVIEW_ITEMS` and appears in the app's **Review
Queue** (`12` §2.4) with the evidence it was inferred from. A Data Owner answers it there;
`POST /review-queue/{item}/respond` resumes the graph on its checkpointed thread. Because the graph
is checkpointed, the 30–60 minutes of expert time (C3) can be spent days after the run started.

This is the concrete reason the product is a web app and not a CLI (`02` D4). The person who owns
the grain answer is not the person who will run a terminal command with a thread id in it.

**The graph cannot proceed past this node without a named human.** `grain_confirmed_by` is
written into the manifest, and a check on an unconfirmed grain cannot be activated (`05` §6).

### 1.5 Where the LLM is and is not used

| Node | LLM? | Why |
|---|---|---|
| `discover_catalog` | No | It is `information_schema`. There is nothing to reason about |
| `ingest_repo` | No | `sqlglot` + `GitPython`. Deterministic and cheap |
| `classify_tables` | Yes | Naming conventions are inconsistent; code context genuinely helps |
| `classify_columns` | Yes, seeded | Patterns from `column_rules.yaml` produce a prior; the LLM only adjudicates unmatched columns and flags suspected audit columns the patterns missed |
| `propose_grain` | Yes, escalated | The highest-stakes judgement, so the strongest model — and it still goes to a human |
| `infer_layer_map` | Yes | Naming alone cannot always tell which bronze table feeds which silver one; `sqlglot` lineage seeds it |
| `infer_dedup_contract` | Yes, escalated | Usually **read** from a `QUALIFY`/`DISTINCT ON` in the transform; the model only judges whether the partition key matches the business meaning of a duplicate |
| `classify_losses` | Yes | Distinguishing "this WHERE is a business filter" from "this WHERE is a bug" needs code context |
| `infer_measure_map` | Yes | Additive vs non-additive. Fallback biases to non-additive, which is the safe direction |
| `expand_manifest` | No | `table_type` → check families and `relation` → `HOP_CHECK_BUNDLES` are both lookup tables |
| `generate_checks` | Yes | Fills template parameters. Roughly 85% of checks are pure parameter fills |
| `run_gates` | No | Executing SQL and comparing results |
| `repair_check` | Yes | Given the gate failure, propose new parameters. Never rewrites a template |
| `open_pr` | No | File writes and a REST call |

Deterministic fallbacks, so the graph always completes: `classify_tables` → naming heuristics;
`classify_columns` → patterns only; `propose_grain` → declared PK, marked unconfirmed;
`infer_layer_map` → schema-name matching plus `sqlglot` lineage; `infer_dedup_contract` → the
parsed `QUALIFY`/`DISTINCT ON` only, and the hop is marked `unvalidated` if there is none;
`infer_measure_map` → `sum`/`count` additive, everything else non-additive;
`generate_checks` → generate only the families with no free parameters. A degraded run sets
`degraded: true` and the PR body says which nodes fell back.

**The fallbacks fail toward less coverage, never toward false coverage.** A hop whose dedup
contract could not be established becomes `unvalidated` — it generates no checks and one coverage
row saying so. It never generates a check that would pass by default.

---

## 2. Reporting graph

Runs at step 9 of a Lane A run — **after** all evidence exists. This ordering is the structural
answer to risk R5 (the LLM inventing a root cause): it never gets to guess, because the facts
are already in the prompt.

### 2.1 Shape

```
START
  │
  ▼
┌────────────────────┐
│ load_failures      │  deterministic: FAIL results from this run
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ collect_evidence   │  deterministic, parallel fan-out per failure:
│  ├ schema_diff     │    schema changes since last green run
│  ├ load_history    │    ACCOUNT_USAGE: load times, row counts, failures
│  ├ git_history     │    commits touching the lineage of this table since last green
│  ├ prior_incidents │    same fingerprint before? how was it resolved?
│  ├ audit_columns   │    which BATCH_ID / run produced the failing rows
│  └ permissions     │    did our grants change? (R7)
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ redact             │  deterministic: PII → hash prefix + type descriptor
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ classify           │  [LLM] CLOSED category vocabulary + confidence + cited evidence ids
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ group              │  [LLM] cluster failures into incidents, seeded by a deterministic
└─────────┬──────────┘        clustering on (table lineage, batch id, category)
          ▼
┌────────────────────┐
│ route              │  deterministic: CODEOWNERS → team queue; git blame → likely author
└─────────┬──────────┘        assign to the TEAM, only MENTION the author (R6)
          ▼
┌────────────────────┐
│ narrate            │  [LLM] short human summary per incident, labelled AI-generated
└─────────┬──────────┘
          ▼
┌────────────────────┐
│ persist_and_report │  deterministic: INCIDENTS upsert, Slack, Jira (or shadow channel)
└─────────┬──────────┘
         END
```

### 2.2 Closed category vocabulary

The classifier may only choose from this list. It is an enum in the Pydantic schema, so an
invented category is a validation error, not an output.

```python
Category = Literal[
    "UPSTREAM_SCHEMA_CHANGE",     # a column changed shape upstream
    "PIPELINE_LOGIC_CHANGE",      # a commit changed the transform
    "SCD_DEGRADED_TO_OVERWRITE",  # the motivating failure
    "DEDUP_WRONG_ROW_KEPT",       # L2: the pick rule was not applied as declared
    "DEDUP_NON_DETERMINISTIC",    # L2: the pick rule does not resolve every tie
    "DEDUP_FAILED",               # L2: duplicates survived into silver
    "UNEXPLAINED_ROW_LOSS",       # any hop: conservation does not close
    "JOIN_FANOUT",                # L3: a join multiplied rows
    "AGGREGATE_MISMATCH",         # L3: a mart does not recompute from silver
    "LATE_OR_PARTIAL_LOAD",       # batch incomplete despite the scope predicate
    "SOURCE_DATA_QUALITY",        # bad data arrived; the pipeline behaved correctly
    "REFERENTIAL_BREAK",          # parent rows missing or arriving late
    "TYPE_OR_PRECISION_LOSS",     # cast or scale problem, incl. RDS→Snowflake
    "DUPLICATE_LOAD",             # a batch applied twice
    "PERMISSION_OR_ACCESS_CHANGE",# our grants changed; NOT data loss
    "DECLARED_TRANSFORM_EXPIRED", # a transforms.yaml entry no longer matches the code
    "CHECK_DEFECT",               # the check is wrong, not the data
    "UNKNOWN",                    # explicitly allowed; better than a fabricated cause
]
```

`UNKNOWN` is a legitimate, reportable outcome. Forcing a category is how a monitoring tool
teaches its users to distrust it.

### 2.3 Classification output schema

```python
class FailureClassification(BaseModel):
    result_id: str
    category: Category
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)   # MUST cite collected evidence
    reasoning: str = Field(max_length=800)
    suggested_next_step: str | None = None

    @model_validator(mode="after")
    def cite_or_be_unknown(self):
        # A category with no evidence citation is a guess. Downgrade it.
        if self.category != "UNKNOWN" and not self.evidence_ids:
            raise ValueError("a classification must cite at least one evidence id")
        return self
```

Confidence below `0.6` is reported as `UNKNOWN` with the model's guess shown as a *hypothesis*,
clearly separated from the evidence in the Slack message.

### 2.4 Grouping

Deterministic clustering runs first: failures sharing a table lineage ancestor, a batch id, or a
category are pre-grouped. The LLM only merges or splits those clusters, and it must justify each
merge with a shared cause. This keeps R6 ("one break files thirty tickets") handled by structure
rather than by prompt.

**The medallion makes this stronger.** `engine/layer_graph.py` knows the hop DAG, so "the earliest
failing layer" is a deterministic computation, not an inference: an L1 failure suppresses incident
creation for every L2 and L3 object downstream of it (results still recorded, marked
`SUPPRESSED_BY`). The reporting agent never has to work out that thirty gold failures are one
bronze problem — it is told.

### 2.5 Why this is trustworthy

| Risk | Structural handling |
|---|---|
| LLM invents a root cause | Evidence collected first, deterministically; classification must cite evidence ids; closed vocabulary; `UNKNOWN` allowed |
| PII reaches the LLM | Redaction node runs before every LLM node; PII columns compared as hashes; every call logged to `LLM_EGRESS_LOG` |
| Blame | Assign to the team queue; mention the likely author. Never auto-assign to a person |
| Alert fatigue | Dedup by failing-row fingerprint before the graph runs; shadow mode until `go_live` |
| LLM unavailable | Every LLM node has a fallback; incidents ship with raw evidence and `category = UNCLASSIFIED` |

---

## 3. Testing the graphs

- **Node tests**: each deterministic node is a pure function over fixtures.
- **LLM node tests**: a `FakeListChatModel` returning recorded structured outputs, plus a schema
  fuzzer that feeds malformed output to confirm the fallback edge is taken.
- **Graph tests**: full traversal on a fixture project, asserting node order, that the interrupt
  fires, and that resume produces the same manifest.
- **Golden PR test**: the authoring graph against a fixture repo produces a byte-stable PR diff.
- **Egress test**: assert that no test run writes an `LLM_EGRESS_LOG` row with
  `ROW_VALUES_SENT = true` during authoring. This is a CI gate.
