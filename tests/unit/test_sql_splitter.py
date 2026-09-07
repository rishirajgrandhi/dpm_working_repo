"""The comment-aware statement splitter.

This exists because the naive version — split on ";" — silently broke the migration
runner on the project's own DDL, which contains inline comments with semicolons in them.
A splitter bug does not fail loudly; it produces half a CREATE TABLE.
"""

from __future__ import annotations

from dphm.util.sql import split_statements, strip_comments


def test_semicolon_inside_a_line_comment_does_not_split() -> None:
    """The bug this module was written for."""
    sql = """
    create table T (
        NARRATIVE string,   -- LLM text; always labelled as such
        OTHER     string
    );
    """
    statements = split_statements(sql)
    assert len(statements) == 1
    assert "OTHER" in statements[0]


def test_semicolon_inside_a_string_literal_does_not_split() -> None:
    statements = split_statements("insert into T values ('a;b'); select 1;")
    assert len(statements) == 2
    assert statements[0] == "insert into T values ('a;b')"


def test_escaped_quote_inside_a_literal_is_handled() -> None:
    statements = split_statements("select 'it''s fine; really'; select 2;")
    assert len(statements) == 2
    assert "it''s fine; really" in statements[0]


def test_block_comments_are_removed() -> None:
    assert split_statements("select 1 /* a; b */ ; select 2;") == ["select 1", "select 2"]


def test_trailing_statement_without_a_semicolon_is_kept() -> None:
    """A file whose last statement has no terminator must not lose it."""
    assert split_statements("select 1; select 2") == ["select 1", "select 2"]


def test_empty_and_comment_only_input_yields_nothing() -> None:
    assert split_statements("") == []
    assert split_statements("-- just a comment\n") == []
    assert split_statements(";;;") == []


def test_strip_comments_preserves_literals() -> None:
    assert strip_comments("select '-- not a comment'").strip() == "select '-- not a comment'"


def test_double_quoted_identifiers_are_preserved() -> None:
    """Snowflake quoted identifiers can contain almost anything, including a semicolon."""
    statements = split_statements('select "weird;name" from T; select 1;')
    assert len(statements) == 2
    assert '"weird;name"' in statements[0]
