"""CSRF mitigation for cookie-based sessions (Origin / Referer checks on unsafe methods)."""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _host_port_from_header(host_header: str | None) -> tuple[str, str | None]:
    if not host_header:
        return "", None
    host_header = host_header.strip()
    if not host_header:
        return "", None
    if ":" in host_header and host_header[0] != "[":
        host, _, port = host_header.rpartition(":")
        if port.isdigit():
            return host.lower(), port
    return host_header.lower(), None


def _request_targets(request: Request) -> tuple[str, str | None, str]:
    """Return (hostname, port or None, scheme) for the request as seen by the app."""
    host_header = request.headers.get("host")
    hostname, port = _host_port_from_header(host_header)
    scheme = request.url.scheme or "http"
    return hostname, port, scheme


def _origin_matches_request(origin: str, request: Request) -> bool:
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    req_host, req_port, _ = _request_targets(request)
    oh = (parsed.hostname or "").lower()
    op = str(parsed.port) if parsed.port else None
    if oh != req_host:
        return False
    if req_port is None and op is None:
        return True
    if req_port is not None and op is not None and op == req_port:
        return True
    if req_port is None or op is None:
        default_http = "80" if parsed.scheme == "http" else "443"
        if req_port is None:
            return op == default_http
        if op is None:
            return req_port == default_http
    return False


def _referer_matches_request(referer: str, request: Request) -> bool:
    try:
        parsed = urlparse(referer)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    origin_like = f"{parsed.scheme}://{parsed.netloc}"
    return _origin_matches_request(origin_like, request)


class CsrfProtectionMiddleware(BaseHTTPMiddleware):
    """Reject cross-site POST/PUT/PATCH/DELETE when Origin/Referer do not match this host."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path.startswith("/static") or path == "/healthz":
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin:
            if not _origin_matches_request(origin, request):
                return JSONResponse({"detail": "CSRF validation failed (origin)."}, status_code=403)
            return await call_next(request)

        referer = request.headers.get("referer")
        if referer:
            if not _referer_matches_request(referer, request):
                return JSONResponse({"detail": "CSRF validation failed (referer)."}, status_code=403)
            return await call_next(request)

        # No Origin and no Referer (e.g. some tests, curl, or privacy tools).
        return await call_next(request)
