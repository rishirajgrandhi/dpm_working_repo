"""The onboarding wizard's API (12 §2.9).

Four steps, each independently useful:

  1. POST /connections/test    — verify credentials BEFORE anything is saved
  2. POST /projects            — write the project config and its secrets
  3. GET  /projects/{p}/discover — what tables exist, and what kind each looks like
  4. POST /projects/{p}/monitor  — record which tables to watch

Step 1 exists separately so the UI can turn a button green before the user commits. A
wizard that only tells you the password was wrong after saving everything is a wizard
people abandon.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from dphm.api.auth import Admin, Viewer
from dphm.api.deps import get_project, load_all, projects_root
from dphm.util.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["onboarding"])


# ── step 1: test a connection ─────────────────────────────────────────────────


class SnowflakeTestRequest(BaseModel):
    account: str
    user: str
    private_key_path: str
    warehouse: str = "DPHM_WH"
    database: str
    role: str = "DPHM_READER"


class PostgresTestRequest(BaseModel):
    host: str
    port: int = 5432
    database: str
    user: str
    password: str = ""


class ConnectionTestResult(BaseModel):
    ok: bool
    detail: str
    # What to do about it. A failure message without a next step is a dead end.
    remedy: str | None = None
    schemas: list[str] = Field(default_factory=list)


def _test_snowflake(req: SnowflakeTestRequest) -> ConnectionTestResult:
    import snowflake.connector as sf

    if not Path(req.private_key_path).expanduser().exists():
        return ConnectionTestResult(
            ok=False,
            detail=f"private key not found at {req.private_key_path}",
            remedy=(
                "Generate a key pair and register the public half on the Snowflake user. "
                "See HANDOVER.md §7."
            ),
        )
    try:
        conn = sf.connect(
            account=req.account,
            user=req.user,
            private_key_file=str(Path(req.private_key_path).expanduser()),
            role=req.role,
            warehouse=req.warehouse,
            database=req.database,
            login_timeout=20,
        )
    except Exception as exc:
        message = str(exc).splitlines()[0]
        remedy = "Check the account identifier, user and role."
        if "does not exist or not authorized" in message:
            remedy = (
                f"The role {req.role} does not exist, or this user cannot use it. "
                "Create it, or pick a role the user holds."
            )
        elif "JWT token is invalid" in message or "Private key" in message:
            remedy = (
                "The private key does not match the public key registered on this user. "
                "Re-register the public half."
            )
        return ConnectionTestResult(ok=False, detail=message[:300], remedy=remedy)

    try:
        cur = conn.cursor()
        cur.execute("select current_role(), current_warehouse(), current_database()")
        row = cur.fetchone()
        if row is None:  # pragma: no cover - a SELECT of literals always returns a row
            return ConnectionTestResult(
                ok=False, detail="connected, but the session returned no state"
            )
        role, warehouse, database = row[0], row[1], row[2]
        cur.execute("show schemas in database identifier(%s)", (req.database,))
        columns = [c[0].upper() for c in cur.description]
        idx = columns.index("NAME")
        schemas = sorted(
            str(r[idx]).upper()
            for r in cur.fetchall()
            if str(r[idx]).upper() not in {"INFORMATION_SCHEMA"}
        )
        cur.close()
    except Exception as exc:
        return ConnectionTestResult(
            ok=False,
            detail=f"connected, but could not read schemas: {str(exc).splitlines()[0][:200]}",
            remedy=f"Grant USAGE on database {req.database} to role {req.role}.",
        )
    finally:
        conn.close()

    return ConnectionTestResult(
        ok=True,
        detail=f"role={role} warehouse={warehouse} database={database}",
        schemas=schemas,
    )


def _test_postgres(req: PostgresTestRequest) -> ConnectionTestResult:
    import psycopg

    try:
        conn = psycopg.connect(
            host=req.host,
            port=req.port,
            dbname=req.database,
            user=req.user,
            password=req.password,
            connect_timeout=10,
            options="-c default_transaction_read_only=on",
        )
    except Exception as exc:
        return ConnectionTestResult(
            ok=False,
            detail=str(exc).splitlines()[0][:300],
            remedy=(
                "Check the host, port and credentials, and that this machine can reach the "
                "database (VPC, bastion, or security group)."
            ),
        )
    try:
        cur = conn.cursor()
        cur.execute(
            "select nspname from pg_namespace where nspname not like 'pg_%' "
            "and nspname <> 'information_schema' order by nspname"
        )
        schemas = [str(r[0]) for r in cur.fetchall()]
        cur.execute("select current_setting('transaction_read_only')")
        row = cur.fetchone()
        read_only = row[0] if row else "unknown"
        cur.close()
    finally:
        conn.close()
    return ConnectionTestResult(
        ok=True,
        detail=f"connected, read_only={read_only}",
        schemas=schemas,
    )


@router.post("/connections/test/snowflake", response_model=ConnectionTestResult)
async def test_snowflake(req: SnowflakeTestRequest, _user: Admin) -> ConnectionTestResult:
    """Admin only: it accepts credentials."""
    return await run_in_threadpool(_test_snowflake, req)


@router.post("/connections/test/postgres", response_model=ConnectionTestResult)
async def test_postgres(req: PostgresTestRequest, _user: Admin) -> ConnectionTestResult:
    return await run_in_threadpool(_test_postgres, req)


# ── step 2: create the project ────────────────────────────────────────────────


class CreateProjectRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_\-]+$", min_length=2, max_length=40)
    team: str = "@data-platform"
    slack_channel: str = "#dpl-alerts"
    shadow_channel: str = "#dpl-shadow"
    snowflake: SnowflakeTestRequest
    snowflake_schemas: list[str] = Field(min_length=1)
    snowflake_role_writer: str = "DPHM_WRITER"
    state_schema: str = "DPHM_STATE"
    scratch_schema: str = "DPHM_SCRATCH"
    postgres: PostgresTestRequest | None = None
    postgres_schemas: list[str] = Field(default_factory=lambda: ["public"])
    repo_url: str = ""
    repo_branch: str = "main"
    batch_strategy: Literal["control_table", "audit_column", "hook_only"] = "hook_only"
    batch_control_table: str | None = None
    overwrite: bool = False


class CreateProjectResult(BaseModel):
    project: str
    path: str
    warnings: list[str] = Field(default_factory=list)


def _create(req: CreateProjectRequest) -> CreateProjectResult:
    from dphm.config.writer import (
        PostgresInput,
        ProjectInput,
        ProjectWriteError,
        SnowflakeInput,
        write_project,
    )

    spec = ProjectInput(
        name=req.name,
        team=req.team,
        slack_channel=req.slack_channel,
        shadow_channel=req.shadow_channel,
        snowflake=SnowflakeInput(
            account=req.snowflake.account,
            user=req.snowflake.user,
            private_key_path=req.snowflake.private_key_path,
            database=req.snowflake.database,
            schemas=req.snowflake_schemas,
            warehouse=req.snowflake.warehouse,
            role_reader=req.snowflake.role,
            role_writer=req.snowflake_role_writer,
            state_schema=req.state_schema,
            scratch_schema=req.scratch_schema,
        ),
        postgres=(
            PostgresInput(
                host=req.postgres.host,
                database=req.postgres.database,
                user=req.postgres.user,
                password=req.postgres.password,
                schemas=req.postgres_schemas,
                port=req.postgres.port,
            )
            if req.postgres
            else None
        ),
        repo_url=req.repo_url,
        repo_branch=req.repo_branch,
        batch_strategy=req.batch_strategy,
        batch_control_table=req.batch_control_table,
    )
    try:
        path = write_project(projects_root(), spec, overwrite=req.overwrite)
    except ProjectWriteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    warnings: list[str] = []
    if not req.postgres:
        warnings.append(
            "No source database configured, so source-to-bronze comparison is "
            "unavailable. Everything downstream still runs."
        )
    if not req.repo_url:
        warnings.append(
            "No git repository configured. Commit-bound sign-offs, ownership routing and "
            "staleness detection are unavailable."
        )
    if req.batch_strategy == "hook_only":
        warnings.append(
            "Batch strategy is hook_only, so checks stay INCONCLUSIVE until the load job "
            "posts its batch id. That is deliberate: reading mid-load produces false "
            "failures."
        )
    load_all()  # pick the new project up without a restart
    return CreateProjectResult(project=req.name, path=str(path), warnings=warnings)


@router.post("/projects", response_model=CreateProjectResult, status_code=201)
async def create_project(req: CreateProjectRequest, _user: Admin) -> CreateProjectResult:
    return await run_in_threadpool(_create, req)


@router.delete("/projects/{project}", status_code=204)
async def delete_project(project: str, _user: Admin) -> None:
    """Remove config and secrets. Nothing in the warehouse is touched."""
    from dphm.config.writer import delete_project as remove

    removed = await run_in_threadpool(remove, projects_root(), project)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown project '{project}'"
        )
    load_all()


# ── step 3: discovery ─────────────────────────────────────────────────────────


class DiscoveredTable(BaseModel):
    fqn: str
    columns: list[str] = Field(default_factory=list)
    schema_name: str
    name: str
    kind: str
    row_count: int | None = None
    column_count: int
    # A guess, shown as a guess. The user corrects it; nothing is inferred silently.
    suggested_type: str
    suggested_grain: list[str] = Field(default_factory=list)
    reason: str
    already_monitored: bool = False


class DiscoveryResult(BaseModel):
    project: str
    tables: list[DiscoveredTable] = Field(default_factory=list)
    error: str | None = None


def _suggest(name: str, columns: list[str]) -> tuple[str, list[str], str]:
    """Guess a table's kind from its shape. Deliberately simple and always overridable."""
    upper = {c.upper() for c in columns}
    scd2_markers = {"VALID_FROM", "VALID_TO"} | {"IS_CURRENT"}
    keys = [c for c in columns if c.upper().endswith(("_ID", "_KEY", "_SK", "_CODE"))]

    if {"VALID_FROM", "VALID_TO"} <= upper:
        grain = [k for k in keys if not k.upper().endswith(("_SK", "_KEY"))][:1]
        return (
            "scd2",
            [*grain, "VALID_FROM"] if grain else ["VALID_FROM"],
            "has VALID_FROM and VALID_TO, which is the shape of a history-keeping dimension",
        )
    if name.upper().startswith(("DIM_", "D_")):
        return "scd1", keys[:1], "named like a dimension, with no validity columns"
    if name.upper().startswith(("FCT_", "FACT_", "F_")):
        return "fact", keys[:1], "named like a fact table"
    if name.upper().startswith(("MART_", "AGG_", "RPT_")):
        date_cols = [c for c in columns if "DATE" in c.upper()]
        return "mart", (date_cols[:1] + keys[:1]), "named like an aggregate or report table"
    if scd2_markers & upper:
        return "scd1", keys[:1], "has some validity columns but not a full pair"
    return "raw", keys[:2], "no strong signal; treated as a raw landing table"


def _discover(project: str) -> DiscoveryResult:
    from dphm.catalog.snowflake import SnowflakeCatalog
    from dphm.warehouse.connection import SnowflakeConnector

    loaded = get_project(project)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown project '{project}'"
        )
    monitored = {t.name.upper() for t in loaded.manifest.tables}
    try:
        tables = SnowflakeCatalog(SnowflakeConnector(loaded.project.target)).list_tables(
            loaded.project.target.schemas
        )
    except Exception as exc:
        return DiscoveryResult(
            project=project, error=f"could not read the catalog: {str(exc).splitlines()[0][:250]}"
        )

    out: list[DiscoveredTable] = []
    for meta in tables:
        columns = list(meta.column_names)
        kind, grain, reason = _suggest(meta.name, columns)
        out.append(
            DiscoveredTable(
                fqn=meta.fqn,
                columns=columns,
                schema_name=meta.schema,
                name=meta.name,
                kind=meta.kind,
                row_count=meta.row_count,
                column_count=len(columns),
                suggested_type=kind,
                suggested_grain=grain,
                reason=reason,
                already_monitored=meta.fqn in monitored,
            )
        )
    return DiscoveryResult(project=project, tables=out)


@router.get("/projects/{project}/discover", response_model=DiscoveryResult)
async def discover(project: str, _user: Viewer) -> DiscoveryResult:
    """What is in the warehouse, and what each table looks like."""
    return await run_in_threadpool(_discover, project)


# ── step 4: choose what to monitor ────────────────────────────────────────────


class MonitorTable(BaseModel):
    fqn: str
    # The table's real columns, from discovery. Hop inference needs them to find an
    # ordering column for a dedup rule; without them it was looking in scd2 metadata that
    # raw tables never have, and so never found one.
    columns: list[str] = Field(default_factory=list)
    table_type: Literal["raw", "scd1", "scd2", "fact", "mart", "reference"]
    grain: list[str] = Field(min_length=1)
    # Set when a person confirms the grain. Until then no check on this table can
    # activate, because a wrong grain produces a check that passes while comparing
    # nothing — the most dangerous silent failure there is.
    grain_confirmed_by: str | None = None
    scd2_business_key: list[str] = Field(default_factory=list)
    scd2_surrogate_key: str | None = None
    scd2_valid_from: str | None = None
    scd2_valid_to: str | None = None
    scd2_is_current: str | None = None
    scd2_tracked_columns: list[str] = Field(default_factory=list)


class MonitorRequest(BaseModel):
    tables: list[MonitorTable]
    # Derive the medallion hops from schema names, so layer checks are generated too.
    infer_hops: bool = True


class MonitorResult(BaseModel):
    project: str
    tables_written: int
    hops_written: int
    notes: list[str] = Field(default_factory=list)


_LAYER_ORDER = ("BRONZE", "SILVER", "GOLD")


def _infer_hops(tables: list[MonitorTable]) -> tuple[list[dict[str, Any]], list[str]]:
    """Pair tables across layers by name, and propose a relation for each pair.

    Name matching is a starting point, not a claim. Every contract it proposes is written
    with `key_confirmed_by: null`, so nothing it guesses can activate a check until a
    person has agreed with it.
    """
    notes: list[str] = []
    by_layer: dict[str, dict[str, MonitorTable]] = {layer: {} for layer in _LAYER_ORDER}
    for table in tables:
        parts = table.fqn.split(".")
        if len(parts) != 3:
            continue
        schema = parts[1].upper()
        if schema in by_layer:
            by_layer[schema][parts[2].upper()] = table

    hops: list[dict[str, Any]] = []
    for name, silver in by_layer["SILVER"].items():
        bronze = by_layer["BRONZE"].get(name)
        if not bronze:
            continue
        key = silver.grain[:1] or bronze.grain[:1]
        # Prefer an explicit "updated at"-shaped column, then any timestamp-ish one.
        # The key itself is excluded: every row in a partition shares it, so it cannot
        # resolve a tie. Earlier this looked at scd2 metadata, which a raw bronze table
        # never has — so it never found an ordering column at all.
        key_upper = {k.upper() for k in key}
        candidates = [c for c in bronze.columns if c.upper() not in key_upper]
        order_candidates = [c for c in candidates if "UPDATED" in c.upper()] or [
            c for c in candidates if any(m in c.upper() for m in ("_AT", "TIMESTAMP", "DATE"))
        ]
        if not order_candidates:
            notes.append(
                f"{silver.fqn}: could not find an ordering column for the dedup rule, so "
                "this hop is recorded as `conserved` rather than `dedup_of`. A dedup "
                "contract needs a rule for WHICH duplicate survives; guessing one would "
                "produce a check that passes while asserting nothing."
            )
            hops.append(
                {
                    "id": f"l2_{name.lower()}",
                    "hop": "bronze_to_silver",
                    "lane": "A",
                    "source": bronze.fqn,
                    "target": silver.fqn,
                    "relation": "conserved",
                    "key": key,
                    "declared_losses": [],
                    "strict_conservation": True,
                }
            )
            continue
        # A tiebreaker that is neither the key nor the primary ordering column, or the
        # rule is not total and L2.6 will say so.
        tiebreaker = next(
            (
                c
                for c in candidates
                if c.upper() != order_candidates[0].upper()
                and ("ROW" in c.upper() or c.upper().endswith("_ID"))
            ),
            order_candidates[0],
        )
        hops.append(
            {
                "id": f"l2_{name.lower()}",
                "hop": "bronze_to_silver",
                "lane": "A",
                "source": bronze.fqn,
                "target": silver.fqn,
                "relation": "dedup_of",
                "dedup": {
                    "key": key,
                    "key_confirmed_by": None,
                    "pick_rule": {
                        "order_by": [
                            {"column": order_candidates[0], "direction": "desc", "nulls": "last"},
                            {"column": tiebreaker, "direction": "desc", "nulls": "last"},
                        ],
                        # False, because a name-matched guess is not a total ordering we
                        # can vouch for. Ties become a reported finding rather than a
                        # silent failure.
                        "must_be_total": False,
                    },
                    "compare_columns": "business",
                },
                "declared_losses": [],
                "strict_conservation": True,
            }
        )

    for name, gold in by_layer["GOLD"].items():
        source = _find_upstream(name, by_layer)
        if source is None:
            notes.append(
                f"{gold.fqn}: no upstream table found by name, so it is recorded as "
                "unvalidated — it appears in coverage as something we cannot check, "
                "rather than being quietly omitted."
            )
            continue
        hop: dict[str, Any] = {
            "id": f"l3_{name.lower()}",
            "hop": "silver_to_gold",
            "lane": "A",
            "source": source.fqn,
            "target": gold.fqn,
            "key": gold.grain[:1],
        }
        if gold.table_type == "scd2":
            hop["relation"] = "scd2_of"
        elif gold.table_type == "mart":
            hop |= {
                "relation": "aggregate_of",
                "group_by": gold.grain,
                "group_by_confirmed_by": None,
                "measures": [],
            }
            notes.append(
                f"{gold.fqn}: recorded as an aggregate, but with no measures declared. "
                "Declare which columns are additive to get recomputation checks; a "
                "measure nobody declares is reported as unverified rather than assumed."
            )
        else:
            hop |= {"relation": "conserved", "fanout_max": 1, "measures": []}
        hops.append(hop)

    return hops, notes


def _name_variants(name: str) -> list[str]:
    """Plausible upstream names for a gold table.

    `DIM_CUSTOMER` should find `CUSTOMERS`: stripping the prefix gives the singular, so
    the plural has to be tried too. Without this, every gold table was reported as having
    no upstream — honest, but needlessly unhelpful when the match is obvious.
    """
    stems = {name}
    for prefix in ("DIM_", "FCT_", "FACT_", "MART_", "AGG_", "RPT_", "D_", "F_"):
        if name.startswith(prefix):
            stems.add(name.removeprefix(prefix))
    out: list[str] = []
    for stem in stems:
        out.append(stem)
        out.append(f"{stem}S")  # CUSTOMER -> CUSTOMERS
        if stem.endswith("S"):
            out.append(stem[:-1])  # ORDERS -> ORDER
        if stem.endswith("Y"):
            out.append(f"{stem[:-1]}IES")  # COMPANY -> COMPANIES
    return out


def _find_upstream(name: str, by_layer: dict[str, dict[str, MonitorTable]]) -> MonitorTable | None:
    for candidate in _name_variants(name):
        for layer in ("SILVER", "BRONZE"):
            found = by_layer[layer].get(candidate)
            if found is not None:
                return found
    return None


def _write_monitor(project: str, req: MonitorRequest) -> MonitorResult:
    import yaml

    loaded = get_project(project)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown project '{project}'"
        )
    directory = projects_root() / project

    tables: list[dict[str, Any]] = []
    notes: list[str] = []
    for table in req.tables:
        entry: dict[str, Any] = {
            "name": table.fqn,
            "table_type": table.table_type,
            "grain": table.grain,
            "grain_confirmed_by": table.grain_confirmed_by,
            "lane_b": "unvalidated",
        }
        if table.table_type == "scd2":
            missing = [
                label
                for label, value in (
                    ("business key", table.scd2_business_key),
                    ("surrogate key", table.scd2_surrogate_key),
                    ("valid_from", table.scd2_valid_from),
                    ("valid_to", table.scd2_valid_to),
                    ("tracked columns", table.scd2_tracked_columns),
                )
                if not value
            ]
            if missing:
                notes.append(
                    f"{table.fqn}: declared scd2 but missing {', '.join(missing)}, so it is "
                    "recorded as scd1 instead. The 11-check history suite needs all of "
                    "them; generating a partial suite would look like coverage it is not."
                )
                entry["table_type"] = "scd1"
            else:
                entry["scd2"] = {
                    "business_key": table.scd2_business_key,
                    "surrogate_key": table.scd2_surrogate_key,
                    "valid_from": table.scd2_valid_from,
                    "valid_to": table.scd2_valid_to,
                    "is_current": table.scd2_is_current,
                    "tracked_columns": table.scd2_tracked_columns,
                    "type1_columns": [],
                    "open_end_sentinel": "9999-12-31",
                }
        tables.append(entry)
        if not table.grain_confirmed_by:
            notes.append(
                f"{table.fqn}: grain not confirmed, so its checks are recorded but cannot activate."
            )

    (directory / "manifest.yaml").write_text(
        "# Written by the onboarding wizard. `grain_confirmed_by` must be a real person\n"
        "# before any check on a table can activate.\n"
        + yaml.safe_dump({"tables": tables}, sort_keys=False, width=100)
    )

    hops: list[dict[str, Any]] = []
    if req.infer_hops:
        hops, hop_notes = _infer_hops(req.tables)
        notes.extend(hop_notes)
        layers = {
            layer.lower(): {
                "database": loaded.project.target.database,
                "schema": layer,
            }
            for layer in _LAYER_ORDER
            if layer in {t.fqn.split(".")[1].upper() for t in req.tables if "." in t.fqn}
        }
        (directory / "layers.yaml").write_text(
            "# Hops inferred from schema names by the wizard. Every contract it proposes\n"
            "# is unconfirmed, so nothing it guessed can activate a check.\n"
            + yaml.safe_dump({"layers": layers, "hops": hops}, sort_keys=False, width=100)
        )

    load_all()
    reloaded = get_project(project)
    if reloaded is None:
        errors = ", ".join(
            f"{k}: {v}"
            for k, v in __import__("dphm.api.deps", fromlist=["load_errors"]).load_errors().items()
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"the written config did not load: {errors}",
        )
    return MonitorResult(
        project=project, tables_written=len(tables), hops_written=len(hops), notes=notes
    )


@router.post("/projects/{project}/monitor", response_model=MonitorResult)
async def set_monitored(project: str, req: MonitorRequest, _user: Admin) -> MonitorResult:
    """Record which tables to watch, and infer the medallion hops between them."""
    return await run_in_threadpool(_write_monitor, project, req)
