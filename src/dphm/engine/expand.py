"""Config -> concrete parameterised checks (03 §5 `expand_manifest`).

Deterministic: a lookup from table type and hop relation to a template list, plus
parameter fill from the declarations already in config. No model is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dphm.engine import hops
from dphm.util.ids import check_id as make_check_id

if TYPE_CHECKING:
    from dphm.config.loader import LoadedProject
    from dphm.config.models import DeclaredLoss, HopSpec, MeasureSpec, TableSpec


@dataclass(frozen=True)
class PlannedCheck:
    """A check ready to render: template, target, parameters, and provenance."""

    check_id: str
    template: str
    table: str
    params: dict[str, Any]
    lane: str = "A"
    hop_id: str | None = None
    layer: str | None = None
    relation: str = ""
    severity: str = "medium"
    # Why this check cannot run, when it cannot. Reported as UNVALIDATED, never dropped:
    # a silently dropped check is invisible missing coverage.
    unvalidated_reason: str | None = None
    grain_confirmed: bool = False
    contract_confirmed: bool = False
    key_columns: tuple[str, ...] = ()

    @property
    def is_runnable(self) -> bool:
        return self.unvalidated_reason is None


@dataclass
class Plan:
    checks: list[PlannedCheck] = field(default_factory=list)
    # Objects with nothing to check, and why. The honest half of the report.
    uncovered: list[tuple[str, str]] = field(default_factory=list)

    @property
    def runnable(self) -> list[PlannedCheck]:
        return [c for c in self.checks if c.is_runnable]

    @property
    def unvalidated(self) -> list[PlannedCheck]:
        return [c for c in self.checks if not c.is_runnable]


def _layer_of(table: str) -> str | None:
    parts = table.upper().split(".")
    if len(parts) < 2:
        return None
    schema = parts[-2]
    return schema.lower() if schema in {"BRONZE", "SILVER", "GOLD"} else None


def _measure_params(measures: list[MeasureSpec]) -> list[dict[str, Any]]:
    return [
        {
            "column": m.column,
            "source_column": m.source_column or m.column,
            "expr": m.expr or f"sum({m.source_column or m.column})",
            "abs_tol": m.abs_tolerance or 0.0,
            "rel_tol": m.rel_tolerance or 0.0,
        }
        for m in measures
    ]


def plan_project(loaded: LoadedProject, *, sample_limit: int = 20) -> Plan:
    """Every check this project's config implies, runnable or not."""
    plan = Plan()
    manifest = loaded.manifest

    for table in manifest.tables:
        _plan_table(plan, table, sample_limit=sample_limit)

    for hop in loaded.layers.hops:
        _plan_hop(plan, hop, loaded, sample_limit=sample_limit)

    # Gold objects with no hop feeding them cannot be checked at all — reported, never
    # quietly omitted (07B §9).
    hop_targets = {h.target.upper() for h in loaded.layers.hops}
    for table in manifest.tables:
        layer = _layer_of(table.name)
        if layer in {"silver", "gold"} and table.name.upper() not in hop_targets:
            plan.uncovered.append(
                (table.name, "no hop declares how this table is derived — unvalidated")
            )

    return plan


def _plan_table(plan: Plan, table: TableSpec, *, sample_limit: int) -> None:
    for template in hops.expand_table_type(table.table_type):
        params: dict[str, Any] = {
            "table": table.name,
            "grain": list(table.grain),
            "sample_limit": sample_limit,
        }
        reason = None if table.activatable else "grain not confirmed by a human (B3/R4)"
        plan.checks.append(
            PlannedCheck(
                check_id=make_check_id(template, table.name, params),
                template=template,
                table=table.name,
                params=params,
                layer=_layer_of(table.name),
                relation="one_row_per_key",
                unvalidated_reason=reason,
                grain_confirmed=table.activatable,
                key_columns=tuple(table.grain),
            )
        )


def _plan_hop(plan: Plan, hop: HopSpec, loaded: LoadedProject, *, sample_limit: int) -> None:
    templates = hops.expand_hop(hop.relation)
    if not templates:
        plan.uncovered.append(
            (hop.target, f"hop {hop.id} relation={hop.relation} generates no checks")
        )
        return

    table = loaded.manifest.get(hop.target)
    scd2 = table.scd2 if table and table.scd2 else None
    layer = _layer_of(hop.target)

    for template in templates:
        params, reason, _keys = _params_for(template, hop, loaded, sample_limit=sample_limit)
        if params is None:
            plan.uncovered.append((hop.target, reason or f"{template}: not parameterisable"))
            continue
        plan.checks.append(
            PlannedCheck(
                check_id=make_check_id(template, hop.target, params),
                template=template,
                table=hop.target,
                params=params,
                lane=hop.lane,
                hop_id=hop.id,
                layer=layer,
                relation=hop.relation,
                severity="high"
                if "dedup_pick_rule" in template or "tracked_change" in template
                else "medium",
                unvalidated_reason=reason,
                grain_confirmed=bool(table and table.activatable),
                contract_confirmed=hop.contract_confirmed,
                key_columns=(
                    tuple(scd2.business_key)
                    if scd2
                    else tuple(hop.dedup.key)
                    if hop.dedup
                    else tuple(hop.key)
                ),
            )
        )


def _params_for(
    template: str, hop: HopSpec, loaded: LoadedProject, *, sample_limit: int
) -> tuple[dict[str, Any] | None, str | None, tuple[str, ...]]:
    """Parameters for one (template, hop) pair, plus why it cannot run if it cannot."""
    table = loaded.manifest.get(hop.target)
    base: dict[str, Any] = {"sample_limit": sample_limit, "scope_predicate": "1=1"}
    dedup = hop.dedup

    if template.startswith("scd/"):
        if not table or not table.scd2:
            return None, f"{hop.target} is not declared table_type=scd2", ()
        s = table.scd2
        params = {
            **base,
            "table": hop.target,
            "business_key": list(s.business_key),
            "surrogate_key": s.surrogate_key,
            "valid_from": s.valid_from,
            "valid_to": s.valid_to,
            "is_current": s.is_current or "IS_CURRENT",
            "tracked_columns": list(s.tracked_columns),
            "type1_columns": list(s.type1_columns),
            "open_end_sentinel": s.open_end_sentinel or "9999-12-31",
            "compared_columns": list(s.tracked_columns),
        }
        reason = None if table.activatable else "grain not confirmed by a human (B3/R4)"
        if template == "scd/closed_versions_immutable":
            params["baseline_table"] = (
                f"{loaded.project.target.database}."
                f"{loaded.project.target.state_schema}.SCD_VERSION_HASHES"
            )
        if template == "scd/tracked_change_created_version":
            if not s.stage_table:
                return (
                    None,
                    "no stage_table declared, so SCD check 7 cannot be generated — the "
                    "suite ships its other checks and reports this one as unvalidated",
                    (),
                )
            params |= {
                "stage_table": s.stage_table,
                "stage_loaded_at": s.stage_loaded_at,
                "stage_scope_predicate": "1=1",
            }
        if template == "scd/type1_consistent_across_versions" and not s.type1_columns:
            return None, "no type-1 columns declared, so there is nothing to assert", ()
        return params, reason, tuple(s.business_key)

    if template.startswith("layer/dedup"):
        if not dedup:
            return None, f"hop {hop.id} has no dedup contract", ()
        loss_filter = _loss_predicate(list(hop.declared_losses), list(dedup.key), alias="src")
        reason = None if dedup.activatable else "dedup key/pick rule not confirmed (07B §6.3)"
        compare = _compare_columns(hop, loaded)
        return (
            {
                **base,
                "silver_table": hop.target,
                "bronze_table": hop.source,
                "dedup_key": list(dedup.key),
                "pick_order_by": dedup.pick_rule.order_by_sql,
                "pick_order_columns": dedup.pick_rule.order_columns,
                "compare_columns": compare,
                "bronze_scope_predicate": "1=1",
                "loss_filter_predicate": loss_filter,
            },
            reason,
            tuple(dedup.key),
        )

    if template == "layer/reject_reasons_closed":
        reject = next((loss for loss in hop.declared_losses if loss.kind == "reject_table"), None)
        if not reject or not reject.table:
            return None, f"hop {hop.id} declares no reject table", ()
        return (
            {
                **base,
                "reject_table": reject.table,
                "reason_column": reject.reason_column,
                "allowed_reasons": list(reject.allowed_reasons),
            },
            None,
            (),
        )

    if template == "layer/row_conservation":
        losses = []
        for loss in hop.declared_losses:
            if loss.kind == "reject_table" and loss.table:
                losses.append(
                    {"name": loss.name, "count_expr": f"select count(*) from {loss.table}"}
                )
            elif loss.kind == "filter" and loss.predicate:
                losses.append(
                    {
                        "name": loss.name,
                        "count_expr": (f"select count(*) from {hop.source} where {loss.predicate}"),
                    }
                )
        loss_filter = _loss_predicate(
            list(hop.declared_losses),
            list(dedup.key) if dedup else list(hop.key),
            alias="src",
        )
        return (
            {
                **base,
                "hop_id": hop.id,
                "input_table": hop.source,
                "output_table": hop.target,
                "input_scope_predicate": "1=1",
                "output_scope_predicate": "1=1",
                "declared_losses": losses,
                "dedup_key": list(dedup.key) if dedup else [],
                "loss_filter_predicate": loss_filter,
            },
            None,
            (),
        )

    if template == "layer/no_invented_keys":
        key = list(dedup.key) if dedup else list(hop.key)
        if not key:
            return None, f"hop {hop.id} declares no key", ()
        return (
            {
                **base,
                "silver_table": hop.target,
                "bronze_table": hop.source,
                "dedup_key": key,
                "bronze_scope_predicate": "1=1",
            },
            None,
            tuple(key),
        )

    if template == "layer/fanout_guard":
        if hop.fanout_max is None or not hop.key:
            return None, f"hop {hop.id} declares no fanout_max", ()
        return (
            {
                **base,
                "target_table": hop.target,
                "key": list(hop.key),
                "fanout_max": hop.fanout_max,
            },
            None,
            tuple(hop.key),
        )

    if template == "layer/measure_conservation":
        additive = [m for m in hop.measures if m.additive]
        if not additive:
            return None, f"hop {hop.id} declares no additive measures", ()
        return (
            {
                **base,
                "hop_id": hop.id,
                "source_table": hop.source,
                "target_table": hop.target,
                "source_scope_predicate": "1=1",
                "target_scope_predicate": "1=1",
                "measures": _measure_params(additive),
            },
            None,
            (),
        )

    if template == "layer/aggregate_of":
        additive = [m for m in hop.measures if m.additive]
        if not hop.group_by or not additive:
            return None, f"hop {hop.id} declares no group_by or no additive measures", ()
        reason = (
            None
            if hop.group_by_confirmed_by
            else "mart group_by not confirmed by a human (07B §6.3)"
        )
        return (
            {
                **base,
                "mart_table": hop.target,
                "source_table": hop.source,
                "source_scope_predicate": "1=1",
                "group_by": list(hop.group_by),
                "additive_measures": _measure_params(additive),
                "non_additive": hop.unverified_measures,
            },
            reason,
            tuple(hop.group_by),
        )

    if template == "layer/dim_key_coverage":
        if not table or not table.scd2:
            return None, f"{hop.target} is not an SCD2 dimension", ()
        # Declared losses must be subtracted from the source side. A row deliberately
        # filtered or rejected upstream is not a "missing dimension key" — reporting it
        # as one is the chronic false alarm that gets a monitoring tool switched off.
        loss_filter = _loss_predicate(
            _upstream_losses(hop, loaded), list(table.scd2.business_key), alias="src"
        )
        return (
            {
                **base,
                "source_table": hop.source,
                "dim_table": hop.target,
                "key": list(table.scd2.business_key),
                "is_current": table.scd2.is_current or "IS_CURRENT",
                "source_scope_predicate": "1=1",
                "loss_filter_predicate": loss_filter,
            },
            None,
            tuple(table.scd2.business_key),
        )

    if template == "layer/fk_resolves_as_of":
        if not table or not table.foreign_keys:
            return None, f"{hop.target} declares no foreign keys", ()
        fk = table.foreign_keys[0]
        ref_parts = fk.references.rsplit(".", 1)
        dim_table = ref_parts[0]
        dim_key = ref_parts[1] if len(ref_parts) > 1 else "ID"
        dim = loaded.manifest.get(dim_table)
        if not dim or not dim.scd2:
            return None, f"{dim_table} is not an SCD2 dimension, so as-of has no ranges", ()
        return (
            {
                **base,
                "fact_table": hop.target,
                "fact_key": table.grain[0],
                "fk_column": fk.column,
                "event_ts": "PLACED_AT",
                "dim_table": dim_table,
                "dim_key": dim_key,
                "valid_from": dim.scd2.valid_from,
                "valid_to": dim.scd2.valid_to,
                "open_end_sentinel": dim.scd2.open_end_sentinel or "9999-12-31",
            },
            None,
            (),
        )

    if template == "keys/grain_unique":
        if not table:
            return None, f"{hop.target} is not in the manifest", ()
        reason = None if table.activatable else "grain not confirmed by a human (B3/R4)"
        return (
            {**base, "table": hop.target, "grain": list(table.grain)},
            reason,
            tuple(table.grain),
        )

    return None, f"{template}: no parameter mapping", ()


def _compare_columns(hop: HopSpec, loaded: LoadedProject) -> list[str]:
    """Which columns the surviving row must match on.

    Returns [] for `business`, which signals the runner to resolve the real business
    columns from the catalog. Falling back to the dedup key was wrong twice over: the key
    is already selected (so it rendered `CUSTOMER_ID, CUSTOMER_ID`), and comparing keys to
    keys asserts nothing — a wrong-duplicate-kept defect differs in the BUSINESS columns.
    """
    dedup = hop.dedup
    if dedup and isinstance(dedup.compare_columns, list):
        return list(dedup.compare_columns)
    return []


def _loss_predicate(losses: list[DeclaredLoss], key: list[str], *, alias: str = "") -> str:
    """SQL excluding every NAMED loss from a source-side set.

    Both kinds count. A `filter` loss is a predicate; a `reject_table` loss is an
    anti-join against the rows that were quarantined. Handling only filters left rejected
    rows in the expected set, so pick-rule fidelity reported the picked row as missing
    from silver and dimension coverage reported it as a dropped key — both of them
    correct arithmetic on an incomplete definition of "loss".
    """
    prefix = f"{alias}." if alias else ""
    parts: list[str] = []
    for loss in losses:
        if loss.kind == "filter" and loss.predicate:
            parts.append(f" and not ({loss.predicate})")
        elif loss.kind == "reject_table" and loss.table and key:
            joins = " and ".join(f"r.{k} = {prefix}{k}" for k in key)
            parts.append(f" and not exists (select 1 from {loss.table} r where {joins})")
    return "".join(parts)


def _upstream_losses(hop: HopSpec, loaded: LoadedProject) -> list[DeclaredLoss]:
    """Losses declared on THIS hop, plus those on the hop that produced its source.

    A dimension built from bronze inherits bronze->silver's filters: those rows were
    deliberately dropped, so they are not missing keys.
    """
    losses = list(hop.declared_losses)
    for other in loaded.layers.hops:
        if other.id != hop.id and other.source.upper() == hop.source.upper():
            losses.extend(other.declared_losses)
    return losses
