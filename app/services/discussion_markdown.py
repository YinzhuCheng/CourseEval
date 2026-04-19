"""Markdown rendering and sanitization for course discussions (Python-Markdown + Bleach).

Dialect: Python-Markdown with fenced_code, sane_lists, nl2br (aligned with course materials).
v1: Remote hotlinked images in Markdown source are rejected at post time; renderer only allows
same-origin discussion upload URLs under /data-files/uploads/discussion-images/...
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import bleach
import markdown
from markupsafe import Markup

_DISCUSSION_IMG_PATH_RE = re.compile(
    r"^/data-files/uploads/discussion-images/course-\d+/post-\d+/[^?\s]+$"
)
_REMOTE_IMG_MD = re.compile(r"!\[[^\]]*\]\(\s*https?://", re.IGNORECASE)

_BASE_TAGS = bleach.sanitizer.ALLOWED_TAGS.union(
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
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "img",
    }
)
_AI_TAGS = _BASE_TAGS - {"a", "img"}

_DISCUSSION_ATTRS = {
    **bleach.sanitizer.ALLOWED_ATTRIBUTES,
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title", "loading"],
    "code": ["class"],
    "pre": ["class"],
}

DISCUSSION_BODY_MAX_CHARS = 256 * 1024


def discussion_body_too_large(body: str | None) -> bool:
    if not body:
        return False
    return len(body) > DISCUSSION_BODY_MAX_CHARS


def has_disallowed_remote_image_markdown(body: str | None) -> bool:
    if not body:
        return False
    return bool(_REMOTE_IMG_MD.search(body))


def _markdown_to_html(raw: str) -> str:
    return markdown.markdown(
        raw,
        extensions=["fenced_code", "nl2br", "sane_lists"],
        output_format="html",
    )


def _clean_html(html: str, *, for_ai: bool) -> str:
    tags = _AI_TAGS if for_ai else _BASE_TAGS
    return bleach.clean(html, tags=tags, attributes=_DISCUSSION_ATTRS, strip=True)


def _fix_links_and_images(html: str) -> str:
    """Enforce https/mailto links with rel; keep only discussion upload image URLs."""

    def repl_a(m: re.Match[str]) -> str:
        inner = m.group(0)
        href_m = re.search(r'href\s*=\s*"([^"]+)"', inner, flags=re.I)
        if not href_m:
            href_m = re.search(r"href\s*=\s*'([^']+)'", inner, flags=re.I)
        if not href_m:
            return ""
        href = href_m.group(1).strip()
        parsed = urlparse(href)
        scheme = (parsed.scheme or "").lower()
        if scheme == "https":
            if 'rel="' not in inner and "rel='" not in inner:
                return inner.replace("<a ", '<a rel="noopener noreferrer nofollow" ', 1)
            return inner
        if scheme == "mailto":
            if 'rel="' not in inner and "rel='" not in inner:
                return inner.replace("<a ", '<a rel="noopener noreferrer" ', 1)
            return inner
        return ""

    html = re.sub(r"<a\b[^>]*>.*?</a>", repl_a, html, flags=re.DOTALL | re.IGNORECASE)

    def repl_img(m: re.Match[str]) -> str:
        inner = m.group(0)
        src_m = re.search(r'src\s*=\s*"([^"]+)"', inner, flags=re.I)
        if not src_m:
            src_m = re.search(r"src\s*=\s*'([^']+)'", inner, flags=re.I)
        if not src_m:
            return ""
        src = src_m.group(1).strip()
        if _DISCUSSION_IMG_PATH_RE.match(src):
            return inner
        return ""

    html = re.sub(r"<img\b[^>]*>", repl_img, html, flags=re.IGNORECASE)
    return html


def render_discussion_markdown(raw: str | None, *, for_ai: bool = False) -> Markup:
    """Render Markdown to safe HTML."""
    if not raw or not str(raw).strip():
        return Markup("")
    html = _markdown_to_html(str(raw))
    html = _clean_html(html, for_ai=for_ai)
    if not for_ai:
        html = _fix_links_and_images(html)
    return Markup(html)
