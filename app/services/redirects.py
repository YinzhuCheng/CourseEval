"""Redirect helpers for user-supplied return paths."""

from __future__ import annotations

from urllib.parse import urlsplit, urlparse


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


def safe_referer_redirect(referer: str | None, default: str, *, request_host: str, request_port: int | None) -> str:
    """Use Referer's path+query only when scheme/host/port match the current request (anti open-redirect)."""
    if not referer:
        return default
    try:
        parsed = urlparse(referer)
    except ValueError:
        return default
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return default
    host = parsed.hostname.lower()
    port = parsed.port
    req_host = request_host.lower().strip()
    if host != req_host:
        return default
    if request_port is not None:
        if port not in (None, request_port):
            return default
    elif port is not None:
        expected = 443 if parsed.scheme == "https" else 80
        if port != expected:
            return default
    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//"):
        return default
    if "\\" in path or any(ord(ch) < 32 for ch in path):
        return default
    qs = f"?{parsed.query}" if parsed.query else ""
    return f"{path}{qs}"
