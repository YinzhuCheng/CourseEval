"""Render Markdown to safe HTML for course materials."""

from __future__ import annotations

import bleach
import markdown


ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS.union(
    {
        "p",
        "pre",
        "code",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "hr",
        "br",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "img",
    }
)
ALLOWED_ATTRIBUTES = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "img": ["src", "alt", "title", "width", "height", "loading"],
    "a": ["href", "title", "rel"],
    "code": ["class"],
    "pre": ["class"],
}


def render_material_markdown(raw: str | None) -> str:
    if not raw or not str(raw).strip():
        return ""
    html = markdown.markdown(
        str(raw),
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        output_format="html",
    )
    return bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True)
