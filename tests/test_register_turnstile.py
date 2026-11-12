"""Turnstile gate on POST /api/v1/auth/register (app.services.turnstile)."""
import asyncio
import os
import unittest
from unittest import mock

import httpx

from app.services import turnstile

ENV = {
    "TURNSTILE_SECRET": "test-secret",
    "TURNSTILE_HOSTNAMES": "aivory.uk,aivory.id",
    "INTERNAL_SERVICE_TOKEN": "internal-xyz",
}


def _client(payload=None, status=200, exc=None):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        if exc:
            raise exc
        return httpx.Response(status, json=payload or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def run(coro):
    return asyncio.run(coro)


class ModeTest(unittest.TestCase):
    def test_default_follows_secret(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(turnstile.mode(), "off")
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET": "x"}, clear=True):
            self.assertEqual(turnstile.mode(), "enforce")

    def test_explicit_off_wins(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_SECRET": "x", "REGISTER_TURNSTILE": "off"}, clear=True):
            self.assertEqual(turnstile.mode(), "off")


class VerifySignupTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, ENV, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_valid_token_passes_and_forwards_ip(self):
        client, seen = _client({"success": True, "action": "signup", "hostname": "aivory.uk"})
        run(turnstile.verify_signup("tok", remote_ip="1.2.3.4", client=client))
        self.assertIn("response=tok", seen["body"])
        self.assertIn("remoteip=1.2.3.4", seen["body"])

    def test_missing_token_rejected_without_calling_cloudflare(self):
        client, seen = _client({"success": True, "action": "signup", "hostname": "aivory.uk"})
        with self.assertRaises(turnstile.TurnstileRejected):
            run(turnstile.verify_signup(None, client=client))
        self.assertNotIn("body", seen)

    def test_failed_challenge_rejected(self):
        client, _ = _client({"success": False, "error-codes": ["invalid-input-response"]})
        with self.assertRaises(turnstile.TurnstileRejected):
            run(turnstile.verify_signup("tok", client=client))

    def test_wrong_action_rejected(self):
        # A checkout payment token must not open account creation.
        client, _ = _client({"success": True, "action": "checkout", "hostname": "aivory.uk"})
        with self.assertRaises(turnstile.TurnstileRejected):
            run(turnstile.verify_signup("tok", client=client))

    def test_unknown_hostname_rejected(self):
        client, _ = _client({"success": True, "action": "signup", "hostname": "evil.example"})
        with self.assertRaises(turnstile.TurnstileRejected):
            run(turnstile.verify_signup("tok", client=client))

    def test_cloudflare_down_fails_closed(self):
        client, _ = _client(exc=httpx.ConnectError("down"))
        with self.assertRaises(turnstile.TurnstileRejected):
            run(turnstile.verify_signup("tok", client=client))
        client, _ = _client({}, status=500)
        with self.assertRaises(turnstile.TurnstileRejected):
            run(turnstile.verify_signup("tok", client=client))

    def test_internal_caller_skips_check(self):
        run(turnstile.verify_signup(None, x_internal_token="internal-xyz"))
        with self.assertRaises(turnstile.TurnstileRejected):
            run(turnstile.verify_signup(None, x_internal_token="wrong"))

    def test_enforce_without_hostnames_is_unavailable(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_HOSTNAMES": ""}):
            with self.assertRaises(turnstile.TurnstileUnavailable):
                run(turnstile.verify_signup("tok"))

    def test_off_mode_skips_everything(self):
        with mock.patch.dict(os.environ, {"REGISTER_TURNSTILE": "off"}):
            run(turnstile.verify_signup(None))


class RegisterRouteTest(unittest.TestCase):
    """The route maps the gate to HTTP and never reaches auth_service on a reject."""

    def setUp(self):
        p = mock.patch.dict(os.environ, {**ENV, "JWT_SECRET": os.environ.get("JWT_SECRET", "t" * 40)}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import auth as auth_routes

        self.auth_routes = auth_routes
        app = FastAPI()
        app.include_router(auth_routes.router)
        self.client = TestClient(app)

    def test_reject_is_403_and_creates_nothing(self):
        with mock.patch.object(self.auth_routes.auth_service, "register") as reg:
            r = self.client.post("/api/v1/auth/register", json={"email": "a@b.co", "password": "secret123"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("verification", r.json()["detail"])
        reg.assert_not_called()

    def test_misconfigured_is_503(self):
        with mock.patch.dict(os.environ, {"TURNSTILE_HOSTNAMES": ""}):
            with mock.patch.object(self.auth_routes.auth_service, "register") as reg:
                r = self.client.post(
                    "/api/v1/auth/register",
                    json={"email": "a@b.co", "password": "secret123", "turnstile_token": "tok"},
                )
        self.assertEqual(r.status_code, 503)
        reg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
