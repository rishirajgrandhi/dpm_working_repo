"""Lane A run orchestration (03 §4).

    load config -> resolve scope -> plan checks -> render -> execute in hop order
      -> record results -> write coverage -> close run

Every step is deterministic. No model is involved anywhere in this path: everything that
decides pass/fail is templated SQL, which is the product's core safety property.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dphm.engine import expand
from dphm.engine import scope as scope_mod
from dphm.engine.columns import ResolvedColumns, classify
from dphm.engine.execute import count_rows, execute_check
from dphm.engine.render import TemplateRenderError, render
from dphm.engine.result import CheckResult
from dphm.state.repo import CoverageRow, RunRecord, StateRepo, new_run_id
from dphm.util.ids import new_ulid
from dphm.util.logging import bind_run, clear_run, get_logger
from dphm.warehouse.connection import SnowflakeConnector

if TYPE_CHECKING:
    from dphm.config.loader import LoadedProject

log = get_logger(__name__)

# Checks execute in hop order so an upstream break is seen before its downstream
# consequences (07B §8).
_LAYER_ORDER = {"bronze": 0, "silver": 1, "gold": 2, None: 3}


@dataclass
class RunSummary:
    run_id: str
    project: str
    status: str
    results: list[CheckResult] = field(default_factory=list)
    uncovered: list[tuple[str, str]] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def counts(self) -> dict[str, int]:
        out = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0, "UNVALIDATED": 0}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "FAIL"]


def run_lane_a(
    loaded: LoadedProject,
    *,
    trigger: str = "ui",
    batch_id: str | None = None,
    persist: bool = True,
    only_runnable: bool = True,
) -> RunSummary:
    """Execute the project's Lane A checks and record everything."""
    started = time.monotonic()
    run_id = new_run_id()
    project = loaded.name
    bind_run(run_id, project=project)
    connector = SnowflakeConnector(loaded.project.target)
    repo = StateRepo(connector, schema=loaded.project.target.state_schema)

    # Resolve scope first: if we cannot tell which rows are safe to read, every check is
    # INCONCLUSIVE rather than a guess (risk R2).
    try:
        resolved_scope = scope_mod.resolve(loaded.project.batch, batch_id=batch_id)
        scope_error: str | None = None
    except scope_mod.ScopeUnresolvableError as exc:
        resolved_scope = scope_mod.FULL
        scope_error = str(exc)
        log.warning("scope_unresolvable", error=scope_error)

    # Catalog read once: which tables carry the batch column, and what columns each has.
    catalog = _read_catalog(connector, loaded)
    batch_capable = frozenset(
        fqn for fqn, cols in catalog.items() if scope_mod.applies_to(cols, loaded.project.batch)
    )

    plan = expand.plan_project(loaded, sample_limit=loaded.project.defaults.sample_limit)
    summary = RunSummary(
        run_id=run_id, project=project, status="RUNNING", uncovered=list(plan.uncovered)
    )

    if persist:
        repo.open_run(
            RunRecord(
                run_id=run_id,
                project=project,
                lane="A",
                trigger_kind=trigger,
                config_sha=loaded.config_sha,
                shadow_mode=not loaded.project.owner.go_live,
                scope_predicate=resolved_scope.predicate,
            )
        )

    ordered = sorted(plan.checks, key=lambda c: (_LAYER_ORDER.get(c.layer, 3), c.template))
    row_counts: dict[str, int | None] = {}

    # ONE reader connection for the whole run. Opening one per check cost 3-5 seconds
    # each — minutes over a full suite — and served no purpose: the checks are all reads
    # against the same warehouse as the same role.
    with connector.connect(role="reader", query_tag=f"dphm:{run_id}") as conn:
        _execute_all(
            ordered,
            conn=conn,
            catalog=catalog,
            loaded=loaded,
            repo=repo,
            summary=summary,
            run_id=run_id,
            project=project,
            resolved_scope=resolved_scope,
            scope_error=scope_error,
            batch_capable=batch_capable,
            row_counts=row_counts,
            persist=persist,
        )

    counts = summary.counts
    summary.status = "FAILED" if counts["FAIL"] else "OK"
    summary.duration_s = time.monotonic() - started

    if persist:
        repo.write_coverage(run_id, project, _coverage_rows(loaded, plan, summary))
        repo.close_run(
            run_id,
            status=summary.status,
            warehouse_seconds=round(summary.duration_s, 3),
            notes=(
                f"{counts['PASS']} pass, {counts['FAIL']} fail, "
                f"{counts['INCONCLUSIVE']} inconclusive, {counts['UNVALIDATED']} unvalidated"
            ),
        )
    repo.close()
    clear_run()
    return summary


def _execute_all(
    ordered: list[expand.PlannedCheck],
    *,
    conn: object,
    catalog: dict[str, list[str]],
    loaded: LoadedProject,
    repo: StateRepo,
    summary: RunSummary,
    run_id: str,
    project: str,
    resolved_scope: scope_mod.Scope,
    scope_error: str | None,
    batch_capable: frozenset[str],
    row_counts: dict[str, int | None],
    persist: bool,
) -> None:
    for planned in ordered:
        if planned.unvalidated_reason:
            # Recorded as UNVALIDATED, never dropped. A silently dropped check is
            # invisible missing coverage.
            result = CheckResult(
                result_id=new_ulid(),
                run_id=run_id,
                check_id=planned.check_id,
                status="UNVALIDATED",
                scope_predicate=resolved_scope.predicate,
                error_message=planned.unvalidated_reason,
                columns_compared=[],
                columns_excluded=[],
            )
            summary.results.append(result)
            if persist:
                repo.write_result(result)
            continue

        columns = _resolve_columns(loaded, planned)
        params = dict(planned.params)
        # Scope only where the table carries the batch column. Elsewhere the table is
        # rebuilt rather than appended, so the whole table IS its completed state —
        # applying the predicate anyway just yields `invalid identifier BATCH_ID`.
        effective = resolved_scope if planned.table.upper() in batch_capable else scope_mod.FULL
        params["scope_predicate"] = effective.predicate
        for key, table_key in (
            ("bronze_scope_predicate", "bronze_table"),
            ("source_scope_predicate", "source_table"),
            ("input_scope_predicate", "input_table"),
            ("output_scope_predicate", "output_table"),
            ("target_scope_predicate", "target_table"),
            ("stage_scope_predicate", "stage_table"),
        ):
            if key in params:
                referenced = str(params.get(table_key, "")).upper()
                params[key] = (
                    resolved_scope.predicate
                    if referenced in batch_capable
                    else scope_mod.FULL.predicate
                )
        if "compare_columns" in params and not params["compare_columns"]:
            # `business` means: everything in the table that is not a key and not an
            # audit column. Resolved from the catalog, because that is the only place the
            # real column list exists.
            params["compare_columns"] = _business_columns(
                catalog, planned.table, loaded, exclude=planned.key_columns
            )
            if not params["compare_columns"]:
                result = CheckResult(
                    result_id=new_ulid(),
                    run_id=run_id,
                    check_id=planned.check_id,
                    status="INCONCLUSIVE",
                    scope_predicate=effective.predicate,
                    error_message=(
                        f"no business columns resolved for {planned.table}; the catalog "
                        "could not be read, so there is nothing to compare"
                    ),
                    columns_compared=[],
                    columns_excluded=[],
                )
                summary.results.append(result)
                if persist:
                    repo.write_result(result)
                continue

        try:
            rendered = render(planned.template, params)
        except TemplateRenderError as exc:
            result = CheckResult(
                result_id=new_ulid(),
                run_id=run_id,
                check_id=planned.check_id,
                status="INCONCLUSIVE",
                scope_predicate=resolved_scope.predicate,
                error_message=str(exc)[:400],
                columns_compared=[],
                columns_excluded=[],
            )
            summary.results.append(result)
            if persist:
                repo.write_result(result)
            continue

        if planned.table not in row_counts:
            row_counts[planned.table] = count_rows(conn, planned.table, effective.predicate)

        result = execute_check(
            conn,
            rendered,
            run_id=run_id,
            check_id=planned.check_id,
            columns=columns,
            scope=effective,
            sample_limit=loaded.project.defaults.sample_limit,
            min_rows_to_evaluate=1,
            evaluated_row_count=row_counts[planned.table],
        )
        if scope_error and result.status == "PASS" and planned.table.upper() in batch_capable:
            # We could not establish the batch boundary, so a pass is not trustworthy.
            result.status = "INCONCLUSIVE"
            result.error_message = scope_error

        summary.results.append(result)
        if persist:
            repo.write_result(result)
            repo.upsert_check(
                check_id=planned.check_id,
                project=project,
                lane=planned.lane,
                template=planned.template,
                table_name=planned.table,
                relationship=planned.relation,
                hop_id=planned.hop_id,
                layer=planned.layer,
                severity=planned.severity,
                # Cannot be `active`: no check may activate before its four gates pass (10).
                status="shadow",
                params=planned.params,
                sql_sha256=rendered.sql_sha256,
            )
        log.info(
            "check_complete",
            check_id=planned.check_id,
            status=result.status,
            failing=result.failing_row_count,
            ms=result.duration_ms,
        )


def _resolve_columns(loaded: LoadedProject, planned: expand.PlannedCheck) -> ResolvedColumns:
    """Which columns this check compares, and which it skips and why (risk R1)."""
    declared: list[str] = []
    for key in (
        "tracked_columns",
        "compare_columns",
        "grain",
        "business_key",
        "dedup_key",
        "group_by",
    ):
        value = planned.params.get(key)
        if isinstance(value, list):
            declared.extend(str(v) for v in value)
    if not declared:
        declared = list(planned.key_columns) or ["*"]
    return classify(
        planned.table,
        sorted(dict.fromkeys(declared)),
        loaded.column_rules,
        key_columns=planned.key_columns,
    )


def _coverage_rows(
    loaded: LoadedProject, plan: expand.Plan, summary: RunSummary
) -> list[CoverageRow]:
    """One row per table. Coverage is the honest half of the report (07B §9)."""
    by_table: dict[str, list[expand.PlannedCheck]] = {}
    for check in plan.checks:
        by_table.setdefault(check.table.upper(), []).append(check)
    status_by_id = {r.check_id: r.status for r in summary.results}
    hop_by_target = {h.target.upper(): h for h in loaded.layers.hops}

    rows: list[CoverageRow] = []
    for table in loaded.manifest.tables:
        checks = by_table.get(table.name.upper(), [])
        hop = hop_by_target.get(table.name.upper())
        # "Active" here means it actually ran and produced a verdict. An UNVALIDATED or
        # INCONCLUSIVE check is NOT coverage.
        ran = sum(1 for c in checks if status_by_id.get(c.check_id) in {"PASS", "FAIL"})
        columns = _resolve_columns(loaded, checks[0]) if checks else ResolvedColumns((), ())
        rows.append(
            CoverageRow(
                table_name=table.name,
                table_type=table.table_type,
                layer=expand._layer_of(table.name),
                hop_id=hop.id if hop else None,
                hop_relation=hop.relation if hop else "unvalidated",
                contract_confirmed=hop.contract_confirmed if hop else False,
                unverified_measures=hop.unverified_measures if hop else [],
                checks_active=ran,
                checks_expected=len(checks),
                columns_total=len(columns.compared) + len(columns.excluded),
                columns_compared=len(columns.compared),
                grain_confirmed=table.activatable,
                unvalidated=ran == 0,
            )
        )
    return rows


def _batch_capable_tables(connector: SnowflakeConnector, loaded: LoadedProject) -> frozenset[str]:
    """Tables that carry the configured batch column, read from the catalog."""
    from dphm.catalog.snowflake import SnowflakeCatalog

    batch = loaded.project.batch
    try:
        tables = SnowflakeCatalog(connector).list_tables(loaded.project.target.schemas)
    except Exception as exc:
        log.warning("catalog_read_failed", error=str(exc)[:200])
        return frozenset()
    return frozenset(t.fqn for t in tables if scope_mod.applies_to(list(t.column_names), batch))


def _read_catalog(connector: SnowflakeConnector, loaded: LoadedProject) -> dict[str, list[str]]:
    """fqn -> column names, read once per run rather than per check."""
    from dphm.catalog.snowflake import SnowflakeCatalog

    try:
        tables = SnowflakeCatalog(connector).list_tables(loaded.project.target.schemas)
    except Exception as exc:
        log.warning("catalog_read_failed", error=str(exc)[:200])
        return {}
    return {t.fqn: list(t.column_names) for t in tables}


def _business_columns(
    catalog: dict[str, list[str]],
    table: str,
    loaded: LoadedProject,
    *,
    exclude: tuple[str, ...] = (),
) -> list[str]:
    """Business columns for a table: not a key, not audit, not derived.

    This is what `compare_columns: business` means. It has to come from the catalog —
    the config declares the KEY, not the full column list, and comparing keys to keys
    would assert nothing at all.
    """
    columns = catalog.get(table.upper())
    if not columns:
        return []
    resolved = classify(table, columns, loaded.column_rules, key_columns=exclude)
    skip = {c.upper() for c in exclude}
    # Drop everything classified as a key, not just the declared ones. Keys are selected
    # separately by the templates, so including them here duplicated a column in the
    # SELECT list — and comparing a key to itself asserts nothing anyway.
    return [
        c
        for c in resolved.compared
        if c.upper() not in skip and resolved.classes.get(c.upper()) != "key"
    ]
