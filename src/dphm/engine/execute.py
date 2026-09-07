"""Execute a rendered check and materialize a CheckResult (06 §2)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from dphm.engine.result import CheckResult
from dphm.util.ids import incident_fingerprint, new_ulid
from dphm.util.logging import get_logger

if TYPE_CHECKING:
    from dphm.engine.columns import ResolvedColumns
    from dphm.engine.render import RenderedCheck
    from dphm.engine.scope import Scope

log = get_logger(__name__)


def execute_check(
    conn: Any,
    rendered: RenderedCheck,
    *,
    run_id: str,
    check_id: str,
    columns: ResolvedColumns,
    scope: Scope,
    sample_limit: int = 20,
    min_rows_to_evaluate: int = 1,
    evaluated_row_count: int | None = None,
) -> CheckResult:
    """Run the SQL. Zero rows is a pass; the engine never interprets a scalar.

    An error is INCONCLUSIVE, never FAIL: "the query broke" and "the data is wrong" are
    different findings and must not share a status (03 §8).
    """
    started = time.monotonic()
    query_id: str | None = None
    rows: list[dict[str, Any]] = []

    try:
        cur = conn.cursor()
        try:
            # Tag per check so warehouse credits are attributable (12 §5, 13 §4).
            cur.execute("alter session set query_tag = %s", (f"dphm:{run_id}:{check_id}",))
            cur.execute(rendered.sql)
            query_id = getattr(cur, "sfqid", None)
            names = [c[0].lower() for c in cur.description]
            rows = [dict(zip(names, r, strict=False)) for r in cur.fetchall()]
        finally:
            cur.close()
    except Exception as exc:
        return CheckResult(
            result_id=new_ulid(),
            run_id=run_id,
            check_id=check_id,
            status="INCONCLUSIVE",
            scope_predicate=scope.predicate,
            sql_sha256=rendered.sql_sha256,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_message=f"{type(exc).__name__}: {str(exc).splitlines()[0][:400]}",
            **columns.as_result_fields(),  # type: ignore[arg-type]
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    failing_keys = [r.get("failing_key") for r in rows]
    codes = sorted({str(r.get("violation_code")) for r in rows if r.get("violation_code")})

    # A check that evaluated nothing has ABSTAINED, not passed. Without this, an empty
    # table and a wrong grain both look green — the exact false comfort gate 3 exists to
    # prevent (06 §6).
    if not rows and evaluated_row_count is not None and evaluated_row_count < min_rows_to_evaluate:
        return CheckResult(
            result_id=new_ulid(),
            run_id=run_id,
            check_id=check_id,
            status="INCONCLUSIVE",
            evaluated_row_count=evaluated_row_count,
            scope_predicate=scope.predicate,
            sql_sha256=rendered.sql_sha256,
            query_id=query_id,
            duration_ms=duration_ms,
            error_message=(
                f"evaluated {evaluated_row_count} rows, below min_rows_to_evaluate="
                f"{min_rows_to_evaluate}. Zero violations over zero rows is an abstention, "
                "not a pass."
            ),
            **columns.as_result_fields(),  # type: ignore[arg-type]
        )

    return CheckResult(
        result_id=new_ulid(),
        run_id=run_id,
        check_id=check_id,
        status="FAIL" if rows else "PASS",
        failing_row_count=len(rows),
        evaluated_row_count=evaluated_row_count,
        failing_fingerprint=incident_fingerprint(check_id, failing_keys) if rows else None,
        sample_rows=rows[:sample_limit],
        scope_predicate=scope.predicate,
        sql_sha256=rendered.sql_sha256,
        query_id=query_id,
        duration_ms=duration_ms,
        violation_codes=codes,
        **columns.as_result_fields(),  # type: ignore[arg-type]
    )


def count_rows(conn: Any, table: str, predicate: str) -> int | None:
    """Row count within scope, for the abstention guard. None if it cannot be read.

    Feeds `min_rows_to_evaluate`: a check that returned no violations because it
    evaluated no rows has abstained, not passed (06 §6).
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(f"select count(*) from {table} where {predicate}")
            row = cur.fetchone()
            return int(row[0]) if row else None
        finally:
            cur.close()
    except Exception as exc:
        log.warning("row_count_failed", table=table, error=str(exc)[:200])
        return None
