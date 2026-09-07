"""Destructive-statement guard.

The product's first principle is that it reports on data and never changes it. In normal
operation the Snowflake GRANT enforces that: `DPHM_READER` simply cannot write. But while
*building* the tool, the operator holds elevated access, and at that point the only thing
standing between a typo and someone's production database is care.

This module is that guarantee expressed as code rather than as care.

Two design choices worth stating, because both cost something:

**It fails closed.** If a statement is destructive and we cannot determine with confidence
what it targets, it is REFUSED. A guard that permits what it cannot parse is not a guard —
and an unparseable DROP is exactly the case where being wrong is unrecoverable.

**It applies regardless of role.** Holding ACCOUNTADMIN does not exempt a statement. The
role determines what Snowflake *would* allow; this determines what we are *willing to ask
for*. Elevated access exists for provisioning, not for reaching outside the sandbox.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from dphm.util.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

log = get_logger(__name__)

# Objects the tool owns and may destroy. Anything else is someone's data.
DEFAULT_WRITABLE_ROOTS: frozenset[str] = frozenset(
    {
        "DPHM_TEST",  # the dev sandbox
        "DPHM_STATE",  # the state store
        "DPHM_SCRATCH",  # transient parity landings and mutation clones
    }
)

# Statements that destroy or overwrite. CREATE OR REPLACE is included deliberately: it
# drops the existing object, and "create" in the name makes that easy to forget.
_DESTRUCTIVE = re.compile(
    r"""^\s*(?:
          (?P<drop>drop|undrop)\s+(?:table|schema|database|view|stage|task|stream|pipe
                                     |materialized\s+view|dynamic\s+table|function
                                     |procedure|sequence|role|user|warehouse|share)
        | (?P<truncate>truncate)(?:\s+table)?
        | (?P<delete>delete\s+from)
        | (?P<update>update)
        | (?P<replace>create\s+or\s+replace\s+(?:table|view|schema|database|materialized\s+view
                                                 |dynamic\s+table|stage|task|stream))
        | (?P<alter>alter\s+(?:table|schema|database|warehouse|account|user|role))
        | (?P<insert>insert\s+(?:into|overwrite))
        | (?P<merge>merge\s+into)
        | (?P<copy>copy\s+into)
        | (?P<grant>grant|revoke)
        | (?P<createuser>create\s+(?:or\s+replace\s+)?(?:user|role|warehouse|share
                                                        |resource\s+monitor|network\s+policy))
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)

# Account-level statements. These act on identities, roles, compute or the account
# itself — objects with no database to check a name against — so object-name checking
# cannot make them safe. They are refused unless the CALL SITE passes
# allow_account_level=True, which keeps provisioning possible while never letting an
# account-level change happen as a side effect.
_ACCOUNT_LEVEL = re.compile(
    r"""^\s*(?:
          alter\s+account
        | (?:create|alter|drop|undrop)\s+(?:or\s+replace\s+)?
              (?:user|role|database\s+role|share|warehouse|resource\s+monitor
                 |network\s+policy|integration|account)
        | (?:grant|revoke)\b
        | (?:un)?set\s+password
        | use\s+(?:role|secondary\s+roles)
    )\b""",
    re.IGNORECASE | re.VERBOSE,
)


class UnsafeStatementError(RuntimeError):
    """A statement was refused before it reached the warehouse."""


# The TARGET of each destructive form — the object actually being written or destroyed.
# Sources are reads and are deliberately not checked: the mutation gate must be able to
# clone FROM production INTO scratch, which is a read of production and a write of ours.
_TARGET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:drop|undrop)\s+(?:table|schema|database|view|stage|task|stream|pipe"
        r"|materialized\s+view|dynamic\s+table|function|procedure|sequence)"
        r"(?:\s+if\s+exists)?\s+(?P<target>[\w$.\"]+)",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*truncate(?:\s+table)?(?:\s+if\s+exists)?\s+(?P<target>[\w$.\"]+)", re.I),
    re.compile(r"^\s*delete\s+from\s+(?P<target>[\w$.\"]+)", re.IGNORECASE),
    re.compile(r"^\s*update\s+(?P<target>[\w$.\"]+)", re.IGNORECASE),
    re.compile(r"^\s*insert\s+(?:into|overwrite\s+into)\s+(?P<target>[\w$.\"]+)", re.I),
    re.compile(r"^\s*merge\s+into\s+(?P<target>[\w$.\"]+)", re.IGNORECASE),
    re.compile(r"^\s*copy\s+into\s+(?P<target>[\w$.\"]+)", re.IGNORECASE),
    re.compile(
        r"^\s*create\s+(?:or\s+replace\s+)?(?:transient\s+|temporary\s+)?"
        r"(?:table|view|schema|database|materialized\s+view|dynamic\s+table|stage|task|stream)"
        r"(?:\s+if\s+not\s+exists)?\s+(?P<target>[\w$.\"]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*alter\s+(?:table|schema|database|warehouse|user|role)"
        r"(?:\s+if\s+exists)?\s+(?P<target>[\w$.\"]+)",
        re.IGNORECASE,
    ),
)


def target_object(sql: str) -> str | None:
    """The object a destructive statement writes to or destroys, or None if unclear."""
    for pattern in _TARGET_PATTERNS:
        match = pattern.match(sql)
        if match:
            return match.group("target").replace('"', "")
    return None


def _target_root(sql: str) -> str | None:
    """The top-level database (or schema, when unqualified) of the write target.

    An UNQUALIFIED target returns its own name, which will not be in the allowlist unless
    it happens to be DPHM_STATE or DPHM_SCRATCH. That is intentional: `drop table
    CUSTOMERS` depends on session context this guard cannot see, so it is refused and the
    caller is told to qualify it.
    """
    target = target_object(sql)
    if target is None:
        return None
    return target.split(".")[0].upper()


def is_destructive(sql: str) -> str | None:
    """Return the kind of destructive statement, or None."""
    match = _DESTRUCTIVE.match(sql)
    if not match:
        return None
    return next((name for name, value in match.groupdict().items() if value), "unknown")


def guard_statement(
    sql: str,
    *,
    writable_roots: Iterable[str] = DEFAULT_WRITABLE_ROOTS,
    allow_account_level: bool = False,
    context: str = "",
) -> None:
    """Raise `UnsafeStatementError` unless this statement is safe to send.

    `allow_account_level` must be set explicitly, per call, for provisioning work —
    creating a role, a warehouse, a resource monitor. It is never a default and never
    session-wide, so an account-level change is always a deliberate line of code.
    """
    allowed = {r.upper() for r in writable_roots}
    statement = sql.strip()
    where = f" ({context})" if context else ""

    if _ACCOUNT_LEVEL.match(statement):
        if not allow_account_level:
            raise UnsafeStatementError(
                f"refused an account-level statement{where}: {statement[:120]!r}\n"
                "These act on identities, roles, privileges, compute, or the account — "
                "objects with no database name to verify — so an object check cannot make "
                "them safe. Pass allow_account_level=True at the call site if this is "
                "deliberate provisioning."
            )
        # Logged at warning level: an account-level change should be visible in the log
        # of whatever run made it, without anyone having to go looking.
        log.warning("account_level_statement_allowed", statement=statement[:200], context=context)
        return

    kind = is_destructive(statement)
    if kind is None:
        return  # a read is always fine

    root = _target_root(statement)
    if root is None:
        # Fail closed. A destructive statement whose target we cannot identify is the
        # exact case where guessing is unrecoverable.
        raise UnsafeStatementError(
            f"refused a destructive statement whose target could not be identified{where}: "
            f"{statement[:120]!r}\n"
            "Qualify the object name (DATABASE.SCHEMA.OBJECT) so the guard can verify it."
        )

    if root not in allowed:
        qualified = "." in (target_object(statement) or "")
        hint = (
            "The tool reports on data; it never changes it (01 §5). Only DPHM_TEST (dev), "
            "DPHM_STATE and DPHM_SCRATCH may be written or dropped."
            if qualified
            else "The target is unqualified, so it depends on session context this guard "
            "cannot see. Write DATABASE.SCHEMA.OBJECT."
        )
        raise UnsafeStatementError(
            f"refused a {kind.upper()} on {target_object(statement)!r}{where} — target "
            f"database {root!r} is outside {sorted(allowed)}.\n"
            f"  statement: {statement[:160]!r}\n" + hint
        )


def guard_script(
    sql: str,
    *,
    writable_roots: Iterable[str] = DEFAULT_WRITABLE_ROOTS,
    allow_account_level: bool = False,
    context: str = "",
) -> None:
    """Guard every statement in a multi-statement script.

    Checks all of them before any is sent: a script that is half-applied because
    statement nine was refused is worse than one that never ran.
    """
    from dphm.util.sql import split_statements

    for index, statement in enumerate(split_statements(sql), 1):
        guard_statement(
            statement,
            writable_roots=writable_roots,
            allow_account_level=allow_account_level,
            context=f"{context} statement {index}" if context else f"statement {index}",
        )
