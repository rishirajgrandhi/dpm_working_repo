"""Runs, results and coverage (12 §2.7, §3)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from dphm.api.auth import Engineer, Viewer
from dphm.api.deps import get_project
from dphm.state.repo import StateRepo
from dphm.warehouse.connection import SnowflakeConnector

router = APIRouter(tags=["runs"])


class RunSummaryOut(BaseModel):
    run_id: str
    lane: str
    trigger_kind: str
    started_at: str | None = None
    ended_at: str | None = None
    status: str
    shadow_mode: bool = True
    warehouse_seconds: float | None = None
    config_sha: str | None = None
    scope_predicate: str | None = None


class ResultOut(BaseModel):
    result_id: str
    check_id: str
    status: str
    failing_row_count: int | None = None
    evaluated_row_count: int | None = None
    # Never optional in the payload: a result that cannot say what it compared is not a
    # pass (risk R1).
    columns_compared: list[str] = Field(default_factory=list)
    columns_excluded: list[str] = Field(default_factory=list)
    scope_predicate: str | None = None
    duration_ms: int | None = None
    suppressed_by: str | None = None
    error_message: str | None = None
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)


class CoverageOut(BaseModel):
    table_name: str
    table_type: str | None = None
    layer: str | None = None
    hop_id: str | None = None
    hop_relation: str | None = None
    contract_confirmed: bool = False
    unverified_measures: list[str] = Field(default_factory=list)
    checks_active: int = 0
    checks_expected: int = 0
    grain_confirmed: bool = False
    unvalidated: bool = False


class RunDetailOut(BaseModel):
    run: RunSummaryOut
    results: list[ResultOut] = Field(default_factory=list)
    coverage: list[CoverageOut] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class RunTriggered(BaseModel):
    run_id: str
    project: str
    status: str
    counts: dict[str, int]
    duration_s: float


def _repo(project: str) -> tuple[StateRepo, Any]:
    loaded = get_project(project)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown project '{project}'"
        )
    connector = SnowflakeConnector(loaded.project.target)
    return StateRepo(connector, schema=loaded.project.target.state_schema), loaded


def _json_list(value: Any) -> list[Any]:
    import json

    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


@router.get("/projects/{project}/runs", response_model=list[RunSummaryOut])
async def list_runs(project: str, _user: Viewer, limit: int = 20) -> list[RunSummaryOut]:
    repo, _ = _repo(project)
    rows = await run_in_threadpool(repo.recent_runs, project, limit)
    return [
        RunSummaryOut(
            run_id=str(r["run_id"]),
            lane=str(r["lane"]),
            trigger_kind=str(r["trigger_kind"]),
            started_at=str(r["started_at"]) if r.get("started_at") else None,
            ended_at=str(r["ended_at"]) if r.get("ended_at") else None,
            status=str(r["status"]),
            shadow_mode=bool(r.get("shadow_mode", True)),
            warehouse_seconds=float(r["warehouse_seconds"])
            if r.get("warehouse_seconds") is not None
            else None,
            config_sha=str(r["config_sha"]) if r.get("config_sha") else None,
            scope_predicate=str(r["scope_predicate"]) if r.get("scope_predicate") else None,
        )
        for r in rows
    ]


@router.get("/projects/{project}/runs/latest", response_model=RunDetailOut)
async def latest_run(project: str, _user: Viewer) -> RunDetailOut:
    """The most recent run with its results and coverage — the Overview payload."""
    repo, _ = _repo(project)
    runs = await run_in_threadpool(repo.recent_runs, project, 1)
    if not runs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"project '{project}' has no runs yet",
        )
    row = runs[0]
    run_id = str(row["run_id"])
    results = await run_in_threadpool(repo.results_for_run, run_id)
    coverage = await run_in_threadpool(repo.coverage_for_run, run_id)

    counts: dict[str, int] = {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 0, "UNVALIDATED": 0}
    out_results: list[ResultOut] = []
    for r in results:
        st = str(r["status"])
        counts[st] = counts.get(st, 0) + 1
        out_results.append(
            ResultOut(
                result_id=str(r["result_id"]),
                check_id=str(r["check_id"]),
                status=st,
                failing_row_count=r.get("failing_row_count"),
                evaluated_row_count=r.get("evaluated_row_count"),
                columns_compared=_json_list(r.get("columns_compared")),
                columns_excluded=_json_list(r.get("columns_excluded")),
                scope_predicate=str(r["scope_predicate"]) if r.get("scope_predicate") else None,
                duration_ms=r.get("duration_ms"),
                suppressed_by=r.get("suppressed_by"),
                error_message=r.get("error_message"),
                sample_rows=_json_list(r.get("sample_rows")),
            )
        )

    return RunDetailOut(
        run=RunSummaryOut(
            run_id=run_id,
            lane=str(row["lane"]),
            trigger_kind=str(row["trigger_kind"]),
            started_at=str(row["started_at"]) if row.get("started_at") else None,
            ended_at=str(row["ended_at"]) if row.get("ended_at") else None,
            status=str(row["status"]),
            shadow_mode=bool(row.get("shadow_mode", True)),
            warehouse_seconds=float(row["warehouse_seconds"])
            if row.get("warehouse_seconds") is not None
            else None,
            config_sha=str(row["config_sha"]) if row.get("config_sha") else None,
            scope_predicate=str(row["scope_predicate"]) if row.get("scope_predicate") else None,
        ),
        results=out_results,
        coverage=[
            CoverageOut(
                table_name=str(c["table_name"]),
                table_type=c.get("table_type"),
                layer=c.get("layer"),
                hop_id=c.get("hop_id"),
                hop_relation=c.get("hop_relation"),
                contract_confirmed=bool(c.get("contract_confirmed")),
                unverified_measures=_json_list(c.get("unverified_measures")),
                checks_active=int(c.get("checks_active") or 0),
                checks_expected=int(c.get("checks_expected") or 0),
                grain_confirmed=bool(c.get("grain_confirmed")),
                unvalidated=bool(c.get("unvalidated")),
            )
            for c in coverage
        ],
        counts=counts,
    )


@router.post("/projects/{project}/runs", response_model=RunTriggered)
async def trigger_run(project: str, user: Engineer, batch_id: str | None = None) -> RunTriggered:
    """Submit a Lane A run. Engineer or above (12 §6)."""
    from dphm.runtime.runner import run_lane_a

    loaded = get_project(project)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown project '{project}'"
        )
    summary = await run_in_threadpool(
        run_lane_a, loaded, trigger="ui", batch_id=batch_id, persist=True
    )
    return RunTriggered(
        run_id=summary.run_id,
        project=project,
        status=summary.status,
        counts=summary.counts,
        duration_s=round(summary.duration_s, 2),
    )


class CheckOut(BaseModel):
    check_id: str
    lane: str
    template: str
    table_name: str
    relationship: str | None = None
    hop_id: str | None = None
    layer: str | None = None
    severity: str
    status: str
    grain_confirmed_by: str | None = None
    gates: dict[str, bool] = Field(default_factory=dict)
    activatable: bool = False


@router.get("/projects/{project}/checks", response_model=list[CheckOut])
async def list_checks(project: str, _user: Viewer) -> list[CheckOut]:
    repo, _ = _repo(project)
    rows = await run_in_threadpool(repo.checks_for_project, project)
    out: list[CheckOut] = []
    for r in rows:
        gates = {
            "budget": bool(r.get("gate_budget_ok")),
            "passes_on_good": bool(r.get("gate_passes_good")),
            "mutation": bool(r.get("gate_mutation_ok")),
            "deterministic": bool(r.get("gate_deterministic")),
        }
        out.append(
            CheckOut(
                check_id=str(r["check_id"]),
                lane=str(r["lane"]),
                template=str(r["template"]),
                table_name=str(r["table_name"]),
                relationship=r.get("relationship"),
                hop_id=r.get("hop_id"),
                layer=r.get("layer"),
                severity=str(r["severity"]),
                status=str(r["status"]),
                grain_confirmed_by=r.get("grain_confirmed_by"),
                gates=gates,
                # No check may activate until all four gates pass (10). Surfaced so the
                # UI can disable Activate with a reason BEFORE the click.
                activatable=all(gates.values()),
            )
        )
    return out
