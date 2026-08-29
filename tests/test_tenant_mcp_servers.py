#!/usr/bin/env python3
"""
Unit tests for app/routes/tenant_mcp_servers.py's pure verification logic —
JSON-RPC body parsing and the initialize+tools/list handshake — with
guarded_fetch mocked out (no real network, no Postgres). The SSRF guard
itself is guarded_fetch's own responsibility and is covered exhaustively by
tests/test_guarded_fetch.py; these tests only cover what this module adds
on top: MCP protocol framing and verification-result handling.

Requires fastapi/pydantic/cryptography installed (the module imports them
at load time) — run via `python3 -m unittest tests.test_tenant_mcp_servers`
from the avry-backend root with the project's requirements installed.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.routes.tenant_mcp_servers as m  # noqa: E402
from app.services.guarded_fetch import GuardedFetchError, GuardedResponse  # noqa: E402


class ExtractJsonRpcBody(unittest.TestCase):
    def test_plain_json(self):
        parsed = m._extract_jsonrpc_body(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')
        self.assertEqual(parsed["result"]["tools"], [])

    def test_sse_framed_single_line(self):
        raw = b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"x"}]}}\n\n'
        parsed = m._extract_jsonrpc_body(raw)
        self.assertEqual(parsed["result"]["tools"], [{"name": "x"}])

    def test_sse_framed_with_event_line(self):
        raw = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        parsed = m._extract_jsonrpc_body(raw)
        self.assertTrue(parsed["result"]["ok"])

    def test_empty_body_raises(self):
        with self.assertRaises(ValueError):
            m._extract_jsonrpc_body(b"")

    def test_malformed_json_raises(self):
        with self.assertRaises(Exception):
            m._extract_jsonrpc_body(b"not json")


class ValidateHttpsUrl(unittest.TestCase):
    def test_https_ok(self):
        m._validate_https_url("https://example.com/mcp")  # no raise

    def test_http_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            m._validate_https_url("http://example.com/mcp")

    def test_no_hostname_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            m._validate_https_url("https:///mcp")


class NameValidation(unittest.TestCase):
    def test_valid_names(self):
        for name in ["a", "my-server_1", "A" * 40]:
            with self.subTest(name=name):
                self.assertTrue(m._NAME_RE.match(name))

    def test_invalid_names(self):
        for name in ["", "has space", "semi;colon", "a" * 41, "emoji😀"]:
            with self.subTest(name=name):
                self.assertFalse(m._NAME_RE.match(name))


def _ok_response(body: bytes, status=200):
    return GuardedResponse(status=status, headers={}, body=body, final_url="https://tenant.example/mcp")


class RunVerification(unittest.TestCase):
    def test_success_extracts_tools(self):
        responses = [
            _ok_response(b'{"jsonrpc":"2.0","id":1,"result":{}}'),  # initialize
            _ok_response(
                b'{"jsonrpc":"2.0","id":2,"result":{"tools":['
                b'{"name":"get_orders","description":"List orders"},'
                b'{"name":"refund","description":"Issue a refund"}'
                b"]}}"
            ),  # tools/list
        ]
        with patch("app.routes.tenant_mcp_servers.guarded_fetch", side_effect=responses):
            result = m._run_verification("https://tenant.example/mcp", None, None)
        self.assertEqual(len(result["tools"]), 2)
        self.assertEqual(result["tools"][0]["name"], "get_orders")

    def test_ssrf_rejection_propagates_as_guarded_fetch_error(self):
        with patch(
            "app.routes.tenant_mcp_servers.guarded_fetch",
            side_effect=GuardedFetchError("'internal.example' resolves to a disallowed address"),
        ):
            with self.assertRaises(GuardedFetchError):
                m._run_verification("https://internal.example/mcp", None, None)

    def test_non_2xx_status_raises(self):
        with patch("app.routes.tenant_mcp_servers.guarded_fetch", return_value=_ok_response(b"", status=500)):
            with self.assertRaises(GuardedFetchError):
                m._run_verification("https://tenant.example/mcp", None, None)

    def test_jsonrpc_error_response_raises(self):
        with patch(
            "app.routes.tenant_mcp_servers.guarded_fetch",
            return_value=_ok_response(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"nope"}}'),
        ):
            with self.assertRaises(GuardedFetchError):
                m._run_verification("https://tenant.example/mcp", None, None)

    def test_missing_tools_key_raises(self):
        responses = [
            _ok_response(b'{"jsonrpc":"2.0","id":1,"result":{}}'),
            _ok_response(b'{"jsonrpc":"2.0","id":2,"result":{"nope":true}}'),
        ]
        with patch("app.routes.tenant_mcp_servers.guarded_fetch", side_effect=responses):
            with self.assertRaises(GuardedFetchError):
                m._run_verification("https://tenant.example/mcp", None, None)

    def test_auth_header_forwarded(self):
        captured = {}

        def _fake_fetch(url, method, headers, body, connect_timeout, total_timeout):
            captured["headers"] = headers
            if b"initialize" in body:
                return _ok_response(b'{"jsonrpc":"2.0","id":1,"result":{}}')
            return _ok_response(b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}')

        with patch("app.routes.tenant_mcp_servers.guarded_fetch", side_effect=_fake_fetch):
            m._run_verification("https://tenant.example/mcp", "X-Api-Key", "secret123")
        self.assertEqual(captured["headers"].get("X-Api-Key"), "secret123")

    def test_no_auth_header_when_not_configured(self):
        captured = {}

        def _fake_fetch(url, method, headers, body, connect_timeout, total_timeout):
            captured["headers"] = headers
            if b"initialize" in body:
                return _ok_response(b'{"jsonrpc":"2.0","id":1,"result":{}}')
            return _ok_response(b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}')

        with patch("app.routes.tenant_mcp_servers.guarded_fetch", side_effect=_fake_fetch):
            m._run_verification("https://tenant.example/mcp", None, None)
        self.assertNotIn("X-Api-Key", captured["headers"])


class RegisterServerRequestValidation(unittest.TestCase):
    def test_rejects_bad_transport(self):
        with self.assertRaises(Exception):
            m.RegisterServerRequest(
                agent_type="customer_service",
                name="ok",
                url="https://tenant.example/mcp",
                transport="websocket",
            )

    def test_accepts_streamable_http(self):
        req = m.RegisterServerRequest(
            agent_type="customer_service",
            name="ok",
            url="https://tenant.example/mcp",
            transport="streamable-http",
        )
        self.assertEqual(req.transport, "streamable-http")

    def test_rejects_bad_name(self):
        with self.assertRaises(Exception):
            m.RegisterServerRequest(
                agent_type="customer_service",
                name="bad name!",
                url="https://tenant.example/mcp",
            )


class TierQuota(unittest.TestCase):
    """`_require_paid_tier` returns the tier the quota is read from, so the
    two have to stay in step: every tier the gate can hand back must have a
    quota, and the ladder must be non-decreasing."""

    def test_every_paid_tier_has_a_quota(self):
        from app.services import tiers

        for tier in tiers.CANONICAL_TIERS:
            self.assertIn(tier, m._MAX_SERVERS_BY_TIER)

    def test_quota_is_non_decreasing_up_the_ladder(self):
        from app.services import tiers

        quotas = [m._MAX_SERVERS_BY_TIER[t] for t in tiers.CANONICAL_TIERS]
        self.assertEqual(quotas, sorted(quotas))
        self.assertGreater(quotas[-1], quotas[0])

    def test_superadmin_resolves_to_enterprise(self):
        with patch("app.routes.tenant_mcp_servers.load_user_record", return_value={"user_id": "u", "tier": "free"}), \
             patch("app.routes.tenant_mcp_servers.is_superadmin", return_value=True):
            self.assertEqual(m._require_paid_tier("u"), "enterprise")

    def test_paid_tier_returned_verbatim(self):
        with patch("app.routes.tenant_mcp_servers.load_user_record", return_value={"user_id": "u", "tier": "business"}), \
             patch("app.routes.tenant_mcp_servers.is_superadmin", return_value=False):
            self.assertEqual(m._require_paid_tier("u"), "business")

    def test_legacy_alias_resolves_before_quota_lookup(self):
        # "pro" is a pre-rebrand id still present on old rows; it must land on
        # `business`, not fall through to the defensive quota of 1.
        with patch("app.routes.tenant_mcp_servers.load_user_record", return_value={"user_id": "u", "tier": "pro"}), \
             patch("app.routes.tenant_mcp_servers.is_superadmin", return_value=False):
            tier = m._require_paid_tier("u")
        self.assertEqual(tier, "business")
        self.assertEqual(m._MAX_SERVERS_BY_TIER[tier], 3)

    def test_free_tier_rejected(self):
        from fastapi import HTTPException

        with patch("app.routes.tenant_mcp_servers.load_user_record", return_value={"user_id": "u", "tier": None}), \
             patch("app.routes.tenant_mcp_servers.is_superadmin", return_value=False):
            with self.assertRaises(HTTPException) as ctx:
                m._require_paid_tier("u")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
