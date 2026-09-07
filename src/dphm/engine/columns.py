"""Four-way column classification and the comparison projection (05 §2, 06 §2)."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dphm.catalog.base import TableMeta
    from dphm.config.models import ColumnClass, ColumnRules


@dataclass(frozen=True)
class ResolvedColumns:
    """What a check compared, what it skipped, and why. Never optional (risk R1)."""

    compared: tuple[str, ...]
    excluded: tuple[str, ...]
    reasons: dict[str, str] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)
    hashed: tuple[str, ...] = ()

    def as_result_fields(self) -> dict[str, object]:
        return {
            "columns_compared": list(self.compared),
            "columns_excluded": list(self.excluded),
            "exclusion_reasons": dict(self.reasons),
        }


def _matches(name: str, patterns: list[str]) -> bool:
    upper = name.upper()
    return any(fnmatch.fnmatch(upper, p.upper()) for p in patterns)


def classify(
    table: str,
    columns: list[str],
    rules: ColumnRules,
    *,
    key_columns: tuple[str, ...] = (),
) -> ResolvedColumns:
    """Resolve every column. Override > pattern > default `business` (05 §2)."""
    from dphm.config.models import ColumnOverride

    overrides = {}
    for tbl, cols in rules.overrides.items():
        if tbl.upper() == table.upper():
            overrides = {c.upper(): v for c, v in cols.items()}

    compared: list[str] = []
    excluded: list[str] = []
    reasons: dict[str, str] = {}
    classes: dict[str, str] = {}
    hashed: list[str] = []
    patterns = rules.classification

    for raw in columns:
        name = raw.upper()
        override = overrides.get(name)
        cls: ColumnClass
        if isinstance(override, ColumnOverride):
            cls = override.cls
            if override.compare == "hash":
                hashed.append(name)
        elif isinstance(override, str):
            cls = override
        elif name in {k.upper() for k in key_columns}:
            cls = "key"
        elif _matches(name, patterns.audit):
            cls = "audit"
        elif _matches(name, patterns.key):
            cls = "key"
        elif _matches(name, patterns.derived):
            cls = "derived"
        else:
            cls = "business"
        classes[name] = cls

        if cls == "audit":
            excluded.append(name)
            reasons[name] = "audit_pattern"
        elif cls == "derived":
            excluded.append(name)
            reasons[name] = "derived_not_declared"
        else:
            compared.append(name)
            if name in hashed:
                reasons[name] = "pii_hash"

    return ResolvedColumns(
        compared=tuple(compared),
        excluded=tuple(excluded),
        reasons=reasons,
        classes=classes,
        hashed=tuple(hashed),
    )


def classify_table(
    meta: TableMeta, rules: ColumnRules, *, key_columns: tuple[str, ...] = ()
) -> ResolvedColumns:
    return classify(meta.fqn, list(meta.column_names), rules, key_columns=key_columns)
