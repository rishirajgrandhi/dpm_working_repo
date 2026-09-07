"""Project list and detail (12 §3).

Every payload here is assembled from config plus state. The frontend computes nothing:
it renders what the API returns, which is what makes the dashboard, Slack, Jira and the
PR comment all say the same thing (12 §7).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from dphm.api.auth import Viewer
from dphm.api.deps import all_projects, get_project, load_errors

router = APIRouter(tags=["projects"])


class HopSummary(BaseModel):
    id: str
    hop: str
    lane: str
    source: str
    target: str
    relation: str
    contract_confirmed: bool
    # Non-additive measures we cannot recompute. Surfaced because a mart shown green with
    # silently unverified measures is a lie of omission (07B §5.3).
    unverified_measures: list[str] = Field(default_factory=list)


class ProjectSummary(BaseModel):
    project: str
    team: str
    go_live: bool
    shadow_mode: bool
    tables: int
    hop_count: int
    checks_total: int
    checks_active: int
    config_sha: str


class NotCheckedItem(BaseModel):
    """One row of the "what we are not checking" panel (12 §2.1)."""

    subject: str
    reason: str


class ProjectDetail(ProjectSummary):
    # A distinct field name rather than a narrowed override: overriding `hop_count: int`
    # with a list would generate incoherent TypeScript from the OpenAPI schema, and the
    # generated client is a CI gate (14 §6b).
    hops: list[HopSummary] = Field(default_factory=list)
    # Not collapsible and not below the fold in the UI. Coverage never reaches 100%, and
    # a dashboard that shows only green is the failure this product exists to end.
    not_checked: list[NotCheckedItem] = Field(default_factory=list)


@router.get("/projects", response_model=list[ProjectSummary])
async def list_projects(_user: Viewer) -> list[ProjectSummary]:
    out: list[ProjectSummary] = []
    for loaded in all_projects():
        active = sum(1 for c in loaded.checks if c.status == "active")
        out.append(
            ProjectSummary(
                project=loaded.name,
                team=loaded.project.owner.team,
                go_live=loaded.project.owner.go_live,
                shadow_mode=not loaded.project.owner.go_live,
                tables=len(loaded.manifest.tables),
                hop_count=len(loaded.layers.hops),
                checks_total=len(loaded.checks),
                checks_active=active,
                config_sha=loaded.config_sha,
            )
        )
    return out


@router.get("/projects/invalid", response_model=dict[str, str])
async def invalid_projects(_user: Viewer) -> dict[str, str]:
    """Projects whose config failed to load, with the reason.

    Surfaced rather than hidden: a project that silently vanished from the dashboard is
    worse than one shown with its error.
    """
    return load_errors()


@router.get("/projects/{project}", response_model=ProjectDetail)
async def project_detail(project: str, _user: Viewer) -> ProjectDetail:
    from dphm.config.loader import unresolved_confirmations

    loaded = get_project(project)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown project '{project}'"
        )

    hops = [
        HopSummary(
            id=h.id,
            hop=h.hop,
            lane=h.lane,
            source=h.source,
            target=h.target,
            relation=h.relation,
            contract_confirmed=h.contract_confirmed,
            unverified_measures=h.unverified_measures,
        )
        for h in loaded.layers.hops
    ]

    not_checked = [
        NotCheckedItem(subject=item.split(":", 1)[0], reason=item.split(":", 1)[-1].strip())
        for item in unresolved_confirmations(loaded)
    ]
    for table in loaded.manifest.tables:
        if table.lane_b == "unvalidated" and not table.source_table:
            not_checked.append(
                NotCheckedItem(
                    subject=table.name,
                    reason="no RDS counterpart — unvalidated at L1, reported as uncovered",
                )
            )

    active = sum(1 for c in loaded.checks if c.status == "active")
    return ProjectDetail(
        project=loaded.name,
        team=loaded.project.owner.team,
        go_live=loaded.project.owner.go_live,
        shadow_mode=not loaded.project.owner.go_live,
        tables=len(loaded.manifest.tables),
        hop_count=len(hops),
        hops=hops,
        checks_total=len(loaded.checks),
        checks_active=active,
        config_sha=loaded.config_sha,
        not_checked=not_checked,
    )
