"""JWT_SECRET is fail-closed: no env, no service.

Before this, six modules fell back to a placeholder that is public in this
repo, so a container started without JWT_SECRET accepted tokens anyone could
sign.
"""
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from app.services import jwt_secret

REPO_ROOT = Path(__file__).resolve().parent.parent


class LoadJwtSecretTest(unittest.TestCase):
    def test_missing_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "JWT_SECRET"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(jwt_secret.MissingJwtSecretError):
                jwt_secret.load_jwt_secret()

    def test_blank_raises(self):
        with mock.patch.dict(os.environ, {"JWT_SECRET": "   "}):
            with self.assertRaises(jwt_secret.MissingJwtSecretError):
                jwt_secret.load_jwt_secret()

    def test_old_fallback_value_raises(self):
        with mock.patch.dict(os.environ, {"JWT_SECRET": "your-secret-key-change-in-production"}):
            with self.assertRaises(jwt_secret.MissingJwtSecretError):
                jwt_secret.load_jwt_secret()

    def test_real_secret_returned_unchanged(self):
        with mock.patch.dict(os.environ, {"JWT_SECRET": "s3cr3t-value "}):
            self.assertEqual(jwt_secret.load_jwt_secret(), "s3cr3t-value ")

    def test_every_module_uses_the_shared_secret(self):
        from app.routes import admin_users, deps, impersonation, logs
        from app.services import auth_service, impersonation_token_service

        for mod in (admin_users, deps, impersonation, logs, auth_service, impersonation_token_service):
            self.assertIs(mod.JWT_SECRET, jwt_secret.JWT_SECRET, mod.__name__)

    def test_no_placeholder_fallback_left_in_source(self):
        hits = [
            str(p.relative_to(REPO_ROOT))
            for p in (REPO_ROOT / "app").rglob("*.py")
            if "your-secret-key-change-in-production" in p.read_text(encoding="utf-8", errors="ignore")
            and p.name != "jwt_secret.py"
        ]
        self.assertEqual(hits, [])


class StartupRefusedTest(unittest.TestCase):
    def test_app_import_fails_without_secret(self):
        env = {k: v for k, v in os.environ.items() if k != "JWT_SECRET"}
        proc = subprocess.run(
            [sys.executable, "-c", "import app.services.auth_service"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("JWT_SECRET is not set", proc.stderr)


if __name__ == "__main__":
    unittest.main()
