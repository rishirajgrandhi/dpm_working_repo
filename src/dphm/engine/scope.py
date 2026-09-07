"""Completed-batch predicate resolution (06 §5).

Never falls back to wall-clock. An unresolvable scope yields INCONCLUSIVE, because
guessing produces false failures and false failures are how a monitoring tool loses its
audience (risk R2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dphm.config.models import BatchSpec


_IDENT = re.compile(r"^[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*){0,2}$")


def _ident(name: str, *, field: str) -> str:
    """Validate an identifier before it is interpolated into SQL.

    The predicate is built by string composition because the batch column and control
    table are identifiers, and identifiers cannot be bind parameters. So each one is
    checked against a strict pattern instead — config is trusted input, but "trusted"
    should still be verified where the cost is one regex.
    """
    if not _IDENT.match(name):
        raise ScopeUnresolvableError(
            f"batch.{field}={name!r} is not a valid identifier. It is interpolated into "
            "the scope predicate, so it must be a plain name."
        )
    return name


def _literal(value: str, *, field: str) -> str:
    if "'" in value or "\\" in value:
        raise ScopeUnresolvableError(
            f"batch.{field}={value!r} contains a quote or backslash and cannot be used as "
            "a SQL literal."
        )
    return value


class ScopeUnresolvableError(RuntimeError):
    """We cannot tell which rows are safe to read. The check is INCONCLUSIVE."""


@dataclass(frozen=True)
class Scope:
    predicate: str
    strategy: str
    detail: str = ""

    @property
    def is_full_table(self) -> bool:
        return self.predicate.strip() in {"1=1", "true"}


FULL = Scope(predicate="1=1", strategy="full", detail="whole table")


def resolve(batch: BatchSpec, *, batch_id: str | None = None, table_alias: str = "") -> Scope:
    """Build the predicate for this run, or raise."""
    prefix = f"{table_alias}." if table_alias else ""

    if batch.strategy == "hook_only":
        if batch_id is None:
            raise ScopeUnresolvableError(
                "batch.strategy=hook_only but no batch_id was supplied. The load job must "
                "POST its batch id to /hooks/load-complete. Without it we cannot tell a "
                "finished load from one in progress, so the check is INCONCLUSIVE rather "
                "than a guess (assumption A4, risk R2)."
            )
        return Scope(
            predicate=(
                f"{prefix}{_ident(batch.batch_id_column, field='batch_id_column')} = "
                f"'{_literal(batch_id, field='batch_id')}'"
            ),
            strategy="hook_only",
            detail=f"batch {batch_id} as reported by the load hook",
        )

    if batch.strategy == "control_table":
        if not batch.control_table:
            raise ScopeUnresolvableError("batch.strategy=control_table needs control_table")
        batch_col = _ident(batch.batch_id_column, field="batch_id_column")
        control = _ident(batch.control_table, field="control_table")
        status_col = _ident(batch.status_column, field="status_column")
        done_col = _ident(batch.completed_at_column, field="completed_at_column")
        done_val = _literal(batch.completed_value, field="completed_value")
        # Every identifier above passed _ident(). SQL identifiers cannot be bind
        # parameters, so validated interpolation is the only option here.
        predicate = (
            f"{prefix}{batch_col} in ("
            f"select {batch_col} from {control} "
            f"where {status_col} = '{done_val}' "
            f"and {done_col} >= "
            f"dateadd('hour', -{int(batch.lookback_hours)}, current_timestamp()))"
        )
        return Scope(
            predicate=predicate,
            strategy="control_table",
            detail=(
                f"batches marked {batch.completed_value} in {batch.control_table} "
                f"within {batch.lookback_hours}h"
            ),
        )

    if batch.strategy == "audit_column":
        return Scope(
            predicate=(f"{prefix}_LOADED_AT < dateadd('hour', -1, current_timestamp())"),
            strategy="audit_column",
            detail="rows loaded more than an hour ago (audit-column fallback)",
        )

    raise ScopeUnresolvableError(f"unknown batch strategy {batch.strategy!r}")


def applies_to(columns: list[str], batch: BatchSpec) -> bool:
    """Does this table carry the batch column the predicate needs?

    Not every layer does. Bronze usually carries a batch id; silver and gold often do
    not, because they are rebuilt rather than appended. Applying the predicate anyway
    produces `invalid identifier BATCH_ID` and an INCONCLUSIVE check — which is honest
    but useless, when the right answer is to read the whole (already-complete) table.
    """
    upper = {c.upper() for c in columns}
    return batch.batch_id_column.upper() in upper
