"""One normalized metadata model for both engines (03 §2).

The single most valuable thing this module does is fix the identifier-case problem once,
at the boundary. RDS folds unquoted identifiers to lower case; Snowflake folds them to
UPPER. Comparing raw names across the two is the most common source of a spurious
"column missing" (01 §3.3, 02 §4), so every name that crosses this seam is normalized to
upper case and the original is kept alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

Engine = Literal["snowflake", "postgres"]

# Normalized logical types. The canonicalization table in 07 §3 is keyed off these, so
# the mapping from a vendor type name to one of these is decided exactly once, here.
LogicalType = Literal[
    "integer",
    "decimal",
    "float",
    "text",
    "boolean",
    "date",
    "timestamp",
    "timestamp_tz",
    "json",
    "uuid",
    "binary",
    "other",
]

_SNOWFLAKE_TYPES: dict[str, LogicalType] = {
    "NUMBER": "decimal",
    "DECIMAL": "decimal",
    "NUMERIC": "decimal",
    "INT": "integer",
    "INTEGER": "integer",
    "BIGINT": "integer",
    "SMALLINT": "integer",
    "TINYINT": "integer",
    "BYTEINT": "integer",
    "FLOAT": "float",
    "FLOAT4": "float",
    "FLOAT8": "float",
    "DOUBLE": "float",
    "DOUBLE PRECISION": "float",
    "REAL": "float",
    "VARCHAR": "text",
    "CHAR": "text",
    "CHARACTER": "text",
    "STRING": "text",
    "TEXT": "text",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP_NTZ": "timestamp",
    "DATETIME": "timestamp",
    "TIMESTAMP": "timestamp",
    "TIMESTAMP_LTZ": "timestamp_tz",
    "TIMESTAMP_TZ": "timestamp_tz",
    "VARIANT": "json",
    "OBJECT": "json",
    "ARRAY": "json",
    "BINARY": "binary",
    "VARBINARY": "binary",
}

_POSTGRES_TYPES: dict[str, LogicalType] = {
    "smallint": "integer",
    "integer": "integer",
    "bigint": "integer",
    "numeric": "decimal",
    "decimal": "decimal",
    "money": "decimal",
    "real": "float",
    "double precision": "float",
    "character varying": "text",
    "varchar": "text",
    "character": "text",
    "char": "text",
    "text": "text",
    "citext": "text",
    "name": "text",
    "boolean": "boolean",
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp_tz",
    "json": "json",
    "jsonb": "json",
    "uuid": "uuid",
    "bytea": "binary",
}


def normalize_type(engine: Engine, raw_type: str) -> LogicalType:
    """Map a vendor type name onto a logical type. Unknown types become `other`.

    `other` is deliberate rather than a guess: an unrecognised type is excluded from
    comparison and reported as such, which is honest, where guessing a canonicalization
    would produce a silently wrong comparison.
    """
    if engine == "snowflake":
        return _SNOWFLAKE_TYPES.get(raw_type.strip().upper(), "other")
    return _POSTGRES_TYPES.get(raw_type.strip().lower(), "other")


@dataclass(frozen=True)
class ColumnMeta:
    name: str  # NORMALIZED upper-case — the name used for comparison
    raw_name: str  # as the engine reports it — used when quoting for that engine
    ordinal: int
    raw_type: str
    logical_type: LogicalType
    nullable: bool
    precision: int | None = None
    scale: int | None = None
    char_length: int | None = None
    default: str | None = None
    comment: str | None = None
    # Merged in from Snowflake object tags where they exist (13 §2.1).
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_float(self) -> bool:
        """Floats never enter a fingerprint — cross-engine rendering is not identical (07 §3)."""
        return self.logical_type == "float"


@dataclass(frozen=True)
class TableMeta:
    engine: Engine
    database: str | None
    schema: str  # normalized upper-case
    name: str  # normalized upper-case
    raw_schema: str
    raw_name: str
    columns: tuple[ColumnMeta, ...]
    row_count: int | None = None
    bytes_: int | None = None
    kind: Literal["table", "view", "materialized_view", "external"] = "table"
    comment: str | None = None

    @property
    def fqn(self) -> str:
        """Fully qualified, normalized. The key everything else joins on."""
        parts = [p for p in (self.database, self.schema, self.name) if p]
        return ".".join(parts).upper()

    def column(self, name: str) -> ColumnMeta | None:
        target = name.upper()
        return next((c for c in self.columns if c.name == target), None)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


class CatalogReader(Protocol):
    """Both engines behind one seam (03 §2).

    Keeping this a Protocol rather than a base class is what makes a third engine an
    additive implementation instead of a redesign — relevant given Q0c/Q0e in `16`.
    """

    engine: Engine

    def list_tables(self, schemas: Sequence[str]) -> list[TableMeta]: ...
    def read_table(self, fqn: str) -> TableMeta | None: ...
    def ping(self) -> str: ...
