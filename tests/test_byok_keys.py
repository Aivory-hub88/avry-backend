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


class _Cur:
    """Just enough of a cursor for byok_keys' queries, keyed on the SQL text."""

    def __init__(self, db):
        self.db, self._row = db, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        db = self.db
        if "FROM product.byok_settings" in sql:
            self._row = db.settings
        elif "SELECT 1 FROM product.byok_keys" in sql:
            self._row = (1,) if params[1] in db.keys else None
        elif "INSERT INTO product.byok_settings" in sql:
            db.settings = (params[1], params[2])
            self._row = None
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cur(self.db)

    def commit(self):
        pass

    def close(self):
        pass


class _Db:
    def __init__(self, keys=(), settings=None):
        self.keys, self.settings = set(keys), settings


class ByokSettingsTest(unittest.TestCase):
    def _patched(self, db):
        return mock.patch.multiple(byok_keys, _connect=lambda: _Conn(db), _ensure_schema=lambda c: None)

    def test_defaults_are_on_with_no_primary(self):
        with self._patched(_Db()):
            self.assertEqual(byok_keys._load_settings(_Conn(_Db()).cursor(), "u1"),
                             {"enabled": True, "preferred_provider": None})

    def test_switch_can_be_turned_off_and_back_on(self):
        db = _Db(keys={"openrouter"})
        with self._patched(db):
            r = _client().put("/api/v1/byok-keys/settings", json={"enabled": False})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"enabled": False, "preferred_provider": None})
            r = _client().put("/api/v1/byok-keys/settings", json={"enabled": True})
            self.assertEqual(r.json()["enabled"], True)

    def test_primary_provider_needs_a_saved_key(self):
        db = _Db(keys={"openrouter"})
        with self._patched(db):
            bad = _client().put("/api/v1/byok-keys/settings", json={"preferred_provider": "anthropic"})
            self.assertEqual(bad.status_code, 400)
            self.assertIsNone(db.settings)
            ok = _client().put("/api/v1/byok-keys/settings", json={"preferred_provider": "openrouter"})
            self.assertEqual(ok.json(), {"enabled": True, "preferred_provider": "openrouter"})

    def test_settings_route_is_not_swallowed_by_the_provider_route(self):
        r = _client().put("/api/v1/byok-keys/settings", json={})
        self.assertEqual(r.status_code, 400)  # our handler, not "Unknown provider"

    def test_unknown_primary_provider_is_404(self):
        r = _client().put("/api/v1/byok-keys/settings", json={"preferred_provider": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_resolution_honours_switch_and_primary(self):
        keys = {"openrouter": "sk-or-a", "anthropic": "sk-ant-b"}
        for settings, expected in [
            (None, ("openrouter", "sk-or-a")),                       # default order
            ((True, "anthropic"), ("anthropic", "sk-ant-b")),        # primary wins
            ((True, "openai"), ("openrouter", "sk-or-a")),           # primary has no key: fall through
            ((False, "anthropic"), None),                            # switched off
        ]:
            db = _Db(keys=keys, settings=settings)
            with self._patched(db), mock.patch.object(byok_keys, "_load_plaintext", lambda u, p: keys.get(p)):
                self.assertEqual(byok_keys.resolve_active_byok("u1"), expected, settings)

    def test_switched_off_user_gets_no_key_from_either_resolver(self):
        db = _Db(keys={"openrouter": "x"}, settings=(False, None))
        with self._patched(db), mock.patch.object(byok_keys, "_load_plaintext", lambda u, p: "sk-or-secret"):
            self.assertIsNone(byok_keys.get_user_byok_key("u1", "openrouter"))
            self.assertIsNone(byok_keys.resolve_active_byok("u1"))


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
