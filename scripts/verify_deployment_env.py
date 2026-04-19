#!/usr/bin/env python3
"""
Validate production-like environment variables (loaded via app.config / .env).

Intended for ECS or staging hosts after .env is installed. Safe to skip in CI
unless VERIFY_DEPLOYMENT=1 (or this script is invoked with --strict) and a
real .env is present.

Optional checks (opt-in via environment):
  VERIFY_DEPLOYMENT_REQUIRE_REDIS=1  — ping Redis at REDIS_URL
  VERIFY_DEPLOYMENT_REQUIRE_DOCKER=1 — docker info + image inspect for RUNNER_IMAGE
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fail(msg: str) -> None:
    print(f"verify_deployment_env: {msg}", file=sys.stderr)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        _fail(msg)
        raise SystemExit(1)


def _is_default_secret(secret: str) -> bool:
    normalized = secret.strip().lower()
    return normalized in {"", "change-me-in-production", "changeme"}


def _validate_smtp(host: str, from_addr: str, port: int) -> None:
    _require(bool(host.strip()), "SMTP_HOST must be set for email verification / password reset.")
    _require(bool(from_addr.strip()), "SMTP_FROM_ADDRESS must be set when SMTP is required.")
    _require(1 <= port <= 65535, f"SMTP_PORT must be between 1 and 65535 (got {port}).")


def _validate_app_base_url(url: str) -> None:
    _require(bool(url.strip()), "APP_BASE_URL must be set (public origin for email links).")
    parsed = urlparse(url.strip())
    _require(parsed.scheme in {"http", "https"}, "APP_BASE_URL must include a scheme (http or https).")
    _require(bool(parsed.netloc), "APP_BASE_URL must include a host (e.g. https://eval.example.com).")
    if parsed.scheme == "http":
        host = (parsed.hostname or "").lower()
        if host not in {"localhost", "127.0.0.1"}:
            _fail(
                "APP_BASE_URL uses http with a non-local host; production should use https "
                "so email links match the public site."
            )
            raise SystemExit(1)


def _check_redis(url: str) -> None:
    try:
        from redis import Redis
    except ImportError as exc:  # pragma: no cover - dev deps always include redis
        _fail(f"Redis check requested but redis is not importable: {exc}")
        raise SystemExit(1) from exc
    client = Redis.from_url(url, socket_connect_timeout=3)
    try:
        client.ping()
    except Exception as exc:
        _fail(f"Redis ping failed for REDIS_URL: {exc}")
        raise SystemExit(1) from exc


def _check_docker(image: str) -> None:
    checks = (["docker", "info"], ["docker", "image", "inspect", image])
    for cmd in checks:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()
            _fail(f"Command {' '.join(cmd)!r} failed (exit {proc.returncode}). {tail[:500]}")
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate deployment-oriented environment settings.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if production-like checks fail (default when VERIFY_DEPLOYMENT=1).",
    )
    args = parser.parse_args()
    strict = args.strict or os.getenv("VERIFY_DEPLOYMENT", "").strip() == "1"
    if not strict:
        print("verify_deployment_env: skipping (pass --strict or set VERIFY_DEPLOYMENT=1).")
        return

    # Import after argparse so --help works without app deps on broken PYTHONPATH.
    from app.config import get_settings

    settings = get_settings()

    secret = settings.secret_key
    _require(len(secret) >= 32, "SECRET_KEY should be at least 32 characters.")
    _require(not _is_default_secret(secret), 'SECRET_KEY must not use the default placeholder "change-me-in-production".')

    _validate_app_base_url(settings.app_base_url)
    _require(bool(settings.database_url.strip()), "DATABASE_URL must be set.")
    _require(bool(settings.redis_url.strip()), "REDIS_URL must be set.")
    _require(bool(settings.runner_image.strip()), "RUNNER_IMAGE must be set.")

    _validate_smtp(settings.smtp_host, settings.smtp_from_address, settings.smtp_port)

    _require(settings.pdf_review_max_pages >= 1, "PDF_REVIEW_MAX_PAGES must be at least 1.")
    _require(settings.execution_timeout_seconds >= 1, "EXECUTION_TIMEOUT_SECONDS must be at least 1.")

    if os.getenv("VERIFY_DEPLOYMENT_REQUIRE_REDIS", "").strip() == "1":
        _check_redis(settings.redis_url)

    if os.getenv("VERIFY_DEPLOYMENT_REQUIRE_DOCKER", "").strip() == "1":
        _check_docker(settings.runner_image)

    print("verify_deployment_env: OK (strict checks passed).")


if __name__ == "__main__":
    main()
