"""PostgreSQL catalog reader — information_schema plus pg_catalog (03 §2).

Confirmed as the RDS engine (Q3). MySQL would be a second implementation of the same
`CatalogReader` protocol; nothing above this module would change.

Row counts come from `pg_class.reltuples`, the planner's estimate, deliberately. A
`count(*)` on a production OLTP table is exactly the kind of query this tool must not
issue (02 §1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dphm.catalog.base import ColumnMeta, TableMeta, normalize_type
from dphm.util.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dphm.warehouse.connection import PostgresConnector

log = get_logger(__name__)

_COLUMNS_SQL = """
select c.table_schema, c.table_name, c.column_name, c.ordinal_position,
       c.data_type, c.is_nullable, c.numeric_precision, c.numeric_scale,
       c.character_maximum_length, c.column_default,
       col_description(pgc.oid, c.ordinal_position::int) as comment,
       t.table_type
from information_schema.columns c
join information_schema.tables t
     on t.table_schema = c.table_schema and t.table_name = c.table_name
left join pg_class pgc
     on pgc.relname = c.table_name
    and pgc.relnamespace = (select oid from pg_namespace where nspname = c.table_schema)
where c.table_schema = any(%s)
order by c.table_schema, c.table_name, c.ordinal_position
"""

# reltuples is an estimate and is -1 on a table that has never been analysed.
_ROWCOUNT_SQL = """
select n.nspname, c.relname,
       case when c.reltuples < 0 then null else c.reltuples::bigint end as est_rows,
       pg_total_relation_size(c.oid) as total_bytes
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = any(%s) and c.relkind in ('r', 'p', 'v', 'm', 'f')
"""

_KIND_MAP = {
    "BASE TABLE": "table",
    "VIEW": "view",
    "FOREIGN": "external",
    "LOCAL TEMPORARY": "table",
}


@dataclass(frozen=True)
class PostgresCatalog:
    connector: PostgresConnector
    engine: str = "postgres"

    def ping(self) -> str:
        with self.connector.connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "select current_user, current_database(), version(), "
                    "current_setting('transaction_read_only')"
                )
                row = cur.fetchone()
            finally:
                cur.close()
        version = str(row[2]).split(" on ")[0]
        return f"user={row[0]} database={row[1]} read_only={row[3]} version={version}"

    def list_tables(self, schemas: Sequence[str]) -> list[TableMeta]:
        if not schemas:
            return []
        # Postgres folds unquoted identifiers to LOWER; accept either case from config.
        lower = [s.lower() for s in schemas]

        with self.connector.connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(_ROWCOUNT_SQL, (lower,))
                stats = {
                    (str(s).upper(), str(n).upper()): (rows, size)
                    for s, n, rows, size in cur.fetchall()
                }

                cur.execute(_COLUMNS_SQL, (lower,))
                grouped: dict[tuple[str, str], list[ColumnMeta]] = {}
                raw_names: dict[tuple[str, str], tuple[str, str]] = {}
                kinds: dict[tuple[str, str], str] = {}
                for row in cur.fetchall():
                    (
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
                        table_type,
                    ) = row
                    key = (str(schema).upper(), str(table).upper())
                    raw_names[key] = (str(schema), str(table))
                    kinds[key] = _KIND_MAP.get(str(table_type).upper(), "table")
                    grouped.setdefault(key, []).append(
                        ColumnMeta(
                            name=str(column).upper(),
                            raw_name=str(column),
                            ordinal=int(ordinal),
                            raw_type=str(data_type),
                            logical_type=normalize_type("postgres", str(data_type)),
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
        for (schema, table), columns in sorted(grouped.items()):
            rows, size = stats.get((schema, table), (None, None))
            raw_schema, raw_table = raw_names[(schema, table)]
            out.append(
                TableMeta(
                    engine="postgres",
                    database=None,  # one database per connection on Postgres
                    schema=schema,
                    name=table,
                    raw_schema=raw_schema,
                    raw_name=raw_table,
                    columns=tuple(sorted(columns, key=lambda c: c.ordinal)),
                    row_count=int(rows) if rows is not None else None,
                    bytes_=int(size) if size is not None else None,
                    kind=kinds.get((schema, table), "table"),  # type: ignore[arg-type]
                )
            )
        return out

    def read_table(self, fqn: str) -> TableMeta | None:
        parts = fqn.split(".")
        if len(parts) != 2:
            raise ValueError(
                f"expected a two-part name schema.table, got {fqn!r}. Postgres has one "
                "database per connection."
            )
        schema, table = (p.upper() for p in parts)
        return next((t for t in self.list_tables([schema]) if t.name == table), None)
