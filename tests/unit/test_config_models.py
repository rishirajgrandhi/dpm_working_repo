"""Each test here names the documented failure mode its validator exists to prevent.

14 §2 asks for exactly this: "every invalid YAML shape produces a precise error.
Especially: surrogate key inside business key, tracked n type-1 non-empty, lane_b without
source_table, secret literals."
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dphm.config.models import (
    CheckDef,
    DedupContract,
    HopSpec,
    SCD2Spec,
    SourceSpec,
    TableSpec,
    ValidationSpec,
)


def _scd2(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "business_key": ["CUSTOMER_ID"],
        "surrogate_key": "CUSTOMER_SK",
        "valid_from": "VALID_FROM",
        "valid_to": "VALID_TO",
        "is_current": "IS_CURRENT",
        "tracked_columns": ["NAME", "SEGMENT"],
        "type1_columns": ["PHONE"],
    }
    base.update(overrides)
    return base


# ── SCD2: assumption B2, the most common misclassification ───────────────────


def test_surrogate_key_inside_business_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="most common SCD2 misclassification"):
        SCD2Spec.model_validate(
            _scd2(business_key=["CUSTOMER_ID", "CUSTOMER_SK"], surrogate_key="CUSTOMER_SK")
        )


def test_column_cannot_be_both_tracked_and_type1() -> None:
    """Tracked means "change opens a version"; type-1 means "overwrite in place"."""
    with pytest.raises(ValidationError, match="both tracked and type-1"):
        SCD2Spec.model_validate(_scd2(tracked_columns=["NAME"], type1_columns=["NAME"]))


def test_check7_is_reported_ungeneratable_without_a_stage_table() -> None:
    """The suite must not present as complete when check 7 cannot be built (05 §6)."""
    assert SCD2Spec.model_validate(_scd2()).check7_generatable is False
    spec = SCD2Spec.model_validate(
        _scd2(stage_table="ANALYTICS.SILVER.CUSTOMERS", stage_loaded_at="SOURCE_UPDATED_AT")
    )
    assert spec.check7_generatable is True


def test_stage_columns_must_be_declared_together() -> None:
    with pytest.raises(ValidationError, match="must be declared together"):
        SCD2Spec.model_validate(_scd2(stage_table="ANALYTICS.SILVER.CUSTOMERS"))


# ── Grain confirmation: assumption B3 / risk R4 ──────────────────────────────


def test_unconfirmed_grain_is_not_activatable() -> None:
    """A wrong grain yields a check that passes while comparing nothing."""
    table = TableSpec.model_validate({"name": "A.B.C", "table_type": "fact", "grain": ["ID"]})
    assert table.activatable is False
    confirmed = TableSpec.model_validate(
        {
            "name": "A.B.C",
            "table_type": "fact",
            "grain": ["ID"],
            "grain_confirmed_by": "priya@acme.com",
        }
    )
    assert confirmed.activatable is True


def test_lane_b_comparison_requires_a_source_table() -> None:
    with pytest.raises(ValidationError, match="requires source_table"):
        TableSpec.model_validate(
            {
                "name": "A.B.C",
                "table_type": "fact",
                "grain": ["ID"],
                "lane_b": "exact_match",
            }
        )


def test_scd2_table_type_requires_an_scd2_block() -> None:
    with pytest.raises(ValidationError, match="requires an scd2 block"):
        TableSpec.model_validate({"name": "A.B.C", "table_type": "scd2", "grain": ["ID"]})


# ── The dedup contract: 07B §4, the L2 initial condition ─────────────────────


def _pick(*terms: str) -> dict[str, object]:
    return {"order_by": [{"column": t, "direction": "desc", "nulls": "last"} for t in terms]}


def test_a_total_pick_rule_needs_a_tiebreaker() -> None:
    """One ORDER BY term is almost never a total ordering, and L2.6 would fire forever."""
    with pytest.raises(ValidationError, match="needs a tiebreaker"):
        DedupContract.model_validate(
            {"key": ["CUSTOMER_ID"], "pick_rule": _pick("SOURCE_UPDATED_AT")}
        )


def test_ties_may_be_accepted_as_a_reported_finding_instead() -> None:
    """must_be_total=false is the documented escape hatch, not a silent downgrade."""
    contract = DedupContract.model_validate(
        {
            "key": ["CUSTOMER_ID"],
            "pick_rule": {**_pick("SOURCE_UPDATED_AT"), "must_be_total": False},
        }
    )
    assert contract.pick_rule.must_be_total is False


def test_pick_rule_cannot_order_by_the_dedup_key() -> None:
    """Every row in a partition shares the key, so it cannot break a tie within it."""
    with pytest.raises(ValidationError, match="orders by the dedup key itself"):
        DedupContract.model_validate(
            {"key": ["CUSTOMER_ID"], "pick_rule": _pick("CUSTOMER_ID", "SOURCE_ROW_ID")}
        )


def test_unconfirmed_dedup_key_is_not_activatable() -> None:
    """A dedup check on the wrong key finds no duplicates and reports green forever."""
    contract = DedupContract.model_validate(
        {"key": ["CUSTOMER_ID"], "pick_rule": _pick("SOURCE_UPDATED_AT", "SOURCE_ROW_ID")}
    )
    assert contract.activatable is False


def test_pick_rule_renders_deterministic_sql() -> None:
    contract = DedupContract.model_validate(
        {"key": ["CUSTOMER_ID"], "pick_rule": _pick("SOURCE_UPDATED_AT", "SOURCE_ROW_ID")}
    )
    assert contract.pick_rule.order_by_sql == (
        "SOURCE_UPDATED_AT desc nulls last, SOURCE_ROW_ID desc nulls last"
    )


# ── Hops: the lane/hop invariant and the conservation slack term ─────────────


def _hop(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "l2_x",
        "hop": "bronze_to_silver",
        "lane": "A",
        "source": "A.BRONZE.X",
        "target": "A.SILVER.X",
        "relation": "dedup_of",
        "dedup": {
            "key": ["ID"],
            "pick_rule": _pick("UPDATED_AT", "ROW_ID"),
        },
    }
    base.update(overrides)
    return base


def test_dedup_of_requires_a_dedup_block() -> None:
    with pytest.raises(ValidationError, match="requires a dedup block"):
        HopSpec.model_validate(_hop(dedup=None))


def test_l1_must_be_lane_b_and_l2_must_be_lane_a() -> None:
    """L1 is the only cross-engine hop (07B §1)."""
    with pytest.raises(ValidationError, match="must be lane A"):
        HopSpec.model_validate(_hop(lane="B"))
    with pytest.raises(ValidationError, match="must be lane B"):
        HopSpec.model_validate(
            {
                "id": "l1_x",
                "hop": "source_to_bronze",
                "lane": "A",
                "source": "public.x",
                "target": "A.BRONZE.X",
                "relation": "identical",
            }
        )


def test_dedup_collapse_loss_cannot_be_asserted() -> None:
    """A conservation check with an unconstrained slack term can never fail (07B §4.4)."""
    with pytest.raises(ValidationError, match="COMPUTED from the declared dedup key"):
        HopSpec.model_validate(
            _hop(declared_losses=[{"name": "dedup_collapse", "kind": "dedup", "predicate": "1=1"}])
        )


def test_reject_table_needs_a_closed_reason_set() -> None:
    """Without one, L2.8 cannot tell an explained reject from a catch-all bucket."""
    with pytest.raises(ValidationError, match="CLOSED set of allowed_reasons"):
        HopSpec.model_validate(
            _hop(
                declared_losses=[
                    {
                        "name": "rejected",
                        "kind": "reject_table",
                        "table": "A.SILVER.X_REJECT",
                        "reason_column": "REJECT_REASON",
                    }
                ]
            )
        )


def test_identical_hop_cannot_declare_losses() -> None:
    """Bronze is a faithful landing; nothing may be dropped at L1 (07B §3)."""
    with pytest.raises(ValidationError, match="cannot declare losses"):
        HopSpec.model_validate(
            {
                "id": "l1_x",
                "hop": "source_to_bronze",
                "lane": "B",
                "source": "public.x",
                "target": "A.BRONZE.X",
                "relation": "identical",
                "declared_losses": [{"name": "filtered", "kind": "filter", "predicate": "x = 1"}],
            }
        )


def test_non_additive_measures_are_surfaced_as_unverified() -> None:
    """L3.M4 is a reporting obligation, not a SQL check (07B §5.3)."""
    hop = HopSpec.model_validate(
        {
            "id": "l3_mart",
            "hop": "silver_to_gold",
            "lane": "A",
            "source": "A.SILVER.ORDERS",
            "target": "A.GOLD.MART",
            "relation": "aggregate_of",
            "group_by": ["ORDER_DATE"],
            "group_by_confirmed_by": "priya@acme.com",
            "measures": [
                {"column": "REVENUE", "expr": "sum(AMOUNT)", "additive": True},
                {"column": "AVG_BASKET", "expr": "avg(AMOUNT)", "additive": False},
            ],
        }
    )
    assert hop.unverified_measures == ["AVG_BASKET"]
    assert hop.contract_confirmed is True


# ── The activation rule: 10, enforced at load as well as in the API ──────────


def test_active_check_with_a_failed_gate_is_rejected_at_load() -> None:
    with pytest.raises(ValidationError, match="failed gate"):
        CheckDef.model_validate(
            {
                "id": "x",
                "lane": "A",
                "template": "scd/one_current_row_per_key",
                "table": "A.B.C",
                "status": "active",
                "validation": {
                    "budget_ok": True,
                    "passes_on_good": True,
                    "mutation_caught": False,  # gate 3 — non-negotiable
                    "deterministic": True,
                },
            }
        )


def test_failed_gates_are_named_so_the_ui_can_explain_the_refusal() -> None:
    spec = ValidationSpec()
    assert spec.failed_gates() == ["budget", "passes_on_good", "mutation", "determinism"]


def test_acknowledged_nondeterminism_satisfies_gate_4_only() -> None:
    """Gate 4 may be waived and the check downgraded; gate 3 may never be waived (10)."""
    spec = ValidationSpec(
        budget_ok=True,
        passes_on_good=True,
        mutation_caught=True,
        deterministic=False,
        acknowledged_nondeterministic=True,
    )
    assert spec.all_gates_pass is True
    assert (
        ValidationSpec(
            budget_ok=True,
            passes_on_good=True,
            mutation_caught=False,
            acknowledged_nondeterministic=True,
            deterministic=True,
        ).all_gates_pass
        is False
    )


# ── The read-only posture ────────────────────────────────────────────────────


def test_source_read_only_cannot_be_turned_off() -> None:
    with pytest.raises(ValidationError, match="never changes it"):
        SourceSpec.model_validate(
            {
                "host": "h",
                "database": "d",
                "user": "u",
                "password": "${DPHM_RDS_PASSWORD}",
                "schemas": ["public"],
                "read_only": False,
            }
        )
