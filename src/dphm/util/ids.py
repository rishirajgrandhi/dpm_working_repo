"""Deterministic identity: check ids, fingerprints, run ids.

Identity has to be stable across cosmetic edits and unstable across behaviour changes
(06 §7). Everything here is pure and total so the same inputs always give the same id.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def new_ulid() -> str:
    """A lexicographically sortable id. Used for RUN_ID, RESULT_ID, INCIDENT_ID."""
    ms = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ms << 80) | rand
    out = []
    for _ in range(26):
        out.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def stable_digest(payload: Any, *, length: int = 12) -> str:
    """A short digest of any JSON-able structure, invariant to dict ordering.

    Key ordering must not change a check's identity, or every config reformat would
    orphan a check's history.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:length]


def check_id(template: str, table: str, params: dict[str, Any]) -> str:
    """`{template}.{table}.{stable_param_digest}` — 06 §7.

    The dedup and history key. Stable across cosmetic edits, because it is built from
    the semantic parameters rather than the rendered SQL.
    """
    return f"{template}.{table.lower()}.{stable_digest(params)}"


def sql_sha256(rendered_sql: str) -> str:
    """Changes when behaviour changes, which invalidates gates and sign-offs (06 §7)."""
    return hashlib.sha256(rendered_sql.encode()).hexdigest()


def incident_fingerprint(check_id_: str, failing_keys: list[Any]) -> str:
    """`sha256(check_id + sorted(failing_key set))` — 08 §2, 11 §1.

    Fingerprinting on the *failing rows* rather than the check is what makes "one break,
    one incident with updates" true by construction, and what stops a March sign-off from
    hiding a July bug: a new failing row is a new fingerprint and gets a fresh look.
    """
    canonical = sorted(
        json.dumps(k, sort_keys=True, separators=(",", ":"), default=str) for k in failing_keys
    )
    joined = check_id_ + "|" + "|".join(canonical)
    return hashlib.sha256(joined.encode()).hexdigest()
