"""Pytest: relax CSRF for ASGI tests that omit browser Origin/Referer headers."""

import os


def pytest_configure() -> None:
    os.environ.setdefault("CSRF_ALLOW_MISSING_ORIGIN_REFERER", "1")
    from app.config import reset_settings_cache

    reset_settings_cache()
