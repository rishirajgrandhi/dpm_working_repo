"""Secret resolution and the secret-literal scanner.

Two rules from 13 §3, both enforced here rather than by convention:

1. Secrets come only from env vars, backed by the platform secret manager.
2. A secret literal appearing in a YAML file is a **load-time error**, not a warning.

Rule 2 is the interesting one. It is enforced by an entropy + pattern scan, because a
reviewer skimming a 50-line config will not reliably notice a pasted password, and the
config files are committed to git.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter

# ${VAR} interpolation. Deliberately not ${VAR:-default}: a silently defaulted
# credential is worse than a startup failure.
_ENV_REF = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
_ENV_REF_INLINE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


class SecretResolutionError(RuntimeError):
    """An env var a config file referenced is not set."""


class SecretLiteralError(ValueError):
    """A credential appears to be written into a config file. Refuse to load."""


# Vendor token shapes worth naming explicitly, since these are unambiguous.
_KNOWN_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("JIRA/Atlassian token", re.compile(r"\bATATT[A-Za-z0-9_\-=]{20,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Snowflake password URL", re.compile(r"snowflake://[^\s:]+:[^\s@]+@")),
)

# Config keys whose value is a credential by definition. A literal here is always wrong.
_SECRET_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "private_key",
        "client_secret",
        "signing_secret",
        "auth_token",
        "access_key",
        "secret_key",
    }
)

# Values that are obviously not credentials even though they sit under a secret-ish key.
_ALLOWED_PLACEHOLDERS = frozenset({"", "none", "null", "changeme", "unset", "todo"})


def shannon_entropy(value: str) -> float:
    """Bits per character. High entropy in a config file usually means a pasted key."""
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def looks_like_secret(value: str, *, key: str | None = None) -> str | None:
    """Return a human-readable reason if `value` looks like a credential, else None."""
    if not isinstance(value, str) or _ENV_REF.match(value.strip()):
        return None

    stripped = value.strip()
    for label, pattern in _KNOWN_SECRET_PATTERNS:
        if pattern.search(stripped):
            return f"looks like a {label}"

    if key and key.lower() in _SECRET_KEY_NAMES:
        if stripped.lower() in _ALLOWED_PLACEHOLDERS:
            return None
        if _ENV_REF_INLINE.search(stripped):
            return None
        return (
            f"'{key}' is a credential field, so its value must be an env reference "
            "like ${DPHM_RDS_PASSWORD}, never a literal"
        )

    # Unlabelled high-entropy blobs. The thresholds are set to catch pasted keys while
    # leaving realistic config values (table names, SQL, emails, URLs) alone.
    if (
        len(stripped) >= 24
        and " " not in stripped
        and shannon_entropy(stripped) >= 4.0
        and re.fullmatch(r"[A-Za-z0-9+/=_\-]+", stripped)
    ):
        return (
            f"is a {len(stripped)}-character high-entropy string "
            f"({shannon_entropy(stripped):.1f} bits/char), which is the shape of a credential"
        )
    return None


def resolve(value: str, *, where: str = "config") -> str:
    """Expand `${VAR}` references from the environment.

    A missing variable raises. Interpolating an empty string would let the service start
    with an unset credential and fail later against the warehouse, which is a much worse
    error message than this one.
    """
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        env = os.environ.get(name)
        if env is None:
            missing.append(name)
            return ""
        return env

    result = _ENV_REF_INLINE.sub(_sub, value)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise SecretResolutionError(
            f"{where}: environment variable(s) not set: {names}. "
            "Secrets come only from env vars backed by the platform secret manager (13 §3)."
        )
    return result


def is_env_reference(value: object) -> bool:
    return isinstance(value, str) and bool(_ENV_REF_INLINE.search(value))
