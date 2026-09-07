"""04 rule 6: only state/ and parity/tier2_land.py may hold a DPHM_WRITER connection.

The docs say this rule is "asserted at runtime too". These tests are what makes that
sentence true. Without them the allowlist is a comment.

The real defence is the Snowflake grant itself — DPHM_WRITER has ALL on DPHM_STATE and
DPHM_SCRATCH and nothing anywhere else. This is the second line, and the better error.
"""

from __future__ import annotations

import pytest

from dphm.warehouse.connection import (
    WriterAccessDeniedError,
    assert_writer_allowed,
    query_tag,
)


@pytest.mark.parametrize(
    "module",
    [
        "dphm.state.repo",
        "dphm.state.migrate",
        "dphm.state.incidents",
        "dphm.state.checkpointer",
        "dphm.parity.tier2_land",
        "dphm.engine.mutation",
    ],
)
def test_allowlisted_modules_may_request_a_writer(module: str) -> None:
    assert assert_writer_allowed(module) == module


@pytest.mark.parametrize(
    "module",
    [
        "dphm.engine.execute",
        "dphm.engine.render",
        "dphm.parity.tier0_aggregate",
        "dphm.agents.authoring.nodes",
        "dphm.agents.reporting.nodes",
        "dphm.api.routes.checks",
        "dphm.reporting.slack",
        "dphm.catalog.snowflake",
        "some.third.party",
    ],
)
def test_everything_else_is_refused(module: str) -> None:
    with pytest.raises(WriterAccessDeniedError, match="DPHM_WRITER"):
        assert_writer_allowed(module)


def test_the_check_engine_specifically_cannot_write() -> None:
    """The engine decides pass/fail. It must not be able to change what it is judging."""
    with pytest.raises(WriterAccessDeniedError) as exc:
        assert_writer_allowed("dphm.engine.execute")
    assert "DPHM_READER" in str(exc.value)
    assert "04 rule 6" in str(exc.value)


def test_agents_specifically_cannot_write() -> None:
    """AI is never in the data path: a bad suggestion, never bad data (01 §5)."""
    with pytest.raises(WriterAccessDeniedError):
        assert_writer_allowed("dphm.agents.authoring.graph")


def test_query_tag_shape_is_attributable_per_check() -> None:
    """`dphm:{run_id}:{check_id}` — credits read back per check (12 §5, 13 §4)."""
    assert query_tag("01JB", "scd/x.dim_customer.abc") == "dphm:01JB:scd/x.dim_customer.abc"
    assert query_tag("01JB") == "dphm:01JB"
