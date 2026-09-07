# Documentation Changelog

Corrections applied to this doc set, with the reasoning, so a reader who remembers the earlier
text knows what moved and why. Nothing here changes the product's intent — each entry either
resolves a contradiction *in favour of the document that was already right*, or supplies a
declaration that a spec elsewhere already depended on.

## 2026-09-02 — consistency pass

### Contradictions resolved

| # | Was | Now | Authority |
|---|---|---|---|
| 1 | `04` listed `langgraph-checkpoint-sqlite` ("local resumable onboarding runs") and `15` M6 said "SQLite checkpointing" | Both now say `SnowflakeSaver` over `DPHM_STATE.GRAPH_CHECKPOINTS`; `state/checkpointer.py` added to the `04` tree | `09` §1.3 states the checkpointer is `DPHM_STATE`-backed **explicitly not** local SQLite, `08` already had the `GRAPH_CHECKPOINTS` table, and M6's own exit criterion requires surviving a service restart — which a container-local SQLite file cannot do |
| 2 | `01` §3.1: "P0 ships in M3, P1 in M4, P2 in M5" | A table mapping each family to its real milestone, plus a note that the priority letters order work *within* a milestone | `15` ships the SCD suite in **M2** and the layer families in **M2B**; M3/M4 add no check families at all |
| 3 | `09` §1.3 `build_authoring_graph()` wired none of the four layer-contract nodes | `infer_layer_map` → `infer_dedup_contract` → `classify_losses` → `infer_measure_map`, between `propose_grain` and the interrupt | `09` §1.1's own diagram, `09` §1.5's node table, and `04`'s `agents/authoring/layer_nodes.py` all include them. The code block predated `07B` |
| 4 | L2.5 (pick-rule fidelity) and L2.7 (value fidelity) both expected `L2_WRONG_DUPLICATE_KEPT`; L2.7 had **no template in `HOP_CHECK_BUNDLES`** | L2.7 is `layer/dedup_value_fidelity` → `L2_SURVIVING_ROW_VALUE_MISMATCH`, added to the `dedup_of` bundle. `07B` §4.2 now splits the fidelity template's third branch on whether the silver row matches *any* bronze row for the key | `10`'s gate-3 anti-cheat #2 asserts the returned `violation_code` matches the expected one. One code covering two different causes — "kept the wrong duplicate" vs "rewrote the row it kept" — makes that assertion unable to distinguish two failures with different owners and different fixes |

### Declarations supplied that other specs already depended on

| # | Gap | Added |
|---|---|---|
| 5 | `10` gate 2 requires a golden window "recorded in DPHM_STATE"; no such table existed in `08` | `DPHM_STATE.GOLDEN_WINDOWS`, with `INFERRED` to carry the Q12 fallback honestly. `10` now says a missing window records `NO_GOLDEN_WINDOW` and leaves the check `draft` — it does not pass by default |
| 6 | `08` prose referenced `DPHM_STATE.SCHEMA_MIGRATIONS`; it was absent from `ddl.sql` | The table, with a `CHECKSUM` column so an edited migration is refused rather than re-applied |
| 7 | `06` §3.4's check 7 template — **the check the product exists for** — takes `stage_table`, `stage_loaded_at`, `stage_scope_predicate`. No config file declared any of them | `stage_table` / `stage_loaded_at` on `SCD2Spec`, defaulting to the `scd2_of` hop's silver source so check 7 and L3.D2 stay one template (`07B` §5.1). When neither can be resolved, the suite emits its other ten checks and **one `unvalidated` coverage row for check 7** — never a ten-check suite that presents as complete |

### Status markers

| # | Document | Change |
|---|---|---|
| 8 | `../PLAN.md` | Marked **superseded**, with a table of what reversed. Two of its sections (§4.6, §4.7 — assign the ticket to the likely author) describe behaviour the current design explicitly forbids as blame, per `11` §3 / risk R6 |

### Second pass — after the parent design doc arrived

`final_idea/Data_Pipeline_Health_Monitor_Design.docx (1).pdf` was added to the repo mid-review and
is now at the exact path the docs cite (`../final_idea/…`), so every citation resolves. Reading it
resolved one item and opened two.

| # | Finding | Action |
|---|---|---|
| 12 | Every existing citation checks out. §2.7 is "Handling expected differences" (the four defences `11` §2 implements), §5 is where "during authoring we only ever look at metadata, never row values" comes from, and "14 of 20 risks handled structurally, R8 not" is accurate. **The four deviations in `02` D1–D4 quote the doc correctly** — the Typer CLI, "Streamlit for v0; FastAPI + React later", and the Anthropic-SDK line are all verbatim | Q0d closed |
| 13 | **A fifth deviation existed and was never written down.** The design doc says it would *borrow* a diff engine if a second engine appeared ("we skip cross-engine hash diffing entirely"), and defers cross-engine work in §4.2. v1 brings it into scope as M5 and builds it in-house. `02` D2 quotes only the first half of the A2 consequence and drops the words "a borrowed diff engine" | Added as **`02` D5** and **`16` Q0e** — raised, not settled, because it is the one reversal that would visibly shrink the plan |
| 14 | The design doc's **root-cause accuracy** metric (>60% at launch, predicted vs. human-confirmed category) was dropped from `01` §6 and `PRODUCT.md` §9 — even though `08`'s `FEEDBACK.ACTION` already includes `confirm_root_cause`, so the data is being collected with nothing consuming it. "Monitoring cost per pipeline per month" was dropped too | Both restored to `01` §6 |

**What the design doc does *not* settle: Q0a.** It is intra-Snowflake throughout — its A2 assumes
one engine, and it states "the initial implementation is being planned with snowflake as the
warehousing Platform for simplicity and familiarity." There is no RDS, no medallion, no
bronze/silver/gold, and no dedup contract anywhere in it. So the RDS→Snowflake premise, the
medallion, and the L2 contract were all introduced *after* the design doc, by
`implementation_v1/`, and the Redshift/API estate in `../PARITY_SCOPE.md` is a third picture
again. Three documents, three environments, and the parent is the narrowest of them. Q0a stands.

### Raised, deliberately not answered

| # | Item | Where |
|---|---|---|
| 9 | `../PARITY_SCOPE.md` and this directory describe different environments — a legacy Redshift/dbt estate and non-queryable API sources appear in one and not the other | **`16` Block 0**, Q0a–Q0c. This is a requirements question, not a documentation defect. Writing Redshift support into the spec to make the two agree would invent scope nobody has asked for |
| 10 | ~~The parent design doc is missing~~ | **Resolved** — it arrived mid-review; see *Second pass* above |
| 11 | `../unnamed.png` is a system-architecture diagram for an unrelated **"AI Test Generation Tool"** (ingestion layer → test-generation LLM → coverage mapping engine → risk/gap analyzer) | Left in place — deleting someone's file is their call, not ours. It appears to be a stray paste |

## 2026-09-02 — M0 implementation

One change to the documented layout, forced by the docs' own import contracts.

| # | Finding | Action |
|---|---|---|
| 15 | 04's tree puts the connection factory in `state/`, but `catalog/` needs connections too — and 04 §2 places `catalog` *below* `state`. Implemented as written, `catalog/snowflake.py` imported `state/connection`, i.e. upward, and the layering contract in `.importlinter` correctly rejected it | Extracted to a new `warehouse/` package below `catalog` and `state`, which owns the connectors and the `DPHM_WRITER` allowlist. `04`'s tree and dependency diagram updated. The contract found this on its first run, which is the argument for writing the contracts before the modules |

Also recorded while building, none of which changed the specification:

* `05` §5's activation rule is now enforced in three independent places, as `10` requires
  — a Pydantic validator at config load, the API's activate endpoint, and CI.
* Gate 3 has no Snowflake test account yet (Q1), so its CI job **reports the gate as
  unmet** rather than skipping silently. A skipped mutation gate that reads as green is
  the exact failure mode `10` exists to prevent.
* The `stage_table` field added in the first pass (item 7) is what let the SCD suite's
  check 7 actually be parameterised in `projects/orders/manifest.yaml`.

## 2026-09-07 — first contact with a live Snowflake account

Three bugs in shipped code, all found by running against a real account rather than by
reading. None would have been caught by a mock.

| # | Bug | Fix |
|---|---|---|
| 16 | **`state/migrate.py` split statements naively on `;`.** `ddl.sql` contains inline comments with semicolons in them (`-- LLM text; always labelled as such`), so the splitter cut those CREATE TABLEs in half. **The migration runner would have failed applying its own schema at startup** — the first thing that happens on any real deployment | New `util/sql.py` with a comment- and literal-aware splitter, used by the runner and by the test that parses every statement, so the two cannot disagree. 8 regression tests |
| 17 | **`state/permissions.py` fingerprinted only DIRECT grants.** `SHOW GRANTS TO ROLE X` does not list what X inherits through a role it holds USAGE on, and **every Snowflake role implicitly inherits PUBLIC**. On the live account the path was `PUBLIC` → `SNOWFLAKE_LEARNING_ROLE` → `CREATE SCHEMA` on a database, so the configured role could create schemas in a database nobody had granted it — and the fingerprint reported that role as clean. This is the check the whole "checks execute as DPHM_READER and cannot write" guarantee rests on | The walk now recurses the role hierarchy and always includes PUBLIC. Effective grants went from 659 to 774 on the same role — 115 existed only via inheritance. Cycles terminate; an unreadable role is recorded as `<UNREADABLE>` rather than silently omitted, because an unreadable grant is not an absent one |
| 18 | **The forbidden-privilege list omitted every CREATE.** It held INSERT/UPDATE/DELETE/TRUNCATE/OWNERSHIP/MODIFY/WRITE — but not `CREATE SCHEMA`, which is exactly the privilege found in #17. A role that can add objects to a database can change it by any reading a reviewer would accept | Added `CREATE`/`APPLYBUDGET` plus a `CREATE *` prefix rule, so a privilege type Snowflake adds later (CREATE AGENT, CREATE MODEL — both already exist) fails toward *reporting* rather than silently permitting |

Two smaller corrections from the same session:

* Violations were reported one line per privilege. A broad role produced **82 near-identical
  lines**, in which a single genuine finding would be invisible. Added grouping by object:
  82 lines became 2.
* The Snowflake fixture opened with `create database if not exists DPHM_TEST`. The dev role
  has no `CREATE DATABASE` on the account — correctly — so the seed now assumes the database
  was provisioned once by `scripts/setup_dev_snowflake.sql` as ACCOUNTADMIN.

**Verified working against the live account:** zero-copy clone with mutation isolation (what
makes gate 3 affordable), the full medallion fixture, L2 conservation closing exactly
(`10 = 5 + 1 + 1 + 3`, unexplained 0), SCD2 versioning the tracked change into two rows, and
the as-of FK resolving a pre-change order to the old dimension version and a post-change
order to the new one.

### Not changed, though it looks like #4

L2.2 (`layer/no_invented_keys`) and the L2.5 fidelity template both emit
`L2_SILVER_ROW_NOT_IN_BRONZE`. That is fine and was left alone: two templates emitting one code
for **the same condition** is consistent, and gate 3 asserts the code returned by the check under
test. #4 was a different problem — one code for two *different* conditions.
