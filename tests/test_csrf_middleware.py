import os
import unittest

import anyio
import httpx
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import reset_settings_cache
from app.db import Base
from app.main import app
from app.routes.auth import get_db
from app.services.redirects import safe_referer_redirect


class AsyncAsgiClient:
    def __init__(self, asgi_app):
        self.app = asgi_app

    def get(self, url: str, **kwargs) -> httpx.Response:
        return anyio.run(self._request, "GET", url, kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return anyio.run(self._request, "POST", url, kwargs)

    async def _request(self, method: str, url: str, kwargs: dict) -> httpx.Response:
        kwargs.setdefault("follow_redirects", False)
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, url, **kwargs)


class CsrfMiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_csrf_relaxed = os.environ.get("CSRF_ALLOW_MISSING_ORIGIN_REFERER")
        os.environ["CSRF_ALLOW_MISSING_ORIGIN_REFERER"] = "0"
        reset_settings_cache()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

        async def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = AsyncAsgiClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()
        if self._prev_csrf_relaxed is None:
            os.environ.pop("CSRF_ALLOW_MISSING_ORIGIN_REFERER", None)
        else:
            os.environ["CSRF_ALLOW_MISSING_ORIGIN_REFERER"] = self._prev_csrf_relaxed
        reset_settings_cache()

    def test_post_rejects_mismatched_origin(self) -> None:
        response = self.client.post(
            "/register",
            data={"username": "x", "email": "x@example.com", "password": "pw", "confirm_password": "pw"},
            headers={"origin": "https://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_post_allows_matching_origin(self) -> None:
        response = self.client.post(
            "/forgot-password",
            data={"email": "nobody@example.com"},
            headers={"origin": "http://testserver"},
        )
        self.assertEqual(response.status_code, 303)

    def test_post_without_origin_or_referer_returns_403_when_strict(self) -> None:
        response = self.client.post(
            "/forgot-password",
            data={"email": "nobody@example.com"},
        )
        self.assertEqual(response.status_code, 403)

    def test_locale_redirect_strips_cross_site_referer(self) -> None:
        response = self.client.get(
            "/locale/en",
            headers={"referer": "https://evil.example/phish"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/")


class SafeRefererRedirectTests(unittest.TestCase):
    def test_same_host_path_preserved(self) -> None:
        dest = safe_referer_redirect(
            "http://testserver/student/courses?x=1",
            "/",
            request_host="testserver",
            request_port=None,
        )
        self.assertEqual(dest, "/student/courses?x=1")

    def test_foreign_host_rejected(self) -> None:
        dest = safe_referer_redirect(
            "https://evil.example/ok",
            "/",
            request_host="testserver",
            request_port=None,
        )
        self.assertEqual(dest, "/")


if __name__ == "__main__":
    unittest.main()
