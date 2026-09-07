"""Loading, interpolation, and cross-file validation (05, 04 §4).

The property under test: **validation happens at load, not at run.** A config that cannot
be trusted must never reach the query layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dphm.config.loader import (
    ConfigError,
    load_project,
    scan_for_secret_literals,
    unresolved_confirmations,
)


def test_the_example_project_loads(loaded_project) -> None:
    assert loaded_project.name == "orders"
    assert len(loaded_project.manifest.tables) == 6
    assert {h.id for h in loaded_project.layers.hops} == {
        "l1_customers",
        "l2_customers",
        "l3_dim_customer",
        "l3_fct_order",
        "l3_mart_daily_revenue",
    }


def test_env_references_are_interpolated(loaded_project) -> None:
    assert loaded_project.project.source.host == "rds.test.internal"
    assert loaded_project.project.source.password == "not-a-real-password"


def test_config_sha_changes_when_any_file_changes(write_project) -> None:
    """RUNS.CONFIG_SHA is how a result is attributed to the config that produced it."""
    first = load_project(write_project())
    # A comment-only edit still changes the SHA: the SHA covers file content, so any
    # reviewed change to config is attributable, not just semantic ones.
    second_root = Path(write_project())
    (second_root / "column_rules.yaml").write_text(
        (second_root / "column_rules.yaml").read_text() + "\n# reviewed 2026-09-02\n"
    )
    assert first.config_sha != load_project(second_root).config_sha


def test_missing_env_var_fails_at_load_naming_the_variable(
    write_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DPHM_SF_ACCOUNT", raising=False)
    with pytest.raises(ConfigError, match="DPHM_SF_ACCOUNT"):
        load_project(write_project())


def test_a_secret_literal_is_a_load_time_error(write_project) -> None:
    """13 §3: a secret literal in a YAML file is an error, not a warning."""
    bad = Path(write_project())
    project_yaml = (
        (bad / "project.yaml")
        .read_text()
        .replace("password: ${DPHM_RDS_PASSWORD}", "password: sup3r-s3cret-value")
    )
    (bad / "project.yaml").write_text(project_yaml)
    with pytest.raises(ConfigError, match="secret literal"):
        load_project(bad)


def test_secret_scan_reports_the_path_to_the_offending_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"source\.password"):
        scan_for_secret_literals(
            {"source": {"password": "hunter2hunter2"}}, tmp_path / "project.yaml"
        )


def test_invalid_yaml_names_the_file(write_project) -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_project(write_project(**{"manifest.yaml": "tables: [\n  broken"}))


def test_unknown_key_is_rejected_rather_than_ignored(write_project) -> None:
    """A typo'd key that is silently ignored is how a project believes it excluded a
    column it did not exclude."""
    with pytest.raises(ConfigError, match=r"rgain|Extra inputs"):
        load_project(
            write_project(
                **{
                    "manifest.yaml": (
                        "tables:\n"
                        "  - name: A.B.C\n"
                        "    table_type: fact\n"
                        "    grain: [ID]\n"
                        "    rgain_confirmed_by: someone@acme.com\n"
                    )
                }
            )
        )


# ── cross-file validation ────────────────────────────────────────────────────


def test_a_check_targeting_an_undeclared_table_is_rejected(write_project) -> None:
    root = Path(write_project())
    (root / "checks").mkdir(exist_ok=True)
    (root / "checks" / "orphan.yaml").write_text(
        "- id: orphan.check\n"
        "  lane: A\n"
        "  template: keys/grain_unique\n"
        "  table: ANALYTICS.GOLD.NOT_IN_MANIFEST\n"
    )
    with pytest.raises(ConfigError, match=r"manifest\.yaml does not"):
        load_project(root)


def test_an_unresolved_transform_reference_is_rejected(write_project) -> None:
    """Otherwise a deliberate difference would be reported as a failure."""
    with pytest.raises(ConfigError, match=r"transforms\.yaml does not declare"):
        load_project(write_project(**{"transforms.yaml": "transforms: []\n"}))


def test_scd2_of_hop_requires_its_target_to_be_declared_scd2(write_project) -> None:
    root = Path(write_project())
    # The l3_dim_customer hop declares relation=scd2_of against GOLD.DIM_CUSTOMER.
    # Reclassify that table as a fact and the SCD suite has no business key to build from.
    (root / "manifest.yaml").write_text(
        "tables:\n"
        "  - name: ANALYTICS.GOLD.DIM_CUSTOMER\n"
        "    table_type: fact\n"
        "    grain: [CUSTOMER_ID]\n"
        "  - name: ANALYTICS.SILVER.CUSTOMERS\n"
        "    table_type: scd1\n"
        "    grain: [CUSTOMER_ID]\n"
        "  - name: ANALYTICS.BRONZE.CUSTOMERS\n"
        "    table_type: raw\n"
        "    grain: [CUSTOMER_ID]\n"
        "  - name: ANALYTICS.GOLD.FCT_ORDER\n"
        "    table_type: fact\n"
        "    grain: [ORDER_ID]\n"
        "  - name: ANALYTICS.SILVER.ORDERS\n"
        "    table_type: scd1\n"
        "    grain: [ORDER_ID]\n"
        "  - name: ANALYTICS.GOLD.MART_DAILY_REVENUE\n"
        "    table_type: mart\n"
        "    grain: [ORDER_DATE, COUNTRY]\n"
    )
    with pytest.raises(ConfigError, match="needs table_type=scd2"):
        load_project(root)


def test_unresolved_confirmations_are_returned_not_raised(loaded_project) -> None:
    """A project mid-onboarding must still start, with those checks inactive.

    This list is what the Coverage panel and the "what we are not checking" panel render.
    """
    pending = unresolved_confirmations(loaded_project)
    assert any("grain unconfirmed" in p for p in pending)
    assert any("dedup key/pick rule unconfirmed" in p for p in pending)
    assert any("AVG_BASKET" in p and "UNIQUE_BUYERS" in p for p in pending)


def test_state_schema_cannot_be_monitored(write_project) -> None:
    root = Path(write_project())
    text = (
        (root / "project.yaml")
        .read_text()
        .replace(
            "schemas: [BRONZE, SILVER, GOLD, STAGING, PROD]",
            "schemas: [BRONZE, SILVER, GOLD, DPHM_STATE]",
        )
    )
    (root / "project.yaml").write_text(text)
    with pytest.raises(ConfigError, match="own schemas"):
        load_project(root)
