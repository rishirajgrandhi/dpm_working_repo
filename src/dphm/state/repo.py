"""Typed read/write API over DPHM_STATE (04, 08).

The only module that writes runs, results and coverage. Referential integrity lives here
rather than in FK constraints, because Snowflake does not enforce them and declaring
unenforced constraints is exactly the false confidence this product is against (08 §2).
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dphm import __version__
from dphm.util.ids import new_ulid
from dphm.util.logging import get_logger

if TYPE_CHECKING:
    from dphm.engine.result import CheckResult
    from dphm.warehouse.connection import SnowflakeConnector

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


@dataclass
class RunRecord:
    run_id: str
    project: str
    lane: str
    trigger_kind: str
    config_sha: str
    shadow_mode: bool
    scope_predicate: str = ""
    status: str = "RUNNING"


@dataclass
class CoverageRow:
    table_name: str
    table_type: str | None
    layer: str | None
    hop_id: str | None
    hop_relation: str | None
    contract_confirmed: bool
    unverified_measures: list[str]
    checks_active: int
    checks_expected: int
    columns_total: int
    columns_compared: int
    grain_confirmed: bool
    unvalidated: bool


class StateRepo:
    """Writes to DPHM_STATE. Always via the writer role."""

    def __init__(self, connector: SnowflakeConnector, *, schema: str = "DPHM_STATE") -> None:
        self.connector = connector
        self.schema = schema
        self._writer_cm: Any = None
        self._writer: Any = None
        self._reader_cm: Any = None
        self._reader: Any = None

    # A run writes one row per check plus a coverage row per table. Opening a connection
    # per statement cost 3-5 seconds each and dominated the whole run, so the session is
    # held open and reused. `session()` scopes it; without it each call still works,
    # which keeps one-off reads simple.
    def __enter__(self) -> StateRepo:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for cm in (self._writer_cm, self._reader_cm):
            if cm is not None:
                with contextlib.suppress(Exception):
                    cm.__exit__(None, None, None)
        self._writer_cm = self._writer = None
        self._reader_cm = self._reader = None

    def _conn(self, role: str) -> Any:
        if role == "writer":
            if self._writer is None:
                self._writer_cm = self.connector.connect(role="writer")
                self._writer = self._writer_cm.__enter__()
            return self._writer
        if self._reader is None:
            self._reader_cm = self.connector.connect(role="reader")
            self._reader = self._reader_cm.__enter__()
        return self._reader

    def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        cur = self._conn("writer").cursor()
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
        finally:
            cur.close()

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur = self._conn("reader").cursor()
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
            names = [c[0].lower() for c in cur.description]
            return [dict(zip(names, r, strict=False)) for r in cur.fetchall()]
        finally:
            cur.close()

    # ── runs ──────────────────────────────────────────────────────────────────

    def open_run(self, run: RunRecord) -> str:
        self._write(
            f"""
            insert into {self.schema}.RUNS
                (RUN_ID, PROJECT, LANE, TRIGGER_KIND, STARTED_AT, STATUS,
                 SCOPE_PREDICATE, CONFIG_SHA, TOOL_VERSION, SHADOW_MODE)
            select %s, %s, %s, %s, %s::timestamp_ntz, %s, %s, %s, %s, %s
            """,
            (
                run.run_id,
                run.project,
                run.lane,
                run.trigger_kind,
                _now(),
                "RUNNING",
                run.scope_predicate,
                run.config_sha,
                f"dphm/{__version__}",
                run.shadow_mode,
            ),
        )
        return run.run_id

    def close_run(
        self,
        run_id: str,
        *,
        status: str,
        warehouse_seconds: float | None = None,
        notes: str | None = None,
    ) -> None:
        self._write(
            f"""
            update {self.schema}.RUNS
               set ENDED_AT = %s::timestamp_ntz, STATUS = %s,
                   WAREHOUSE_SECONDS = %s, NOTES = %s
             where RUN_ID = %s
            """,
            (_now(), status, warehouse_seconds, notes, run_id),
        )

    def recent_runs(self, project: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._read(
            f"""
            select RUN_ID, LANE, TRIGGER_KIND, STARTED_AT, ENDED_AT, STATUS,
                   SHADOW_MODE, WAREHOUSE_SECONDS, CONFIG_SHA, SCOPE_PREDICATE
              from {self.schema}.RUNS
             where PROJECT = %s
             order by STARTED_AT desc
             limit {int(limit)}
            """,
            (project,),
        )

    # ── results ───────────────────────────────────────────────────────────────

    def write_result(self, result: CheckResult) -> None:
        self._write(
            f"""
            insert into {self.schema}.CHECK_RESULTS
                (RESULT_ID, RUN_ID, CHECK_ID, STATUS, FAILING_ROW_COUNT,
                 EVALUATED_ROW_COUNT, FAILING_FINGERPRINT, SAMPLE_ROWS,
                 COLUMNS_COMPARED, COLUMNS_EXCLUDED, EXCLUSION_REASONS,
                 SCOPE_PREDICATE, SQL_SHA256, QUERY_ID, DURATION_MS,
                 SUPPRESSED_BY, ERROR_MESSAGE, CREATED_AT)
            select %s, %s, %s, %s, %s, %s, %s,
                   parse_json(%s), parse_json(%s), parse_json(%s), parse_json(%s),
                   %s, %s, %s, %s, %s, %s, %s::timestamp_ntz
            """,
            (
                result.result_id,
                result.run_id,
                result.check_id,
                result.status,
                result.failing_row_count,
                result.evaluated_row_count,
                result.failing_fingerprint,
                json.dumps(result.sample_rows, default=str),
                json.dumps(result.columns_compared),
                json.dumps(result.columns_excluded),
                json.dumps(result.exclusion_reasons),
                result.scope_predicate,
                result.sql_sha256,
                result.query_id,
                result.duration_ms,
                result.suppressed_by,
                result.error_message,
                _now(),
            ),
        )

    def results_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._read(
            f"""
            select RESULT_ID, CHECK_ID, STATUS, FAILING_ROW_COUNT, EVALUATED_ROW_COUNT,
                   FAILING_FINGERPRINT, SAMPLE_ROWS, COLUMNS_COMPARED, COLUMNS_EXCLUDED,
                   SCOPE_PREDICATE, DURATION_MS, SUPPRESSED_BY, ERROR_MESSAGE
              from {self.schema}.CHECK_RESULTS
             where RUN_ID = %s
             order by STATUS, CHECK_ID
            """,
            (run_id,),
        )

    # ── check registry ────────────────────────────────────────────────────────

    def upsert_check(
        self,
        *,
        check_id: str,
        project: str,
        lane: str,
        template: str,
        table_name: str,
        relationship: str,
        hop_id: str | None,
        layer: str | None,
        severity: str,
        status: str,
        params: dict[str, Any],
        sql_sha256: str,
        grain_confirmed_by: str | None = None,
    ) -> None:
        # Delete + insert rather than MERGE: a MERGE here needs the parameter list
        # repeated across its matched and not-matched branches, which is easy to get
        # wrong by one and fails at runtime rather than at review.
        self._write(f"delete from {self.schema}.CHECKS where CHECK_ID = %s", (check_id,))
        self._write(
            f"""
            insert into {self.schema}.CHECKS
                (CHECK_ID, PROJECT, LANE, TEMPLATE, TABLE_NAME, RELATIONSHIP, HOP_ID,
                 LAYER, SEVERITY, STATUS, PARAMS, SQL_SHA256, GRAIN_CONFIRMED_BY,
                 CREATED_AT)
            select %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, parse_json(%s), %s, %s,
                   %s::timestamp_ntz
            """,
            (
                check_id,
                project,
                lane,
                template,
                table_name,
                relationship,
                hop_id,
                layer,
                severity,
                status,
                json.dumps(params, default=str),
                sql_sha256,
                grain_confirmed_by,
                _now(),
            ),
        )

    def checks_for_project(self, project: str) -> list[dict[str, Any]]:
        return self._read(
            f"""
            select CHECK_ID, LANE, TEMPLATE, TABLE_NAME, RELATIONSHIP, HOP_ID, LAYER,
                   SEVERITY, STATUS, SQL_SHA256, GRAIN_CONFIRMED_BY,
                   GATE_BUDGET_OK, GATE_PASSES_GOOD, GATE_MUTATION_OK, GATE_DETERMINISTIC
              from {self.schema}.CHECKS
             where PROJECT = %s and RETIRED_AT is null
             order by LAYER, TABLE_NAME, TEMPLATE
            """,
            (project,),
        )

    # ── coverage ──────────────────────────────────────────────────────────────

    def write_coverage(self, run_id: str, project: str, rows: list[CoverageRow]) -> None:
        # One statement per row is fine here: there are as many rows as tables, and the
        # session is already open.
        for row in rows:
            self._write(
                f"""
                insert into {self.schema}.COVERAGE
                    (RUN_ID, PROJECT, TABLE_NAME, TABLE_TYPE, LAYER, HOP_ID, HOP_RELATION,
                     CONTRACT_CONFIRMED, UNVERIFIED_MEASURES, CHECKS_ACTIVE,
                     CHECKS_EXPECTED, COLUMNS_TOTAL, COLUMNS_COMPARED, GRAIN_CONFIRMED,
                     UNVALIDATED, CREATED_AT)
                select %s, %s, %s, %s, %s, %s, %s, %s, parse_json(%s), %s, %s, %s, %s,
                       %s, %s, %s::timestamp_ntz
                """,
                (
                    run_id,
                    project,
                    row.table_name,
                    row.table_type,
                    row.layer,
                    row.hop_id,
                    row.hop_relation,
                    row.contract_confirmed,
                    json.dumps(row.unverified_measures),
                    row.checks_active,
                    row.checks_expected,
                    row.columns_total,
                    row.columns_compared,
                    row.grain_confirmed,
                    row.unvalidated,
                    _now(),
                ),
            )

    def coverage_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._read(
            f"""
            select TABLE_NAME, TABLE_TYPE, LAYER, HOP_ID, HOP_RELATION,
                   CONTRACT_CONFIRMED, UNVERIFIED_MEASURES, CHECKS_ACTIVE,
                   CHECKS_EXPECTED, GRAIN_CONFIRMED, UNVALIDATED
              from {self.schema}.COVERAGE
             where RUN_ID = %s
             order by LAYER, TABLE_NAME
            """,
            (run_id,),
        )

    def latest_run_id(self, project: str) -> str | None:
        rows = self._read(
            f"""
            select RUN_ID from {self.schema}.RUNS
             where PROJECT = %s order by STARTED_AT desc limit 1
            """,
            (project,),
        )
        return str(rows[0]["run_id"]) if rows else None


def new_run_id() -> str:
    return new_ulid()
