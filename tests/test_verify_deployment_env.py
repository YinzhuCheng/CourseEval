import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import get_settings


class VerifyDeploymentEnvScriptTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_script_skips_when_not_strict(self) -> None:
        get_settings.cache_clear()
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "verify_deployment_env.py")],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipping", proc.stdout)

    def test_strict_passes_with_production_like_env(self) -> None:
        env = {
            "SECRET_KEY": "x" * 32,
            "APP_BASE_URL": "https://eval.example.com",
            "DATABASE_URL": "sqlite:////tmp/verify_deployment_env.db",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "RUNNER_IMAGE": "courseeval-runner:latest",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_ADDRESS": "noreply@example.com",
            "SMTP_PORT": "587",
        }
        get_settings.cache_clear()
        with patch.dict(os.environ, env, clear=False):
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "verify_deployment_env.py"),
                    "--strict",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def test_strict_rejects_default_secret(self) -> None:
        env = {
            "SECRET_KEY": "change-me-in-production",
            "APP_BASE_URL": "https://eval.example.com",
            "DATABASE_URL": "sqlite:////tmp/x.db",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "RUNNER_IMAGE": "courseeval-runner:latest",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_ADDRESS": "noreply@example.com",
        }
        get_settings.cache_clear()
        with patch.dict(os.environ, env, clear=False):
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "verify_deployment_env.py"),
                    "--strict",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SECRET_KEY", proc.stderr)


class VerifyDeploymentEnvModuleTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_module_loads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "scripts" / "verify_deployment_env.py"
        spec = importlib.util.spec_from_file_location("verify_deployment_env", path)
        self.assertIsNotNone(spec and spec.loader)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.main))
