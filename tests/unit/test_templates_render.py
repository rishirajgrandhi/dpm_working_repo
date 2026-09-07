"""Every template must render to SQL that PARSES as Snowflake.

This exists because of a whitespace bug that no amount of reading caught: Jinja's
`trim_blocks` strips the newline after a block tag, so a line ending in `{% endfor %}`
was welded to the next clause — `on s.ID = b.IDwhere 1=1`. It compiled fine as a template
and failed only when Snowflake tried to run it.

Rendering plus parsing every template closes that gap without needing a warehouse.
"""

from __future__ import annotations

import pytest
import sqlglot

from dphm.engine.render import available_templates, render

SAMPLE: dict[str, object] = {
    "table": "DB.SCHEMA.T",
    "silver_table": "DB.SILVER.T",
    "bronze_table": "DB.BRONZE.T",
    "input_table": "DB.BRONZE.T",
    "output_table": "DB.SILVER.T",
    "source_table": "DB.SILVER.T",
    "target_table": "DB.GOLD.T",
    "mart_table": "DB.GOLD.MART",
    "dim_table": "DB.GOLD.DIM",
    "fact_table": "DB.GOLD.FCT",
    "stage_table": "DB.SILVER.T",
    "reject_table": "DB.SILVER.T_REJECT",
    "scope_predicate": "1=1",
    "bronze_scope_predicate": "1=1",
    "silver_scope_predicate": "1=1",
    "source_scope_predicate": "1=1",
    "target_scope_predicate": "1=1",
    "input_scope_predicate": "1=1",
    "output_scope_predicate": "1=1",
    "stage_scope_predicate": "1=1",
    "loss_filter_predicate": "",
    "baseline_table": "DB.DPHM_STATE.SCD_VERSION_HASHES",
    "sample_limit": 20,
    "business_key": ["CUSTOMER_ID"],
    "grain": ["CUSTOMER_ID", "VALID_FROM"],
    "key": ["CUSTOMER_ID"],
    "dedup_key": ["CUSTOMER_ID"],
    "group_by": ["ORDER_DATE", "COUNTRY"],
    "surrogate_key": "CUSTOMER_SK",
    "valid_from": "VALID_FROM",
    "valid_to": "VALID_TO",
    "is_current": "IS_CURRENT",
    "open_end_sentinel": "9999-12-31",
    "tracked_columns": ["NAME", "SEGMENT"],
    "type1_columns": ["PHONE"],
    "compared_columns": ["NAME", "SEGMENT"],
    "compare_columns": ["NAME", "SEGMENT"],
    "stage_loaded_at": "SOURCE_UPDATED_AT",
    "pick_order_by": "UPDATED_AT desc nulls last, ROW_ID desc nulls last",
    "pick_order_columns": ["UPDATED_AT", "ROW_ID"],
    "reason_column": "REJECT_REASON",
    "allowed_reasons": ["NULL_BUSINESS_KEY"],
    "declared_losses": [{"name": "rejected", "count_expr": "select count(*) from DB.S.R"}],
    "hop_id": "l2_x",
    "fanout_max": 1,
    "fact_key": "ORDER_ID",
    "fk_column": "CUSTOMER_SK",
    "dim_key": "CUSTOMER_SK",
    "event_ts": "PLACED_AT",
    "measures": [{"column": "AMOUNT", "source_column": "AMOUNT", "abs_tol": 0.01, "rel_tol": 0.0}],
    "additive_measures": [
        {"column": "REVENUE", "expr": "sum(AMOUNT)", "abs_tol": 0.01, "rel_tol": 0.0}
    ],
    "non_additive": ["AVG_BASKET"],
}


@pytest.mark.parametrize("template", available_templates())
def test_template_renders_and_parses_as_snowflake(template: str) -> None:
    rendered = render(template, dict(SAMPLE))
    try:
        parsed = sqlglot.parse_one(rendered.sql, dialect="snowflake")
    except Exception as exc:  # pragma: no cover - the message is the point
        pytest.fail(f"{template} rendered unparseable SQL: {exc}\n\n{rendered.sql}")
    assert parsed is not None
    assert rendered.sql_sha256


@pytest.mark.parametrize("template", available_templates())
def test_no_clause_is_welded_to_the_previous_line(template: str) -> None:
    """The specific bug: a keyword glued onto an identifier by a stripped newline."""
    sql = render(template, dict(SAMPLE)).sql.lower()
    for keyword in ("where", "group by", "having", "limit", "from", "and ", "or "):
        glued = f"_id{keyword}" if keyword != "and " else "_idand"
        assert glued not in sql, f"{template}: '{keyword}' welded to an identifier"


def test_every_template_is_reachable_from_a_bundle_or_a_table_type() -> None:
    """A template nothing generates is dead code that looks like coverage."""
    from dphm.engine.hops import HOP_CHECK_BUNDLES, TABLE_TYPE_FAMILIES

    referenced = {t for bundle in HOP_CHECK_BUNDLES.values() for t in bundle}
    referenced |= {t for fam in TABLE_TYPE_FAMILIES.values() for t in fam}
    orphans = sorted(set(available_templates()) - referenced)
    assert orphans == [], f"templates no bundle references: {orphans}"


def test_a_missing_parameter_is_an_error_not_an_empty_string() -> None:
    """An empty string spliced into a WHERE clause silently changes what is compared."""
    from dphm.engine.render import TemplateRenderError

    with pytest.raises(TemplateRenderError, match="not supplied"):
        render("keys/grain_unique", {"table": "DB.S.T"})


def test_a_correlated_loss_subquery_references_the_outer_table() -> None:
    """A reject-table anti-join must correlate to the OUTER row.

    Unqualified, Snowflake binds the name to the inner table, making the predicate
    `r.X = r.X` — trivially true, so NOT EXISTS excluded every row and the check reported
    all five silver rows as absent from bronze. The outer table needs an alias.
    """
    sql = render(
        "layer/dedup_pick_rule_fidelity",
        {
            **SAMPLE,
            "loss_filter_predicate": (
                " and not exists (select 1 from DB.S.R r where r.CUSTOMER_ID = src.CUSTOMER_ID)"
            ),
        },
    ).sql
    assert "from DB.BRONZE.T src" in sql
    assert "r.CUSTOMER_ID = src.CUSTOMER_ID" in sql
    # The bug signature: a self-referential comparison inside the subquery.
    assert "r.CUSTOMER_ID = r.CUSTOMER_ID" not in sql


def test_the_loss_predicate_builder_qualifies_the_outer_reference() -> None:
    from dphm.config.models import DeclaredLoss
    from dphm.engine.expand import _loss_predicate

    losses = [
        DeclaredLoss.model_validate(
            {
                "name": "rejected",
                "kind": "reject_table",
                "table": "DB.S.R",
                "reason_column": "REASON",
                "allowed_reasons": ["X"],
            }
        ),
        DeclaredLoss.model_validate(
            {"name": "test", "kind": "filter", "predicate": "IS_TEST = true"}
        ),
    ]
    predicate = _loss_predicate(losses, ["CUSTOMER_ID"], alias="src")
    assert "r.CUSTOMER_ID = src.CUSTOMER_ID" in predicate
    assert "not (IS_TEST = true)" in predicate
    # Without an alias the outer reference is bare, which is the bug.
    bare = _loss_predicate(losses, ["CUSTOMER_ID"])
    assert "r.CUSTOMER_ID = CUSTOMER_ID" in bare


def test_every_template_taking_a_loss_predicate_aliases_its_source_as_src() -> None:
    """The loss predicate correlates to `src`, so the template must define it.

    Two of the three templates were aliased and the third was missed, which produced an
    `invalid identifier SRC.CUSTOMER_ID` at execution. This makes the set self-enforcing
    rather than something to remember.
    """
    from pathlib import Path

    template_dir = Path(__file__).resolve().parents[2] / "src" / "dphm" / "engine" / "templates"
    offenders: list[str] = []
    for path in template_dir.rglob("*.sql.j2"):
        body = path.read_text()
        if "loss_filter_predicate" not in body:
            continue
        # The predicate is applied to a FROM whose table must carry the `src` alias.
        if " src\n" not in body and " src " not in body:
            offenders.append(str(path.relative_to(template_dir)))
    assert offenders == [], (
        f"templates use loss_filter_predicate without aliasing their source as `src`: {offenders}"
    )
