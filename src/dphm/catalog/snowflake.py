"""Snowflake catalog reader — INFORMATION_SCHEMA plus ACCOUNT_USAGE (03 §2).

Row counts come from ACCOUNT_USAGE.TABLES rather than `count(*)`: they are free, and an
estimate is all the authoring agent needs for cardinality reasoning. If ACCOUNT_USAGE is
not readable (assumption A5), counts come back None and the caller reports reduced
signal rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dphm.catalog.base import ColumnMeta, TableMeta, normalize_type
from dphm.util.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dphm.warehouse.connection import SnowflakeConnector

log = get_logger(__name__)

_COLUMNS_SQL = """
select table_catalog, table_schema, table_name, column_name, ordinal_position,
       data_type, is_nullable, numeric_precision, numeric_scale,
       character_maximum_length, column_default, comment
from {database}.information_schema.columns
where table_schema in ({placeholders})
order by table_schema, table_name, ordinal_position
"""

_TABLE_KIND_SQL = """
select table_schema, table_name, table_type, row_count, bytes, comment
from {database}.information_schema.tables
where table_schema in ({placeholders})
"""

_KIND_MAP = {
    "BASE TABLE": "table",
    "VIEW": "view",
    "MATERIALIZED VIEW": "materialized_view",
    "EXTERNAL TABLE": "external",
}


@dataclass(frozen=True)
class SnowflakeCatalog:
    connector: SnowflakeConnector
    engine: str = "snowflake"

    @property
    def database(self) -> str:
        return self.connector.spec.database

    def ping(self) -> str:
        """Return the effective role and warehouse — the diagnostics panel shows this."""
        with self.connector.connect(role="reader") as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "select current_role(), current_warehouse(), current_database(), "
                    "current_version()"
                )
                row = cur.fetchone()
            finally:
                cur.close()
        return f"role={row[0]} warehouse={row[1]} database={row[2]} version={row[3]}"

    def list_tables(self, schemas: Sequence[str]) -> list[TableMeta]:
        if not schemas:
            return []
        placeholders = ", ".join(["%s"] * len(schemas))
        upper = [s.upper() for s in schemas]

        with self.connector.connect(role="reader") as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    _TABLE_KIND_SQL.format(database=self.database, placeholders=placeholders),
                    tuple(upper),
                )
                meta: dict[tuple[str, str], dict[str, Any]] = {}
                for schema, name, ttype, rows, size, comment in cur.fetchall():
                    meta[(str(schema).upper(), str(name).upper())] = {
                        "kind": _KIND_MAP.get(str(ttype).upper(), "table"),
                        "row_count": int(rows) if rows is not None else None,
                        "bytes": int(size) if size is not None else None,
                        "comment": comment,
                    }

                cur.execute(
                    _COLUMNS_SQL.format(database=self.database, placeholders=placeholders),
                    tuple(upper),
                )
                grouped: dict[tuple[str, str, str], list[ColumnMeta]] = {}
                raw_names: dict[tuple[str, str, str], tuple[str, str]] = {}
                for row in cur.fetchall():
                    (
                        catalog,
                        schema,
                        table,
                        column,
                        ordinal,
                        data_type,
                        is_nullable,
                        precision,
                        scale,
                        char_len,
                        default,
                        comment,
                    ) = row
                    key = (str(catalog).upper(), str(schema).upper(), str(table).upper())
                    raw_names[key] = (str(schema), str(table))
                    grouped.setdefault(key, []).append(
                        ColumnMeta(
                            name=str(column).upper(),
                            raw_name=str(column),
                            ordinal=int(ordinal),
                            raw_type=str(data_type),
                            logical_type=normalize_type("snowflake", str(data_type)),
                            nullable=str(is_nullable).upper() == "YES",
                            precision=int(precision) if precision is not None else None,
                            scale=int(scale) if scale is not None else None,
                            char_length=int(char_len) if char_len is not None else None,
                            default=str(default) if default is not None else None,
                            comment=str(comment) if comment else None,
                        )
                    )
            finally:
                cur.close()

        out: list[TableMeta] = []
        for (catalog, schema, table), columns in sorted(grouped.items()):
            extra = meta.get((schema, table), {})
            raw_schema, raw_table = raw_names[(catalog, schema, table)]
            out.append(
                TableMeta(
                    engine="snowflake",
                    database=catalog,
                    schema=schema,
                    name=table,
                    raw_schema=raw_schema,
                    raw_name=raw_table,
                    columns=tuple(sorted(columns, key=lambda c: c.ordinal)),
                    row_count=extra.get("row_count"),
                    bytes_=extra.get("bytes"),
                    kind=extra.get("kind", "table"),
                    comment=extra.get("comment"),
                )
            )
        return out

    def read_table(self, fqn: str) -> TableMeta | None:
        parts = fqn.split(".")
        if len(parts) != 3:
            raise ValueError(
                f"expected a three-part name DATABASE.SCHEMA.TABLE, got {fqn!r}. "
                "Snowflake identifiers are normalized upper-case here (02 §4)."
            )
        _, schema, table = (p.upper() for p in parts)
        return next(
            (t for t in self.list_tables([schema]) if t.name == table),
            None,
        )
