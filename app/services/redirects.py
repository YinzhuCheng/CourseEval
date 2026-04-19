"""Redirect helpers for user-supplied return paths."""

from __future__ import annotations

from urllib.parse import urlsplit


def safe_local_redirect(candidate: str | None, default: str) -> str:
    """Return candidate only when it is a same-origin absolute path."""
    value = (candidate or "").strip()
    if not value:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    if "\\" in value or any(ord(ch) < 32 for ch in value):
        return default
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return default
    return value
