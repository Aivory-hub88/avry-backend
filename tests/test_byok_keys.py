"""BYOK key routes: validation gates run before any DB or network access."""
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import byok_keys
from app.routes.agent_actions import get_current_user_payload


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(byok_keys.router)
    app.dependency_overrides[get_current_user_payload] = lambda: {"user_id": "u1"}
    return TestClient(app)


class ByokKeysTest(unittest.TestCase):
    def test_unknown_provider_is_404(self):
        r = _client().put("/api/v1/byok-keys/nope", json={"api_key": "x" * 30})
        self.assertEqual(r.status_code, 404)

    def test_wrong_prefix_is_rejected_without_calling_provider(self):
        with mock.patch.object(byok_keys, "verify_provider_key") as verify:
            r = _client().put("/api/v1/byok-keys/anthropic", json={"api_key": "sk-or-" + "x" * 30})
        self.assertEqual(r.status_code, 400)
        verify.assert_not_called()

    def test_provider_rejection_is_400_and_nothing_stored(self):
        with mock.patch.object(byok_keys, "verify_provider_key", return_value=(False, "The provider rejected this key")), \
             mock.patch.object(byok_keys, "_connect") as connect:
            r = _client().put("/api/v1/byok-keys/openrouter", json={"api_key": "sk-or-" + "x" * 30})
        self.assertEqual(r.status_code, 400)
        connect.assert_not_called()

    def test_hint_never_exposes_more_than_last_four(self):
        self.assertEqual(byok_keys._hint("sk-or-abcdefghijWXYZ"), "…WXYZ")

    def test_resolver_ignores_unknown_provider(self):
        self.assertIsNone(byok_keys.get_user_byok_key("u1", "nope"))


if __name__ == "__main__":
    unittest.main()
