"""Pydantic v2 models for every config file (05 §6).

Validation happens at LOAD, not at run. A malformed project config must fail before a
single query is issued, with the offending file in the message.

The validators here are not schema decoration — each one encodes a specific documented
failure mode, named in its message so the error teaches the reader why it exists.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dphm.config import defaults

ColumnClass = Literal["key", "business", "audit", "derived"]
TableType = Literal["raw", "scd1", "scd2", "fact", "mart", "reference"]
Lane = Literal["A", "B"]
CheckStatus = Literal["active", "shadow", "snoozed", "draft", "retired"]
LaneBRelation = Literal["exact_match", "subset", "superset", "unvalidated"]
BatchStrategy = Literal["control_table", "audit_column", "hook_only"]
HopName = Literal["source_to_bronze", "bronze_to_silver", "silver_to_gold"]
HopRelation = Literal[
    "identical", "dedup_of", "scd2_of", "conserved", "aggregate_of", "filtered_from", "unvalidated"
]
LossKind = Literal["dedup", "reject_table", "filter"]
CompareMode = Literal["exact", "hash"]


class _Strict(BaseModel):
    """Reject unknown keys everywhere.

    A typo'd key that is silently ignored is how a project ends up believing it excluded
    a column it did not exclude.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# ── project.yaml ──────────────────────────────────────────────────────────────


class OwnerSpec(_Strict):
    team: str
    slack_channel: str
    shadow_channel: str
    jira_project: str | None = None
    # False = shadow mode: run, report to the shadow channel, file nothing (11 §6).
    # Default False, and it stays that way for a mandatory two-week shadow period.
    go_live: bool = False


class SourceSpec(_Strict):
    """INPUT 2 — AWS RDS. Read-only, always (02 §1)."""

    kind: Literal["postgres", "mysql"] = "postgres"
    host: str
    port: int = 5432
    database: str
    user: str
    password: str
    schemas: list[str] = Field(min_length=1)
    read_only: bool = True
    # RDS is a production OLTP box. The tool must not be capable of hurting it (02 §1).
    statement_timeout_s: int = Field(default=120, gt=0)
    max_rows_per_query: int = Field(default=5_000_000, gt=0)

    @model_validator(mode="after")
    def must_be_read_only(self) -> SourceSpec:
        if not self.read_only:
            raise ValueError(
                "source.read_only cannot be false. The tool reports on data, it never "
                "changes it, and it performs no writes of any kind to RDS (01 §5). "
                "Lane B does all its scratch work on the Snowflake side."
            )
        return self


class TargetSpec(_Strict):
    """INPUT 3 — Snowflake: the target warehouse and the state store (02 §1)."""

    kind: Literal["snowflake"] = "snowflake"
    account: str
    user: str
    authenticator: Literal["snowflake_jwt", "externalbrowser", "oauth"] = "snowflake_jwt"
    private_key_path: str | None = None
    role_reader: str = "DPHM_READER"
    role_writer: str = "DPHM_WRITER"
    warehouse: str = "DPHM_WH"
    database: str
    schemas: list[str] = Field(min_length=1)
    state_schema: str = defaults.STATE_SCHEMA
    scratch_schema: str = defaults.SCRATCH_SCHEMA
    query_timeout_s: int = Field(default=300, gt=0)

    @model_validator(mode="after")
    def key_pair_auth_needs_a_key(self) -> TargetSpec:
        if self.authenticator == "snowflake_jwt" and not self.private_key_path:
            raise ValueError(
                "authenticator=snowflake_jwt requires private_key_path. Snowflake auth is "
                "key-pair; there are no passwords in the target path (13 §3)."
            )
        return self

    @model_validator(mode="after")
    def reader_and_writer_are_distinct(self) -> TargetSpec:
        if self.role_reader.upper() == self.role_writer.upper():
            raise ValueError(
                "role_reader and role_writer must be different roles. The separation is what "
                "keeps checks unable to write anywhere: checks execute as the reader, and only "
                "state/ and parity/tier2_land.py may request a writer connection (02 §1, 13 §1)."
            )
        return self


class RepoPaths(_Strict):
    transforms: list[str] = Field(default_factory=list)
    ddl: list[str] = Field(default_factory=list)
    orchestration: list[str] = Field(default_factory=list)


class RepoSpec(_Strict):
    """INPUT 1 — the pipeline code (02 §1)."""

    url: str
    branch: str = "main"
    auth_env: str
    paths: RepoPaths = Field(default_factory=RepoPaths)
    codeowners: str = ".github/CODEOWNERS"


class BatchSpec(_Strict):
    """A4 — the completed-load signal (05 §1, 06 §5).

    If no strategy resolves, a run does NOT fall back to wall-clock. It marks affected
    checks INCONCLUSIVE, because guessing produces false failures and false failures are
    how a monitoring tool loses its audience.
    """

    strategy: BatchStrategy = "hook_only"
    control_table: str | None = None
    batch_id_column: str = "BATCH_ID"
    status_column: str = "STATUS"
    completed_value: str = "SUCCESS"
    completed_at_column: str = "COMPLETED_AT"
    lookback_hours: int = Field(default=48, gt=0)

    @model_validator(mode="after")
    def control_table_strategy_needs_a_table(self) -> BatchSpec:
        if self.strategy == "control_table" and not self.control_table:
            raise ValueError(
                "batch.strategy=control_table requires batch.control_table. Without a "
                "resolvable completed-batch predicate, scheduled checks read mid-load and "
                "report false failures (risk R2, assumption A4)."
            )
        return self


class DefaultsSpec(_Strict):
    numeric_scale: int = Field(default=defaults.DEFAULT_NUMERIC_SCALE, ge=0, le=37)
    float_abs_tolerance: float = defaults.DEFAULT_FLOAT_ABS_TOLERANCE
    float_rel_tolerance: float = defaults.DEFAULT_FLOAT_REL_TOLERANCE
    timestamp_granularity: Literal["second", "millisecond", "microsecond"] = "second"
    null_equals_null: bool = True  # IS NOT DISTINCT FROM semantics
    string_trim: bool = False
    string_case_fold: bool = False
    sample_limit: int = Field(default=defaults.DEFAULT_SAMPLE_LIMIT, gt=0, le=1000)


class BudgetSpec(_Strict):
    lane_a_warehouse_seconds_per_run: int = Field(
        default=defaults.DEFAULT_LANE_A_WAREHOUSE_SECONDS, gt=0
    )
    lane_b_usd_per_run: float = Field(default=defaults.DEFAULT_LANE_B_USD, gt=0)
    lane_b_rds_seconds_per_run: int = Field(default=defaults.DEFAULT_LANE_B_RDS_SECONDS, gt=0)
    llm_usd_per_run: float = Field(default=defaults.DEFAULT_LLM_USD, gt=0)


class LLMSpec(_Strict):
    provider: Literal["anthropic", "bedrock", "vertex"] = "anthropic"
    model: str = "claude-sonnet-5"
    # Grain and business-rule authoring escalate to the stronger model (05 §1, 09 §1.5).
    model_escalated: str = "claude-opus-5"
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_retries: int = Field(default=3, ge=0)

    @model_validator(mode="after")
    def temperature_must_be_zero(self) -> LLMSpec:
        if self.temperature != 0.0:
            raise ValueError(
                "llm.temperature must be 0. Every LLM output here is a reviewable artifact "
                "attributed to a prompt SHA; a nonzero temperature makes a behaviour change "
                "unattributable (09 §0.1, §0.3)."
            )
        return self


class ProjectConfig(_Strict):
    """`project.yaml` — the ~50 hand-written lines (05 §1)."""

    project: str = Field(pattern=r"^[a-z0-9_\-]+$")
    owner: OwnerSpec
    source: SourceSpec
    target: TargetSpec
    repo: RepoSpec
    batch: BatchSpec = Field(default_factory=BatchSpec)
    defaults: DefaultsSpec = Field(default_factory=DefaultsSpec)
    budgets: BudgetSpec = Field(default_factory=BudgetSpec)
    llm: LLMSpec = Field(default_factory=LLMSpec)

    @model_validator(mode="after")
    def state_and_scratch_are_not_monitored(self) -> ProjectConfig:
        """The tool must not monitor its own state.

        A check over DPHM_STATE would make the tool's own writes look like pipeline
        activity, and the state schema is the one place it legitimately writes.
        """
        monitored = {s.upper() for s in self.target.schemas}
        own = {self.target.state_schema.upper(), self.target.scratch_schema.upper()}
        overlap = monitored & own
        if overlap:
            raise ValueError(
                f"target.schemas must not include the tool's own schemas: {sorted(overlap)}. "
                "DPHM_STATE and DPHM_SCRATCH are written by the tool, so monitoring them "
                "would report the tool's own activity as pipeline change."
            )
        return self

    @model_validator(mode="after")
    def jira_needs_a_project_key_when_live(self) -> ProjectConfig:
        if self.owner.go_live and not self.owner.jira_project:
            raise ValueError(
                "owner.go_live=true requires owner.jira_project. Going live means filing "
                "tickets; in shadow mode Jira is never written (11 §6)."
            )
        return self


# ── column_rules.yaml (05 §2) ─────────────────────────────────────────────────


class ColumnOverride(_Strict):
    cls: ColumnClass = Field(alias="class")
    pii: bool = False
    # `hash` means the column participates as SHA2(value) and its raw value never leaves
    # the warehouse or reaches an LLM (05 §2, 13 §2).
    compare: CompareMode = "exact"
    redact_in_reports: bool = False

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def pii_must_not_be_compared_in_the_clear(self) -> ColumnOverride:
        if self.pii and self.compare != "hash":
            raise ValueError(
                "a column marked pii:true must set compare:hash. Equality is preserved by "
                "hashing and the value never materializes outside the warehouse (13 §2)."
            )
        return self


class ClassificationPatterns(_Strict):
    key: list[str] = Field(default_factory=lambda: list(defaults.DEFAULT_KEY_PATTERNS))
    # EXCLUDED BY DEFAULT from comparison. Risk R1.
    audit: list[str] = Field(default_factory=lambda: list(defaults.DEFAULT_AUDIT_PATTERNS))
    derived: list[str] = Field(default_factory=lambda: list(defaults.DEFAULT_DERIVED_PATTERNS))
    # Anything unmatched defaults to `business` and IS compared (05 §2).


class ColumnRules(_Strict):
    classification: ClassificationPatterns = Field(default_factory=ClassificationPatterns)
    # table -> column -> class or override. An explicit override always wins (05 §2).
    overrides: dict[str, dict[str, ColumnClass | ColumnOverride]] = Field(default_factory=dict)


# ── manifest.yaml (05 §3) ─────────────────────────────────────────────────────


class ForeignKeySpec(_Strict):
    column: str
    references: str


class SCD2Spec(_Strict):
    business_key: list[str] = Field(min_length=1)
    surrogate_key: str
    valid_from: str
    valid_to: str
    is_current: str | None = None
    tracked_columns: list[str] = Field(min_length=1)  # type-2: change -> new version
    type1_columns: list[str] = Field(default_factory=list)  # overwrite in place
    open_end_sentinel: str | None = None
    # SCD check 7 (06 §3.4) — the check the product exists for — needs an upstream table
    # to compare the current dimension row against. Resolved from the scd2_of hop's silver
    # source when this table is the gold end of such a hop; declared here otherwise.
    stage_table: str | None = None
    stage_loaded_at: str | None = None

    @model_validator(mode="after")
    def keys_distinct(self) -> SCD2Spec:
        # Assumption B2: the business key must differ from the surrogate key. This is the
        # most common SCD2 misclassification, and it makes the whole suite meaningless.
        if self.surrogate_key in self.business_key:
            raise ValueError(
                "surrogate_key must not be part of business_key — this is the most common "
                "SCD2 misclassification (assumption B2)"
            )
        overlap = set(self.tracked_columns) & set(self.type1_columns)
        if overlap:
            raise ValueError(f"columns are both tracked and type-1: {sorted(overlap)}")
        return self

    @model_validator(mode="after")
    def stage_columns_travel_together(self) -> SCD2Spec:
        if bool(self.stage_table) != bool(self.stage_loaded_at):
            raise ValueError(
                "stage_table and stage_loaded_at must be declared together — check 7 needs "
                "both to find the latest staged row per key (06 §3.4)"
            )
        return self

    @property
    def check7_generatable(self) -> bool:
        """False means the suite emits ten checks plus one `unvalidated` coverage row.

        Never a ten-check suite that presents as complete (05 §6).
        """
        return self.stage_table is not None


class TableSpec(_Strict):
    name: str
    table_type: TableType
    source_table: str | None = None  # RDS counterpart, for Lane B
    grain: list[str] = Field(min_length=1)
    grain_confirmed_by: str | None = None
    grain_confirmed_at: str | None = None
    grain_confirmed_commit: str | None = None
    scd2: SCD2Spec | None = None
    foreign_keys: list[ForeignKeySpec] = Field(default_factory=list)
    lane_b: LaneBRelation = "unvalidated"

    @model_validator(mode="after")
    def scd2_requires_spec(self) -> TableSpec:
        if self.table_type == "scd2" and self.scd2 is None:
            raise ValueError("table_type=scd2 requires an scd2 block")
        if self.table_type != "scd2" and self.scd2 is not None:
            raise ValueError(f"table_type={self.table_type} must not carry an scd2 block")
        if self.lane_b != "unvalidated" and not self.source_table:
            raise ValueError("lane_b comparison requires source_table")
        return self

    @property
    def activatable(self) -> bool:
        """B3/R4: a check on an unconfirmed grain may pass while comparing nothing."""
        return self.grain_confirmed_by is not None


class Manifest(_Strict):
    tables: list[TableSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def table_names_unique(self) -> Manifest:
        seen = [t.name.upper() for t in self.tables]
        dupes = sorted({n for n in seen if seen.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate table entries in manifest: {dupes}")
        return self

    def get(self, name: str) -> TableSpec | None:
        target = name.upper()
        return next((t for t in self.tables if t.name.upper() == target), None)


# ── layers.yaml (07B §2) ──────────────────────────────────────────────────────


class OrderTerm(_Strict):
    column: str
    direction: Literal["asc", "desc"] = "desc"
    nulls: Literal["first", "last"] = "last"

    def to_sql(self) -> str:
        return f"{self.column} {self.direction} nulls {self.nulls}"


class PickRule(_Strict):
    """Which duplicate survives. Must be deterministic (07B §4)."""

    order_by: list[OrderTerm] = Field(min_length=1)
    must_be_total: bool = True

    @property
    def order_by_sql(self) -> str:
        return ", ".join(t.to_sql() for t in self.order_by)

    @property
    def order_columns(self) -> list[str]:
        return [t.column for t in self.order_by]


class DeclaredLoss(_Strict):
    name: str
    kind: LossKind
    table: str | None = None
    reason_column: str | None = None
    allowed_reasons: list[str] = Field(default_factory=list)
    predicate: str | None = None

    @model_validator(mode="after")
    def kind_carries_its_own_fields(self) -> DeclaredLoss:
        if self.kind == "reject_table":
            if not self.table or not self.reason_column:
                raise ValueError(
                    f"loss '{self.name}': kind=reject_table requires table and reason_column"
                )
            if not self.allowed_reasons:
                raise ValueError(
                    f"loss '{self.name}': a reject table needs a CLOSED set of allowed_reasons, "
                    "or L2.8 cannot tell an explained reject from a silent catch-all bucket "
                    "(07B §4)"
                )
        if self.kind == "filter" and not self.predicate:
            raise ValueError(f"loss '{self.name}': kind=filter requires a predicate")
        if self.kind == "dedup" and (self.table or self.predicate):
            raise ValueError(
                f"loss '{self.name}': kind=dedup is COMPUTED from the declared dedup key, not "
                "asserted. A conservation check with an unconstrained slack term can never "
                "fail (07B §4.4)"
            )
        return self


class DedupContract(_Strict):
    """The bronze->silver initial condition (07B §4)."""

    key: list[str] = Field(min_length=1)
    key_confirmed_by: str | None = None
    key_confirmed_at: str | None = None
    pick_rule: PickRule
    compare_columns: Literal["business", "all"] | list[str] = "business"

    @model_validator(mode="after")
    def tiebreaker_present(self) -> DedupContract:
        # A single ORDER BY term is almost never a total ordering. Without a tiebreaker the
        # pipeline is non-reproducible and L2.6 fires on every run (05 §6, 07B §4.3).
        if self.pick_rule.must_be_total and len(self.pick_rule.order_by) < 2:
            raise ValueError(
                "a total pick rule needs a tiebreaker column; set must_be_total=false to "
                "accept ties as a reported finding instead"
            )
        return self

    @model_validator(mode="after")
    def pick_columns_are_not_the_key(self) -> DedupContract:
        """Ordering by the partition key cannot break a tie within that partition."""
        key = {k.upper() for k in self.key}
        useless = sorted(c for c in self.pick_rule.order_columns if c.upper() in key)
        if useless:
            raise ValueError(
                f"pick_rule orders by the dedup key itself: {useless}. Every row in a "
                "partition shares those values, so they cannot resolve a tie."
            )
        return self

    @property
    def activatable(self) -> bool:
        return self.key_confirmed_by is not None  # same rule as grain confirmation


class MeasureSpec(_Strict):
    column: str
    source_column: str | None = None
    expr: str | None = None
    additive: bool = True
    tolerance: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def needs_a_derivation(self) -> MeasureSpec:
        if not self.source_column and not self.expr:
            raise ValueError(
                f"measure '{self.column}' needs either source_column or expr so it can be "
                "independently recomputed — a mart check that cannot recompute is not a check"
            )
        return self

    @property
    def abs_tolerance(self) -> float:
        return self.tolerance.get("abs", 0.0)

    @property
    def rel_tolerance(self) -> float:
        return self.tolerance.get("rel", 0.0)


class HopSpec(_Strict):
    id: str
    hop: HopName
    lane: Lane
    source: str
    target: str
    relation: HopRelation
    key: list[str] = Field(default_factory=list)
    expect_duplicates: bool = False
    dedup: DedupContract | None = None
    declared_losses: list[DeclaredLoss] = Field(default_factory=list)
    strict_conservation: bool = True
    transforms_ref: list[str] = Field(default_factory=list)
    fanout_max: int | None = None
    measures: list[MeasureSpec] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    group_by_confirmed_by: str | None = None

    @model_validator(mode="after")
    def relation_carries_its_own_contract(self) -> HopSpec:
        if self.relation == "dedup_of" and self.dedup is None:
            raise ValueError(
                f"hop '{self.id}': relation=dedup_of requires a dedup block. The dedup "
                "contract IS this hop's initial condition (07B §4)"
            )
        if self.relation == "aggregate_of" and not self.group_by:
            raise ValueError(
                f"hop '{self.id}': relation=aggregate_of requires group_by. An aggregate has "
                "no natural row key, so the grain must be stated"
            )
        if self.relation == "aggregate_of" and not self.measures:
            raise ValueError(f"hop '{self.id}': relation=aggregate_of requires measures")
        if self.relation == "identical" and self.declared_losses:
            raise ValueError(
                f"hop '{self.id}': relation=identical cannot declare losses. Bronze is a "
                "faithful landing; nothing may be dropped at L1 (07B §3)"
            )
        return self

    @model_validator(mode="after")
    def lane_matches_the_hop(self) -> HopSpec:
        """L1 is the only cross-engine hop; everything downstream is Lane A (07B §1)."""
        expected: Lane = "B" if self.hop == "source_to_bronze" else "A"
        if self.lane != expected:
            raise ValueError(
                f"hop '{self.id}': hop={self.hop} must be lane {expected}, not {self.lane}. "
                "L1 is the only cross-engine hop; L2 and L3 are single-engine Snowflake SQL"
            )
        return self

    @property
    def contract_confirmed(self) -> bool:
        """Feeds COVERAGE.CONTRACT_CONFIRMED (08)."""
        if self.relation == "dedup_of":
            return self.dedup is not None and self.dedup.activatable
        if self.relation == "aggregate_of":
            return self.group_by_confirmed_by is not None
        return True

    @property
    def unverified_measures(self) -> list[str]:
        """Non-additive measures we cannot recompute. Reported, never silently dropped.

        L3.M4 is a reporting obligation, not a SQL check: a mart reported green with two
        silently unverified measures is the exact lie of omission this product exists to
        end (07B §5.3).
        """
        return [m.column for m in self.measures if not m.additive]


class LayerRef(_Strict):
    database: str
    schema_: str = Field(alias="schema")
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LayerContract(_Strict):
    layers: dict[str, LayerRef] = Field(default_factory=dict)
    hops: list[HopSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def hop_ids_unique(self) -> LayerContract:
        ids = [h.id for h in self.hops]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate hop ids: {dupes}")
        return self


# ── transforms.yaml (05 §4) ───────────────────────────────────────────────────

TransformKind = Literal[
    "column_dropped",
    "column_renamed",
    "row_filter",
    "type_change",
    "value_mapping",
    "derived_column",
]


class TransformSpec(_Strict):
    """A claim that a difference is intentional, bound to a commit (05 §4).

    Expires when the watched code changes. An expired transform is neither silently
    honoured nor silently dropped: it is honoured for the run and reported as expired,
    so a human decides (07 §8).
    """

    id: str
    kind: TransformKind
    source_table: str | None = None
    target_table: str | None = None
    columns: list[str] = Field(default_factory=list)
    column: str | None = None
    source_predicate: str | None = None
    source_type: str | None = None
    target_type: str | None = None
    compare_as: str | None = None
    reason: str
    declared_by: str
    commit_sha: str
    expires_on_change_of: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def expiry_needs_watched_paths(self) -> TransformSpec:
        if not self.expires_on_change_of:
            raise ValueError(
                f"transform '{self.id}': expires_on_change_of must name at least one path. "
                "An exception from March can otherwise quietly hide a real bug in July "
                "(design doc §2.7, 11 §2)"
            )
        return self


class Transforms(_Strict):
    transforms: list[TransformSpec] = Field(default_factory=list)


# ── checks/*.yaml (05 §5) ─────────────────────────────────────────────────────


class ToleranceSpec(_Strict):
    max_failing_rows: int = Field(default=0, ge=0)  # a violation is a violation
    max_failing_ratio: float | None = None
    float_abs: float = defaults.DEFAULT_FLOAT_ABS_TOLERANCE
    float_rel: float = defaults.DEFAULT_FLOAT_REL_TOLERANCE
    # Below this, the result is INCONCLUSIVE, not PASS. A check over an empty table
    # trivially passes, which is the "check that can never fail" (06 §6).
    min_rows_to_evaluate: int = Field(default=defaults.DEFAULT_MIN_ROWS_TO_EVALUATE, ge=0)


class ValidationSpec(_Strict):
    """The four gates (10). A check with any flag false cannot be activated."""

    budget_ok: bool = False
    passes_on_good: bool = False
    mutation_caught: bool = False  # gate 3 — non-negotiable
    deterministic: bool = False
    acknowledged_nondeterministic: bool = False
    validated_at: str | None = None

    @property
    def all_gates_pass(self) -> bool:
        determinism_ok = self.deterministic or self.acknowledged_nondeterministic
        return self.budget_ok and self.passes_on_good and self.mutation_caught and determinism_ok

    def failed_gates(self) -> list[str]:
        failed: list[str] = []
        if not self.budget_ok:
            failed.append("budget")
        if not self.passes_on_good:
            failed.append("passes_on_good")
        if not self.mutation_caught:
            failed.append("mutation")
        if not (self.deterministic or self.acknowledged_nondeterministic):
            failed.append("determinism")
        return failed


class CheckDef(_Strict):
    id: str
    lane: Lane
    trigger: Literal["scheduled", "on_demand", "load_hook", "pr_hook"] = "scheduled"
    template: str
    table: str
    params: dict[str, object] = Field(default_factory=dict)
    scope: Literal["completed_batches", "full", "window"] = "completed_batches"
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    tolerance: ToleranceSpec = Field(default_factory=ToleranceSpec)
    owner_team: str | None = None
    status: CheckStatus = "draft"
    hop_id: str | None = None
    source_commit_sha: str | None = None
    validation: ValidationSpec = Field(default_factory=ValidationSpec)

    @model_validator(mode="after")
    def active_requires_all_gates(self) -> CheckDef:
        """The load-time half of the activation rule (05 §5, 10).

        The API refuses too, and CI refuses too. Three independent refusals, because a
        check that can never fail manufactures confidence.
        """
        if self.status == "active" and not self.validation.all_gates_pass:
            failed = ", ".join(self.validation.failed_gates())
            raise ValueError(
                f"check '{self.id}' is status=active but failed gate(s): {failed}. "
                "A check that has not been proven able to fail cannot be activated (10)."
            )
        return self
