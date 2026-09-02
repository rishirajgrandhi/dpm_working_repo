# 05 — Configuration Specification

Onboarding a project means writing config, not writing checks. The target is **~50 lines of
hand-written YAML**; everything else is generated into a PR and reviewed.

**These files are the durable artifacts, but nobody hand-edits them in v1.** The onboarding wizard
collects `project.yaml`, the agent proposes the rest, and the app opens a PR — so every structural
change keeps a review and a commit history. What the YAML looks like still matters, because it is
what a reviewer reads in that PR.

Six files per project, each with a distinct owner and lifecycle:

| File | Written by | Changes when |
|---|---|---|
| `project.yaml` | Platform engineer, once | Connections or ownership change |
| `column_rules.yaml` | Platform engineer + agent proposal | Naming conventions change |
| `manifest.yaml` | **Agent proposes, human confirms grain** | Tables are added or reclassified |
| `layers.yaml` | **Agent proposes, human confirms the dedup key, pick rule, and mart grain** — see [`07B`](07B_LAYER_PARITY_MEDALLION.md) §2 | A hop is added, or a transform changes the dedup or aggregation |
| `transforms.yaml` | Pipeline author, in the PR that causes the divergence | A deliberate RDS↔Snowflake difference is introduced |
| `checks/*.yaml` | Agent generates, human reviews | Manifest or code changes |

---

## 1. `project.yaml` — the hand-written core

```yaml
project: orders
owner:
  team: "@data-platform"                 # Jira project / Slack group
  slack_channel: "#dpl-orders-alerts"
  shadow_channel: "#dpl-orders-shadow"   # used until go_live is true
  jira_project: DPO
  go_live: false                         # false = shadow mode: report, file nothing

source:                                  # INPUT 2 — AWS RDS
  kind: postgres
  host: ${DPHM_RDS_HOST}
  port: 5432
  database: orders_prod
  user: ${DPHM_RDS_USER}
  password: ${DPHM_RDS_PASSWORD}
  schemas: [public]
  read_only: true                        # asserted against pg grants at startup
  statement_timeout_s: 120
  max_rows_per_query: 5_000_000

target:                                  # INPUT 3 — Snowflake
  kind: snowflake
  account: ${DPHM_SF_ACCOUNT}
  user: ${DPHM_SF_USER}
  authenticator: snowflake_jwt
  private_key_path: ${DPHM_SF_KEY_PATH}
  role_reader: DPHM_READER
  role_writer: DPHM_WRITER
  warehouse: DPHM_WH                     # dedicated XS warehouse, own cost attribution
  database: ANALYTICS
  schemas: [STAGING, PROD]
  state_schema: DPHM_STATE
  scratch_schema: DPHM_SCRATCH
  query_timeout_s: 300

repo:                                    # INPUT 1 — git
  url: git@github.com:acme/orders-pipeline.git
  branch: main
  auth_env: DPHM_GIT_TOKEN
  paths:
    transforms: [sql/transforms, dbt/models]
    ddl: [sql/ddl, migrations]
    orchestration: [dags]
  codeowners: .github/CODEOWNERS

batch:                                   # A4 — the completed-load signal
  strategy: control_table                # control_table | audit_column | hook_only
  control_table: ANALYTICS.PROD.ETL_BATCH_LOG
  batch_id_column: BATCH_ID
  status_column: STATUS
  completed_value: SUCCESS
  completed_at_column: COMPLETED_AT
  lookback_hours: 48

defaults:
  numeric_scale: 6
  float_abs_tolerance: 1e-9
  float_rel_tolerance: 1e-9
  timestamp_granularity: second
  null_equals_null: true                 # IS NOT DISTINCT FROM semantics
  string_trim: false
  string_case_fold: false

budgets:
  lane_a_warehouse_seconds_per_run: 900
  lane_b_usd_per_run: 5.00
  lane_b_rds_seconds_per_run: 600
  llm_usd_per_run: 2.00

llm:
  provider: anthropic
  model: claude-sonnet-5
  model_escalated: claude-opus-5         # grain + business-rule authoring
  temperature: 0
  max_retries: 3
```

That is the ~50 substantive lines. Everything below is generated or incremental.

---

## 2. `column_rules.yaml` — the four-way classification

The single most important file for a useful first run. Audit columns included by mistake make
every row look different (risk R1).

```yaml
classification:
  key:
    # business keys and surrogate keys, matched or declared
    patterns: ["*_ID", "*_KEY", "*_CODE"]
  audit:                                 # EXCLUDED BY DEFAULT from comparison
    patterns:
      - "_LOADED_AT"
      - "_UPDATED_AT"
      - "ETL_*"
      - "DW_*"
      - "*_BATCH_ID"
      - "INSERTED_AT"
      - "SOURCE_FILE*"
      - "_FIVETRAN_*"
      - "_AIRBYTE_*"
  derived:                               # computed downstream; compared only if declared
    patterns: ["*_FLAG_DERIVED", "*_SCORE"]
  # anything unmatched defaults to `business` and IS compared

overrides:
  ANALYTICS.PROD.DIM_CUSTOMER:
    CUSTOMER_ID: key
    CUSTOMER_SK: key
    UPDATED_AT: business                 # genuinely business-meaningful here, not audit
    EMAIL: {class: business, pii: true, compare: hash}
    SSN:   {class: business, pii: true, compare: hash, redact_in_reports: true}
```

Rules:

- Pattern matching is case-insensitive against the **normalized upper-case** column name.
- An explicit `overrides` entry always wins over a pattern.
- `compare: hash` means the column participates in comparison as `SHA2(value)` and its raw value
  never leaves the warehouse or reaches an LLM.
- **Every check result records the resolved `columns_compared` and `columns_excluded` lists.**
  A run that cannot record them is an error, not a pass.

---

## 3. `manifest.yaml` — tables, types, grain

Proposed by the authoring agent, but `grain` and `table_type: scd2` require an explicit human
confirmation recorded with a name and timestamp (assumptions B2, B3; risk R4).

```yaml
tables:
  - name: ANALYTICS.PROD.DIM_CUSTOMER
    table_type: scd2
    source_table: public.customers       # RDS counterpart, for Lane B
    grain: [CUSTOMER_ID, VALID_FROM]
    grain_confirmed_by: "priya@acme.com"
    grain_confirmed_at: "2026-09-04T10:12:00Z"
    grain_confirmed_commit: 9f2a1c4
    scd2:
      business_key: [CUSTOMER_ID]
      surrogate_key: CUSTOMER_SK
      valid_from: VALID_FROM
      valid_to: VALID_TO
      is_current: IS_CURRENT
      tracked_columns: [NAME, SEGMENT, COUNTRY, CREDIT_LIMIT]   # type-2: change → new version
      type1_columns: [PHONE, EMAIL]                              # overwrite in place, no new version
      open_end_sentinel: "9999-12-31"
    lane_b: exact_match

  - name: ANALYTICS.PROD.FCT_ORDER
    table_type: fact
    source_table: public.orders
    grain: [ORDER_ID]
    grain_confirmed_by: "priya@acme.com"
    foreign_keys:
      - column: CUSTOMER_SK
        references: ANALYTICS.PROD.DIM_CUSTOMER.CUSTOMER_SK
    lane_b: exact_match

  - name: ANALYTICS.PROD.MART_DAILY_REVENUE
    table_type: mart
    grain: [ORDER_DATE, COUNTRY]
    grain_confirmed_by: null             # ← blocks activation of its checks
    lane_b: unvalidated                  # no RDS counterpart; reported as uncovered
```

`table_type` drives which check families are generated:

| table_type | Generated families |
|---|---|
| `raw` | key uniqueness, cast-failure rate, row-count conservation vs source landing |
| `scd1` | key uniqueness, NOT NULL, domain, FK |
| `scd2` | **the 11-check SCD suite**, plus key/domain/FK |
| `fact` | grain uniqueness, FK resolution, fanout guard, conservation, domain |
| `mart` | grain uniqueness, aggregate reconciliation vs its declared inputs |
| `reference` | key uniqueness, enum domain, row-count floor |

---

## 4. `transforms.yaml` — declared RDS→Snowflake divergences (Lane B only)

Every entry is a claim that a difference is intentional. It is bound to a commit SHA and
**expires when that code changes** (design doc §2.7).

```yaml
transforms:
  - id: drop_legacy_notes
    source_table: public.customers
    target_table: ANALYTICS.PROD.DIM_CUSTOMER
    kind: column_dropped
    columns: [LEGACY_NOTES]
    reason: "Deprecated at cutover; product signed off 2026-08-11"
    declared_by: "sam@acme.com"
    commit_sha: 3ab77e1
    expires_on_change_of: ["sql/transforms/dim_customer.sql"]

  - id: filter_test_accounts
    source_table: public.customers
    target_table: ANALYTICS.PROD.DIM_CUSTOMER
    kind: row_filter
    source_predicate: "is_test = false"
    reason: "Test accounts excluded from the warehouse"
    declared_by: "sam@acme.com"
    commit_sha: 3ab77e1
    expires_on_change_of: ["sql/transforms/dim_customer.sql"]

  - id: widen_amount_scale
    kind: type_change
    column: AMOUNT
    source_type: "numeric(12,2)"
    target_type: "NUMBER(38,6)"
    compare_as: "NUMBER(38,2)"           # compare at the narrower scale
```

Supported `kind` values in v1: `column_dropped`, `column_renamed`, `row_filter`, `type_change`,
`value_mapping`, `derived_column` (target-only, excluded from parity).

---

## 5. `checks/*.yaml` — the check definition

Generated by the agent; this is the reviewable artifact in the PR.

```yaml
- id: scd2_one_current_row_per_key.dim_customer
  lane: A
  trigger: scheduled
  template: scd/one_current_row_per_key
  table: ANALYTICS.PROD.DIM_CUSTOMER
  params:
    business_key: [CUSTOMER_ID]
    is_current: IS_CURRENT
  scope: completed_batches               # completed_batches | full | window
  severity: high
  tolerance:
    max_failing_rows: 0
  owner_team: "@data-platform"
  status: active                         # active | shadow | snoozed | draft
  source_commit_sha: 9f2a1c4
  validation:
    budget_ok: true
    passes_on_good: true
    mutation_caught: true                # gate 3 — see 10
    deterministic: true
    validated_at: "2026-09-05T09:00:00Z"
```

A check with any `validation` flag false **cannot be activated**. The API refuses, the Activate
button is disabled with a tooltip explaining which gate failed, and CI fails.

---

## 6. Pydantic model sketch

```python
# src/dphm/config/models.py
from typing import Literal, Annotated
from pydantic import BaseModel, Field, model_validator

ColumnClass = Literal["key", "business", "audit", "derived"]
TableType   = Literal["raw", "scd1", "scd2", "fact", "mart", "reference"]
Lane        = Literal["A", "B"]
Status      = Literal["active", "shadow", "snoozed", "draft"]

class SCD2Spec(BaseModel):
    business_key: list[str] = Field(min_length=1)
    surrogate_key: str
    valid_from: str
    valid_to: str
    is_current: str | None = None
    tracked_columns: list[str] = Field(min_length=1)
    type1_columns: list[str] = []
    open_end_sentinel: str | None = None

    @model_validator(mode="after")
    def keys_distinct(self):
        # Assumption B2: business key must differ from surrogate key.
        if self.surrogate_key in self.business_key:
            raise ValueError(
                "surrogate_key must not be part of business_key — "
                "this is the most common SCD2 misclassification"
            )
        overlap = set(self.tracked_columns) & set(self.type1_columns)
        if overlap:
            raise ValueError(f"columns are both tracked and type-1: {sorted(overlap)}")
        return self

class TableSpec(BaseModel):
    name: str
    table_type: TableType
    source_table: str | None = None
    grain: list[str] = Field(min_length=1)
    grain_confirmed_by: str | None = None
    grain_confirmed_at: str | None = None
    grain_confirmed_commit: str | None = None
    scd2: SCD2Spec | None = None
    lane_b: Literal["exact_match", "subset", "superset", "unvalidated"] = "unvalidated"

    @model_validator(mode="after")
    def scd2_requires_spec(self):
        if self.table_type == "scd2" and self.scd2 is None:
            raise ValueError("table_type=scd2 requires an scd2 block")
        if self.lane_b != "unvalidated" and not self.source_table:
            raise ValueError("lane_b comparison requires source_table")
        return self

    @property
    def activatable(self) -> bool:
        """B3/R4: a check on an unconfirmed grain may pass while comparing nothing."""
        return self.grain_confirmed_by is not None
```

The layer contract models (`HopSpec`, `DedupContract`, `PickRule`, `DeclaredLoss`, `MeasureMap`)
follow the same pattern and live alongside these. Their key validators:

```python
class PickRule(BaseModel):
    order_by: list[OrderTerm] = Field(min_length=1)
    must_be_total: bool = True

class DedupContract(BaseModel):
    key: list[str] = Field(min_length=1)
    key_confirmed_by: str | None = None
    pick_rule: PickRule
    compare_columns: Literal["business", "all"] | list[str] = "business"

    @model_validator(mode="after")
    def tiebreaker_present(self):
        # A single ORDER BY term is almost never a total ordering; without a tiebreaker the
        # pipeline is non-reproducible and L2.6 will fire on every run.
        if self.pick_rule.must_be_total and len(self.pick_rule.order_by) < 2:
            raise ValueError(
                "a total pick rule needs a tiebreaker column; "
                "set must_be_total=false to accept ties as a reported finding instead"
            )
        return self

    @property
    def activatable(self) -> bool:
        return self.key_confirmed_by is not None      # same rule as grain confirmation
```

**Validation happens at load, not at run.** A malformed project config must fail before a single
query is issued, with the offending file and line in the message.
