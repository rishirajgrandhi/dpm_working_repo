"""SQL text handling: splitting a script into statements.

Naive splitting on `;` is wrong, and wrong in a way that only shows up at runtime. Both
`state/ddl.sql` and the test fixtures contain inline comments with semicolons in them
("-- LLM text; always labelled as such"), and a naive split cuts the statement in half —
so the migration runner would fail applying its own DDL.

One implementation, used by the migration runner and by the test that parses every
statement, so the two cannot disagree about what a statement is.
"""

from __future__ import annotations


def strip_comments(sql: str) -> str:
    """Remove `--` line comments and `/* */` block comments, preserving string literals."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # A string literal: copy verbatim, including any -- or ; inside it.
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == quote:
                    # Doubled quote is an escaped quote, not the end of the literal.
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> list[str]:
    """Split a script into executable statements.

    Comments are stripped first, so a semicolon inside a comment cannot split a
    statement. Semicolons inside string literals are respected.
    """
    cleaned = strip_comments(sql)
    statements: list[str] = []
    current: list[str] = []
    i = 0
    n = len(cleaned)
    while i < n:
        ch = cleaned[i]
        if ch in "'\"":
            quote = ch
            current.append(ch)
            i += 1
            while i < n:
                current.append(cleaned[i])
                if cleaned[i] == quote:
                    if i + 1 < n and cleaned[i + 1] == quote:
                        current.append(cleaned[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    trailing = "".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements
