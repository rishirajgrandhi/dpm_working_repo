"""Permission fingerprinting (13 §1, risk R7).

At startup the tool hashes its effective grants on both engines and compares to the
previous run. The reason this exists, stated plainly in the design doc:

    A revoked grant looks exactly like a table full of missing rows. Without this, the
    tool's most alarming possible alert is also its most likely false one.

So a grant change is reported as a `PERMISSION_OR_ACCESS_CHANGE` event, and affected
checks become **INCONCLUSIVE, never FAIL** (03 §8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dphm.util.ids import stable_digest
from dphm.util.logging import get_logger

if TYPE_CHECKING:
    from dphm.warehouse.connection import PostgresConnector, SnowflakeConnector

log = get_logger(__name__)

# Privileges that would let the tool change data it is supposed to only observe.
# Finding any of these outside DPHM_STATE/DPHM_SCRATCH is a violation of the product's
# first principle, and we would rather refuse to start than be capable of it.
_FORBIDDEN_PRIVILEGES = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "OWNERSHIP",
        "MODIFY",
        "WRITE",
        # Structural writes. Omitting these was a real gap: a role with CREATE SCHEMA on
        # a database can add objects to it, which is "changing it" by any reading a
        # reviewer would accept — and it is exactly the privilege found inherited through
        # PUBLIC on a live account.
        "CREATE",
        "APPLYBUDGET",
    }
)


def _is_write_privilege(privilege: str) -> bool:
    """Any CREATE * privilege counts, without enumerating Snowflake's whole vocabulary.

    Snowflake adds object types regularly (CREATE MODEL, CREATE AGENT, …). A prefix rule
    fails toward *reporting* a new privilege rather than silently permitting it, which is
    the direction that cannot produce a false clean.
    """
    upper = privilege.upper()
    return upper in _FORBIDDEN_PRIVILEGES or upper.startswith("CREATE ")


@dataclass(frozen=True)
class GrantFingerprint:
    engine: str
    role_name: str
    grants_hash: str
    grants: list[dict[str, Any]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{self.engine}:{self.role_name} ({len(self.grants)} grants)"


@dataclass(frozen=True)
class PermissionViolation:
    """A grant the tool must not hold."""

    engine: str
    role_name: str
    privilege: str
    on_object: str
    # Which role in the hierarchy actually carries it. Often not the role we configured:
    # an inherited privilege is the hard kind to find and the easy kind to overlook.
    via_role: str = ""

    def __str__(self) -> str:
        inherited = (
            f" (inherited via {self.via_role})"
            if self.via_role and self.via_role.upper() != self.role_name.upper()
            else ""
        )
        return (
            f"{self.engine} role {self.role_name} holds {self.privilege} on "
            f"{self.on_object}{inherited}, which is outside DPHM_STATE/DPHM_SCRATCH. "
            "The tool reports on data; it never changes it (01 §5, 13 §1)."
        )


def _read_role_grants(cur: Any, role: str) -> list[dict[str, Any]]:
    """`role` must already have passed `_quote_ident`."""
    cur.execute(f"show grants to role {role}")
    columns = [c[0].upper() for c in cur.description]
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        record = dict(zip(columns, row, strict=False))
        out.append(
            {
                "privilege": str(record.get("PRIVILEGE", "")).upper(),
                "granted_on": str(record.get("GRANTED_ON", "")).upper(),
                "name": str(record.get("NAME", "")),
                "via_role": role.upper(),
            }
        )
    return out


def _quote_ident(name: str) -> str:
    """Reject anything that is not a plain identifier rather than quoting blindly.

    Role names come from config, so this is defence in depth rather than a live risk —
    but a role name is interpolated into SQL here, and SHOW GRANTS takes no bind params.
    """
    if not name.replace("_", "").replace("$", "").isalnum():
        raise ValueError(f"refusing to interpolate an unsafe role name: {name!r}")
    return name.upper()


def fingerprint_snowflake(
    connector: SnowflakeConnector, *, role: str, writable_schemas: frozenset[str]
) -> tuple[GrantFingerprint, list[PermissionViolation]]:
    """Hash the role's EFFECTIVE privileges, including everything it inherits.

    Walking the hierarchy is the whole point. `SHOW GRANTS TO ROLE X` lists only what was
    granted to X directly — it does not list what X inherits through a role it holds
    USAGE on. And **every Snowflake role implicitly inherits PUBLIC**, whose grants
    accumulate over an account's life and are nobody's deliberate decision.

    Found the hard way on a trial account: PUBLIC held USAGE on a role that held
    CREATE SCHEMA on a database, so the reader role could create schemas in a database
    nobody had granted it. A direct-grants-only fingerprint reported that role as clean.
    """
    grants: list[dict[str, Any]] = []
    with connector.connect(role="reader") as conn:
        cur = conn.cursor()
        try:
            # PUBLIC is inherited by every role whether or not anyone said so.
            pending = [role, "PUBLIC"]
            seen: set[str] = set()
            while pending:
                current = pending.pop()
                if current.upper() in seen:
                    continue
                seen.add(current.upper())
                # Validated before the try: an unsafe identifier is a config error and
                # must surface, not be absorbed as a transient privilege problem.
                safe_name = _quote_ident(current)
                try:
                    role_grants = _read_role_grants(cur, safe_name)
                except Exception as exc:
                    # A role we cannot introspect is recorded as such rather than
                    # silently omitted: an unreadable grant is not an absent one.
                    log.warning("role_grants_unreadable", role=current, error=str(exc)[:200])
                    grants.append(
                        {
                            "privilege": "<UNREADABLE>",
                            "granted_on": "ROLE",
                            "name": current.upper(),
                            "via_role": current.upper(),
                        }
                    )
                    continue
                grants.extend(role_grants)
                # Recurse into roles this one can assume.
                for g in role_grants:
                    if g["granted_on"] == "ROLE" and g["privilege"] == "USAGE":
                        pending.append(g["name"])
        finally:
            cur.close()

    violations = [
        PermissionViolation(
            engine="snowflake",
            role_name=role,
            privilege=g["privilege"],
            on_object=g["name"],
            via_role=str(g.get("via_role", role)),
        )
        for g in grants
        if _is_write_privilege(g["privilege"])
        and not _is_own_schema(g["name"], writable_schemas)
        and not _is_snowflake_owned(g["name"])
    ]

    grants.sort(key=lambda g: (g["granted_on"], g["name"], g["privilege"], g["via_role"]))
    return (
        GrantFingerprint(
            engine="snowflake",
            role_name=role,
            grants_hash=stable_digest(grants, length=32),
            grants=grants,
        ),
        violations,
    )


# Snowflake ships these on every account and grants them through PUBLIC. They contain no
# customer data, so a write privilege on one is noise rather than a finding — but they are
# named explicitly rather than pattern-matched, so a real database called
# SNOWFLAKE_SOMETHING still reports.
_SNOWFLAKE_OWNED_DATABASES = frozenset(
    {"SNOWFLAKE", "SNOWFLAKE_SAMPLE_DATA", "SNOWFLAKE_LEARNING_DB"}
)


def _is_snowflake_owned(object_name: str) -> bool:
    return object_name.split(".")[0].upper() in _SNOWFLAKE_OWNED_DATABASES


def _is_own_schema(object_name: str, writable_schemas: frozenset[str]) -> bool:
    upper = object_name.upper()
    return any(
        f".{schema}." in f".{upper}." or upper.endswith(f".{schema}") for schema in writable_schemas
    )


def fingerprint_postgres(
    connector: PostgresConnector, *, user: str
) -> tuple[GrantFingerprint, list[PermissionViolation]]:
    """Assert the RDS role really is read-only (02 §1: `read_only` is asserted, not trusted)."""
    grants: list[dict[str, Any]] = []
    with connector.connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                select table_schema, table_name, privilege_type
                from information_schema.role_table_grants
                where grantee = %s
                order by table_schema, table_name, privilege_type
                """,
                (user,),
            )
            for schema, table, privilege in cur.fetchall():
                grants.append(
                    {
                        "privilege": str(privilege).upper(),
                        "granted_on": "TABLE",
                        "name": f"{schema}.{table}",
                        "via_role": user.upper(),
                    }
                )
        finally:
            cur.close()

    violations = [
        PermissionViolation(
            engine="postgres",
            role_name=user,
            privilege=g["privilege"],
            on_object=g["name"],
            via_role=user,
        )
        for g in grants
        if _is_write_privilege(g["privilege"])
    ]
    return (
        GrantFingerprint(
            engine="postgres",
            role_name=user,
            grants_hash=stable_digest(grants, length=32),
            grants=grants,
        ),
        violations,
    )


def has_changed(current: GrantFingerprint, previous_hash: str | None) -> bool:
    """A first sighting is not a change — there is nothing to compare against yet."""
    return previous_hash is not None and current.grants_hash != previous_hash


@dataclass(frozen=True)
class ViolationGroup:
    """Violations for one object, collapsed.

    A role with broad DDL produces one violation per privilege — 82 of them on a real
    account. Eighty-two near-identical lines is not a report; a genuine single finding
    would be invisible in it. So the display form groups by object and names the
    privileges together.
    """

    engine: str
    role_name: str
    on_object: str
    via_role: str
    privileges: tuple[str, ...]

    def __str__(self) -> str:
        inherited = (
            f" (inherited via {self.via_role})"
            if self.via_role and self.via_role.upper() != self.role_name.upper()
            else ""
        )
        shown = ", ".join(self.privileges[:5])
        if len(self.privileges) > 5:
            shown += f", and {len(self.privileges) - 5} more"
        return (
            f"{self.engine} role {self.role_name} can write to {self.on_object}{inherited}: {shown}"
        )


def group_violations(violations: list[PermissionViolation]) -> list[ViolationGroup]:
    """Collapse per-privilege violations into one entry per object."""
    buckets: dict[tuple[str, str, str, str], list[str]] = {}
    for v in violations:
        key = (v.engine, v.role_name, v.on_object, v.via_role)
        buckets.setdefault(key, []).append(v.privilege)
    return [
        ViolationGroup(
            engine=engine,
            role_name=role_name,
            on_object=on_object,
            via_role=via_role,
            privileges=tuple(sorted(set(privs))),
        )
        for (engine, role_name, on_object, via_role), privs in sorted(buckets.items())
    ]


def summarize(violations: list[PermissionViolation]) -> str:
    """One readable paragraph for the diagnostics panel and the startup log."""
    groups = group_violations(violations)
    if not groups:
        return "no write privileges outside DPHM_STATE/DPHM_SCRATCH"
    lines = [f"{len(violations)} write privileges across {len(groups)} object(s):"]
    lines.extend(f"  - {g}" for g in groups)
    return "\n".join(lines)
