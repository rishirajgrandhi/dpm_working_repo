"""Jinja2 template + params -> SQL, with a rendered-SQL hash (06 §2)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from dphm.util.ids import sql_sha256

TEMPLATE_DIR = Path(__file__).parent / "templates"


class TemplateRenderError(RuntimeError):
    """A template is missing, or a required parameter was not supplied."""


@dataclass(frozen=True)
class RenderedCheck:
    template: str
    sql: str
    sql_sha256: str
    params: dict[str, Any]


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # StrictUndefined is the point: a missing parameter must be a loud error, not an
        # empty string silently spliced into a WHERE clause, which is how a check quietly
        # starts comparing nothing.
        undefined=StrictUndefined,
        # trim_blocks MUST stay off. It strips the newline after a block tag, so a
        # template whose line ends in `{% endfor %}` gets welded to the next clause —
        # producing `... = b.CUSTOMER_IDwhere 1=1`, which is a compilation error found
        # only by executing it. Extra blank lines are harmless in SQL; missing newlines
        # are not.
        trim_blocks=False,
        lstrip_blocks=True,
        keep_trailing_newline=False,
        autoescape=False,  # SQL, not HTML. Identifiers are validated by the caller.
    )


def available_templates() -> list[str]:
    return sorted(
        str(p.relative_to(TEMPLATE_DIR)).removesuffix(".sql.j2")
        for p in TEMPLATE_DIR.rglob("*.sql.j2")
    )


def render(template: str, params: dict[str, Any]) -> RenderedCheck:
    """Render one template. A missing parameter raises rather than producing bad SQL."""
    from jinja2 import UndefinedError

    try:
        tpl = _env().get_template(f"{template}.sql.j2")
    except TemplateNotFound as exc:
        raise TemplateRenderError(
            f"no template named {template!r}. Available: {', '.join(available_templates())}"
        ) from exc

    try:
        sql = tpl.render(**params).strip()
    except UndefinedError as exc:
        raise TemplateRenderError(
            f"template {template!r} needs a parameter that was not supplied: {exc}. "
            "A missing parameter would render as an empty string and silently change what "
            "the check compares, so it is an error."
        ) from exc

    if not sql:
        raise TemplateRenderError(f"template {template!r} rendered to empty SQL")
    return RenderedCheck(
        template=template, sql=sql, sql_sha256=sql_sha256(sql), params=dict(params)
    )
