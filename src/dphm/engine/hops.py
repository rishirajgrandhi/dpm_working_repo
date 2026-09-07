"""relation -> template bundle. This dict IS "the agent generates the tests" (07B §6.2).

The agent picks the relation and fills the parameters; the bundle is a lookup; the SQL is
a reviewed template. Roughly 85% of layer checks are a pure parameter fill.
"""

from __future__ import annotations

from typing import Final

SCD_SUITE_TEMPLATES: Final[tuple[str, ...]] = (
    "scd/one_current_row_per_key",
    "scd/no_overlapping_ranges",
    "scd/no_gaps_in_history",
    "scd/well_formed_ranges",
    "scd/current_flag_agrees_with_range",
    "scd/no_spurious_versions",
    "scd/tracked_change_created_version",  # the one the product exists for
    "scd/type1_consistent_across_versions",
    "scd/surrogate_key_unique",
    "scd/closed_versions_immutable",
)

HOP_CHECK_BUNDLES: Final[dict[str, tuple[str, ...]]] = {
    "identical": (
        "layer/row_conservation",
        "layer/no_invented_keys",
    ),
    "dedup_of": (
        "layer/dedup_key_unique",
        "layer/dedup_pick_rule_fidelity",
        "layer/dedup_pick_rule_total",
        "layer/row_conservation",
        "layer/no_invented_keys",
        "layer/reject_reasons_closed",
    ),
    "scd2_of": (*SCD_SUITE_TEMPLATES, "layer/dim_key_coverage"),
    "conserved": (
        "keys/grain_unique",
        "layer/fanout_guard",
        "layer/row_conservation",
        "layer/measure_conservation",
        "layer/fk_resolves_as_of",
    ),
    "aggregate_of": (
        "keys/grain_unique",
        "layer/aggregate_of",
    ),
    # Generates NO checks, and one coverage row saying so. An unvalidated hop must never
    # produce a check that would pass by default.
    "unvalidated": (),
    "filtered_from": ("layer/row_conservation", "layer/no_invented_keys"),
}

# Families generated from table_type alone, independent of any hop (05 §3).
TABLE_TYPE_FAMILIES: Final[dict[str, tuple[str, ...]]] = {
    "raw": ("keys/key_not_null",),
    "scd1": ("keys/grain_unique", "keys/key_not_null"),
    "scd2": ("keys/key_not_null",),  # the suite comes from the scd2_of hop
    "fact": ("keys/grain_unique", "keys/key_not_null"),
    "mart": ("keys/grain_unique",),
    "reference": ("keys/grain_unique", "keys/key_not_null"),
}


def expand_hop(relation: str) -> tuple[str, ...]:
    """The templates a hop's relation implies. An unknown relation generates nothing."""
    return HOP_CHECK_BUNDLES.get(relation, ())


def expand_table_type(table_type: str) -> tuple[str, ...]:
    return TABLE_TYPE_FAMILIES.get(table_type, ())
