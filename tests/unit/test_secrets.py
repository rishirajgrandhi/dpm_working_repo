"""The secret-literal scan (13 §3).

The property under test: a credential written into a YAML file is a **load-time error**.
These tests are the reason that claim is true rather than aspirational.
"""

from __future__ import annotations

import pytest

from dphm.util import secrets


# Token-shaped test data is ASSEMBLED AT RUNTIME rather than written as literals.
#
# Written literally, these tripped GitHub's secret-scanning push protection — which was
# right to be suspicious, since it cannot tell a fake from a real one. The lesson is not
# "click the allow-this-secret link": it is that a credential-shaped literal does not
# belong in source, and that applies to a test fixture too. Concatenation keeps the
# scanner under test exercised while leaving nothing in the file for a scanner to find.
def _shaped(prefix: str, body: str) -> str:
    return prefix + body


TOKEN_SHAPES = [
    _shaped("sk-" + "ant-", "api03-" + "A" * 8 + "B" * 8 + "C" * 8),
    _shaped("AKIA", "IOSFODNN" + "7" + "EXAMPLE"),
    _shaped("ghp" + "_", "x" * 24 + "1234"),
    _shaped("xox" + "b-", "1" * 12 + "-" + "abcdefghijklmnop"),
    _shaped("-----BEGIN " + "RSA ", "PRIVATE KEY-----"),
    _shaped("snowflake://user:", "hunter2@account/db"),
]


@pytest.mark.parametrize("value", TOKEN_SHAPES)
def test_known_token_shapes_are_caught(value: str) -> None:
    assert secrets.looks_like_secret(value) is not None


def test_the_test_file_itself_contains_no_credential_shaped_literal() -> None:
    """The fixtures above must stay assembled, not inlined.

    If someone "simplifies" them back to literals, secret scanning blocks the next push
    and the reason will not be obvious. This fails first, with the reason attached.
    """
    from pathlib import Path

    source = Path(__file__).read_text()
    # Assembled here too, or this guard would itself put the literals in the file it
    # is guarding — which is how it failed the first time it ran.
    forbidden = ("xox" + "b-1", "sk-" + "ant-api03", "ghp" + "_x", "AKIA" + "IOSFODNN")
    for shape in forbidden:
        assert shape not in source, (
            f"{shape!r} appears as a literal in this file. Assemble it at runtime "
            "instead — GitHub push protection rejects credential-shaped literals, and "
            "it cannot tell a fixture from a real key."
        )


def test_credential_keys_reject_literals_but_accept_env_refs() -> None:
    assert secrets.looks_like_secret("hunter2", key="password") is not None
    assert secrets.looks_like_secret("${DPHM_RDS_PASSWORD}", key="password") is None


def test_high_entropy_blob_is_caught_without_a_key_hint() -> None:
    assert secrets.looks_like_secret("Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MEFCQ0RFRg") is not None


@pytest.mark.parametrize(
    "value",
    [
        "ANALYTICS.GOLD.DIM_CUSTOMER",
        "#dpl-orders-alerts",
        "sam@acme.com",
        "git@github.com:acme/orders-pipeline.git",
        "select count(*) from orders where is_test = false",
        "9999-12-31",
        "SOURCE_UPDATED_AT desc nulls last",
    ],
)
def test_realistic_config_values_are_not_flagged(value: str) -> None:
    """False positives here would make the scanner unusable, so they are tested too."""
    assert secrets.looks_like_secret(value) is None


def test_resolve_raises_naming_the_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DPHM_NOPE", raising=False)
    with pytest.raises(secrets.SecretResolutionError, match="DPHM_NOPE"):
        secrets.resolve("${DPHM_NOPE}", where="project.yaml")


def test_resolve_does_not_default_a_missing_secret_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty interpolation would let the service start with no credential.

    That fails later, against the warehouse, with a far worse error message.
    """
    monkeypatch.delenv("DPHM_ABSENT", raising=False)
    with pytest.raises(secrets.SecretResolutionError):
        secrets.resolve("prefix-${DPHM_ABSENT}-suffix", where="test")
