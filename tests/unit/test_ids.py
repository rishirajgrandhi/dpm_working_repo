"""Identity stability (06 §7, 08 §2, 11 §1)."""

from __future__ import annotations

import time

from dphm.util.ids import check_id, incident_fingerprint, new_ulid, stable_digest


def test_digest_is_invariant_to_key_order() -> None:
    """A config reformat must not orphan a check's history."""
    assert stable_digest({"a": 1, "b": 2}) == stable_digest({"b": 2, "a": 1})


def test_check_id_is_stable_across_cosmetic_edits() -> None:
    a = check_id(
        "scd/one_current_row_per_key",
        "ANALYTICS.GOLD.DIM_CUSTOMER",
        {"business_key": ["CUSTOMER_ID"], "is_current": "IS_CURRENT"},
    )
    b = check_id(
        "scd/one_current_row_per_key",
        "analytics.gold.dim_customer",
        {"is_current": "IS_CURRENT", "business_key": ["CUSTOMER_ID"]},
    )
    assert a == b


def test_check_id_changes_when_a_parameter_changes() -> None:
    a = check_id("scd/x", "T", {"business_key": ["CUSTOMER_ID"]})
    b = check_id("scd/x", "T", {"business_key": ["ACCOUNT_ID"]})
    assert a != b


def test_incident_fingerprint_is_invariant_to_failing_key_order() -> None:
    """Same break, same incident — updated, not re-filed (11 §1)."""
    keys_a = [{"CUSTOMER_ID": 1}, {"CUSTOMER_ID": 2}]
    keys_b = [{"CUSTOMER_ID": 2}, {"CUSTOMER_ID": 1}]
    assert incident_fingerprint("c1", keys_a) == incident_fingerprint("c1", keys_b)


def test_a_new_failing_row_produces_a_new_fingerprint() -> None:
    """New failures always get a fresh look, so a March sign-off cannot hide a July bug."""
    base = [{"CUSTOMER_ID": 1}]
    grown = [{"CUSTOMER_ID": 1}, {"CUSTOMER_ID": 99}]
    assert incident_fingerprint("c1", base) != incident_fingerprint("c1", grown)


def test_fingerprint_is_scoped_to_the_check() -> None:
    keys = [{"CUSTOMER_ID": 1}]
    assert incident_fingerprint("c1", keys) != incident_fingerprint("c2", keys)


def test_ulids_are_unique_and_fixed_width() -> None:
    ids = [new_ulid() for _ in range(500)]
    assert len(set(ids)) == 500
    assert all(len(i) == 26 for i in ids)


def test_ulids_sort_in_time_order_across_milliseconds() -> None:
    """RUN_ID ordering is how "the latest run" is resolved without a timestamp join.

    Within a single millisecond the random suffix makes order arbitrary, which is fine —
    the guarantee we depend on is only across milliseconds.
    """
    first = new_ulid()
    time.sleep(0.005)
    second = new_ulid()
    assert first < second
