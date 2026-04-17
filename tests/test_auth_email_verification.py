import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.auth import can_verify_email_token, hash_password
from app.config import get_settings
from app.db import Base, utcnow
from app.main import app
from app.models import User
from app.routes.auth import get_db


class AuthEmailVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
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

        def override_get_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.settings = get_settings()
        self.original_base_url = self.settings.app_base_url
        self.original_registration_invite_code = self.settings.registration_invite_code
        self.original_internal_email_domain = self.settings.internal_email_domain

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        object.__setattr__(self.settings, "app_base_url", self.original_base_url)
        object.__setattr__(self.settings, "registration_invite_code", self.original_registration_invite_code)
        object.__setattr__(self.settings, "internal_email_domain", self.original_internal_email_domain)
        self.engine.dispose()

    def _db_user(self, email: str) -> User | None:
        with self.session_factory() as db:
            return db.scalar(select(User).where(User.email == email))

    def test_register_creates_unverified_user_and_redirects_to_pending_page(self) -> None:
        object.__setattr__(self.settings, "app_base_url", "https://courseeval.example.com")

        response = self.client.post(
            "/register",
            data={
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/register/pending?email=newuser@example.com")

        user = self._db_user("newuser@example.com")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertFalse(user.email_verified)
        self.assertTrue(user.is_active)
        self.assertIsNotNone(user.email_verification_token)
        self.assertTrue(can_verify_email_token(user))

    def test_unverified_user_cannot_log_in(self) -> None:
        with self.session_factory() as db:
            db.add(
                User(
                    username="pending",
                    email="pending@example.com",
                    password_hash=hash_password("password123"),
                    email_verified=False,
                    email_verification_token="pending-token",
                    is_active=True,
                )
            )
            db.commit()

        response = self.client.post(
            "/login",
            data={"login": "pending@example.com", "password": "password123"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login?email=pending@example.com")

    def test_verify_email_marks_user_verified_and_logs_in(self) -> None:
        with self.session_factory() as db:
            db.add(
                User(
                    username="verifyme",
                    email="verifyme@example.com",
                    password_hash=hash_password("password123"),
                    email_verified=False,
                    email_verification_token="verify-token",
                    email_verification_sent_at=utcnow(),
                    is_active=True,
                )
            )
            db.commit()

        response = self.client.get("/verify-email?token=verify-token", follow_redirects=False)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/system")

        user = self._db_user("verifyme@example.com")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertTrue(user.email_verified)
        self.assertIsNone(user.email_verification_token)

    def test_resend_verification_rotates_token(self) -> None:
        with self.session_factory() as db:
            db.add(
                User(
                    username="rotate",
                    email="rotate@example.com",
                    password_hash=hash_password("password123"),
                    email_verified=False,
                    email_verification_token="old-token",
                    is_active=True,
                )
            )
            db.commit()

        response = self.client.post(
            "/verify-email/resend",
            data={"email": "rotate@example.com"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login?email=rotate@example.com")

        user = self._db_user("rotate@example.com")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertNotEqual(user.email_verification_token, "old-token")
        self.assertFalse(user.email_verified)

    def test_login_page_prefills_email_for_resend_form(self) -> None:
        response = self.client.get("/login?email=tester@example.com")

        self.assertEqual(response.status_code, 200)
        self.assertIn('value="tester@example.com"', response.text)

    def test_register_email_contains_verification_link_in_log_fallback(self) -> None:
        object.__setattr__(self.settings, "app_base_url", "https://courseeval.example.com")

        response = self.client.post(
            "/register",
            data={
                "username": "mailuser",
                "email": "mailuser@example.com",
                "password": "password123",
                "confirm_password": "password123",
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.session_factory() as db:
            user = db.scalar(select(User).where(User.email == "mailuser@example.com"))
            assert user is not None
            verification_token = user.email_verification_token

        pending_page = self.client.get("/register/pending?email=mailuser@example.com")
        self.assertIn("mailuser@example.com", pending_page.text)
        self.assertIsNotNone(verification_token)

    def test_invite_registration_can_activate_without_email(self) -> None:
        object.__setattr__(self.settings, "registration_invite_code", "server-invite")
        object.__setattr__(self.settings, "internal_email_domain", "internal.local")

        response = self.client.post(
            "/register",
            data={
                "registration_mode": "invite",
                "username": "invited",
                "email": "",
                "invite_code": "server-invite",
                "password": "password123",
                "confirm_password": "password123",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/system")

        user = self._db_user_by_username("invited")
        self.assertIsNotNone(user)
        assert user is not None
        self.assertTrue(user.email_verified)
        self.assertIsNone(user.email_verification_token)
        self.assertTrue(user.email.endswith("@internal.local"))

    def test_invalid_invite_code_is_rejected(self) -> None:
        object.__setattr__(self.settings, "registration_invite_code", "server-invite")

        response = self.client.post(
            "/register",
            data={
                "registration_mode": "invite",
                "username": "badinvite",
                "email": "",
                "invite_code": "wrong-code",
                "password": "password123",
                "confirm_password": "password123",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._db_user_by_username("badinvite"))

    def _db_user_by_username(self, username: str) -> User | None:
        with self.session_factory() as db:
            return db.scalar(select(User).where(User.username == username))


if __name__ == "__main__":
    unittest.main()
