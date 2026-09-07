"""API contract and RBAC (12 §3, §6; 14 §6b).

14 §6b asks for RBAC contract tests on every mutating endpoint, and specifically that
"Engineer must be rejected on sign-off and contract confirmation". The endpoints those
describe arrive in M3/M6; the role ladder they depend on is tested here now, so the
guarantee is in place before the first endpoint relies on it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dphm.api.auth import Role, role_for_groups


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, example_project_dir) -> TestClient:
    monkeypatch.setenv("DPHM_DEV_AUTH", "1")
    monkeypatch.setenv("DPHM_SKIP_MIGRATIONS", "1")
    monkeypatch.setenv("DPHM_PROJECTS_DIR", str(example_project_dir.parent))
    from dphm.api.main import create_app

    with TestClient(create_app()) as c:
        yield c


# ── the role ladder ──────────────────────────────────────────────────────────


def test_roles_are_ordered_so_dependencies_can_say_at_least() -> None:
    assert Role.VIEWER < Role.ENGINEER < Role.DATA_OWNER < Role.ADMIN


def test_engineer_cannot_sign_off_or_confirm_a_contract() -> None:
    """Risk C1: if the monitoring team can sign off on its own alerts, the board gets
    rubber-stamped green (12 §6, 13 §2b)."""
    assert Role.ENGINEER.value < Role.DATA_OWNER.value
    engineer_can = Role.ENGINEER >= Role.DATA_OWNER
    assert engineer_can is False


def test_unmapped_groups_default_to_viewer_not_engineer() -> None:
    """Read access is harmless; an unmapped group must not acquire the ability to spend."""
    assert role_for_groups(("some-random-group",)) is Role.VIEWER


def test_group_mapping_grants_the_highest_matching_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DPHM_ROLE_MAP", "grp-eng:engineer,grp-owners:data_owner")
    assert role_for_groups(("grp-eng", "grp-owners")) is Role.DATA_OWNER
    assert role_for_groups(("grp-eng",)) is Role.ENGINEER


def test_role_parse_rejects_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        Role.parse("superuser")


# ── auth is required ─────────────────────────────────────────────────────────


def test_endpoints_require_a_session(monkeypatch: pytest.MonkeyPatch, example_project_dir) -> None:
    monkeypatch.delenv("DPHM_DEV_AUTH", raising=False)
    monkeypatch.setenv("DPHM_SKIP_MIGRATIONS", "1")
    monkeypatch.setenv("DPHM_PROJECTS_DIR", str(example_project_dir.parent))
    from dphm.api.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/api/v1/projects").status_code == 401


def test_dev_auth_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev bypass that survives to production makes every sign-off fiction.

    Tested at the dependency level rather than through the app: the startup migration
    guard also refuses in production, and it would fire first, so going through a client
    would not actually exercise this branch.
    """
    from fastapi import HTTPException

    from dphm.api.auth import _dev_principal

    monkeypatch.setenv("DPHM_DEV_AUTH", "1")
    monkeypatch.setenv("DPHM_ENV", "production")
    with pytest.raises(HTTPException) as exc:
        _dev_principal()
    assert exc.value.status_code == 500
    assert "unattributable" in str(exc.value.detail)


def test_dev_auth_grants_an_admin_session_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dphm.api.auth import _dev_principal

    monkeypatch.setenv("DPHM_DEV_AUTH", "1")
    monkeypatch.delenv("DPHM_ENV", raising=False)
    principal = _dev_principal()
    assert principal is not None
    assert principal.role is Role.ADMIN
    assert principal.is_dev_session is True


def test_skipping_migrations_is_refused_in_production(
    monkeypatch: pytest.MonkeyPatch, example_project_dir
) -> None:
    """08 §2: the service refuses to serve on a state store of unknown level."""
    monkeypatch.setenv("DPHM_SKIP_MIGRATIONS", "1")
    monkeypatch.setenv("DPHM_ENV", "production")
    monkeypatch.setenv("DPHM_PROJECTS_DIR", str(example_project_dir.parent))
    from dphm.api.main import StateStoreNotReadyError, create_app

    with (
        pytest.raises(StateStoreNotReadyError, match="Refusing to serve"),
        TestClient(create_app()),
    ):
        pass


# ── payloads ─────────────────────────────────────────────────────────────────


def test_healthz_says_nothing_about_the_warehouse(client: TestClient) -> None:
    """Liveness is not health. Warehouse reachability is /diagnostics."""
    body = client.get("/api/v1/healthz").json()
    assert body["status"] == "ok"
    assert "snowflake" not in str(body).lower()


def test_project_list_reports_shadow_mode(client: TestClient) -> None:
    body = {p["project"]: p for p in client.get("/api/v1/projects").json()}
    assert "orders" in body, "the documentation example project must load"
    orders = body["orders"]
    assert orders["shadow_mode"] is True  # go_live defaults false, for two weeks
    # No check can be active until all four gates pass (10), so this must be zero.
    assert orders["checks_active"] == 0
    # Every project starts in shadow mode; none may quietly be live.
    assert all(p["shadow_mode"] for p in body.values())


def test_project_detail_surfaces_what_is_not_checked(client: TestClient) -> None:
    """12 §2.1: the panel is not collapsible and not below the fold, so the API must
    always supply it — including on a green project."""
    body = client.get("/api/v1/projects/orders").json()
    assert body["not_checked"], "a project with unconfirmed grain must report it"
    reasons = " ".join(item["reason"] for item in body["not_checked"])
    assert "grain unconfirmed" in reasons
    assert "AVG_BASKET" in reasons  # non-additive, cannot be recomputed


def test_project_detail_reports_hop_contract_state(client: TestClient) -> None:
    body = client.get("/api/v1/projects/orders").json()
    hops = {h["id"]: h for h in body["hops"]}
    assert hops["l2_customers"]["relation"] == "dedup_of"
    assert hops["l2_customers"]["contract_confirmed"] is False
    assert hops["l1_customers"]["lane"] == "B"  # the only cross-engine hop
    assert hops["l3_mart_daily_revenue"]["unverified_measures"] == [
        "AVG_BASKET",
        "UNIQUE_BUYERS",
    ]


def test_unknown_project_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/projects/nope").status_code == 404


def test_openapi_schema_is_served_for_client_generation(client: TestClient) -> None:
    """The frontend's types are generated from this, and CI fails if they drift."""
    schema = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/projects/{project}" in schema["paths"]
    assert "ProjectDetail" in schema["components"]["schemas"]
