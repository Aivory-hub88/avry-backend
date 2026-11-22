#!/usr/bin/env python3
"""
Unit tests for app/routes/n8n_mcp_connection.py — the user-level n8n MCP
connection (docs/N8N-MCP-CONNECTION-PLAN-2026-11.md). Postgres, the guarded
fetcher, credit_service and the encryption helper are all mocked; the SSRF
guard itself is covered by tests/test_guarded_fetch.py.

Run from the avry-backend root:
    JWT_SECRET=x python3 -m unittest tests.test_n8n_mcp_connection
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

import app.routes.n8n_mcp_connection as n  # noqa: E402
import app.routes.tenant_mcp_servers as t  # noqa: E402
from app.services.guarded_fetch import GuardedFetchError  # noqa: E402

# What n8n 2.26's instance-level MCP server actually lists (28 tools), plus one
# a later version might add.
ALL_TOOLS = [
    "search_workflows", "execute_workflow", "get_execution", "search_executions", "get_workflow_details",
    "publish_workflow", "unpublish_workflow", "prepare_test_pin_data", "test_workflow", "list_credentials",
    "search_data_tables", "create_data_table", "rename_data_table", "add_data_table_column",
    "delete_data_table_column", "rename_data_table_column", "add_data_table_rows", "search_nodes",
    "get_node_types", "get_workflow_best_practices", "validate_workflow", "validate_node_config",
    "create_workflow_from_code", "search_projects", "search_folders", "archive_workflow",
    "update_workflow", "get_sdk_reference", "brand_new_tool_from_a_later_n8n",
]
TOOLS_JSON = [{"name": name, "description": f"{name} does something"} for name in ALL_TOOLS]
DANGEROUS = [
    "list_credentials", "create_data_table", "add_data_table_rows", "delete_data_table_column",
    "publish_workflow", "unpublish_workflow", "archive_workflow", "update_workflow",
    "create_workflow_from_code", "test_workflow", "brand_new_tool_from_a_later_n8n",
]
NOW = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))

    def fetchone(self):
        return self.conn.fetchone_results.pop(0) if self.conn.fetchone_results else None

    def fetchall(self):
        return self.conn.fetchall_results.pop(0) if self.conn.fetchall_results else []


class _FakeConn:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed = []
        self.fetchone_results = list(fetchone_results or [])
        self.fetchall_results = list(fetchall_results or [])
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _public_row(status="verified", enabled=None, tools=None, error=None):
    return (
        "https://n8n.example.com/mcp-server/http", status, "reversible",
        NOW if status == "verified" else None, error, 29 if status == "verified" else None,
        TOOLS_JSON if tools is None else tools,
        list(n.PHASE1_TOOLS) if enabled is None else enabled, NOW, NOW,
    )


def _patched(conn):
    return patch.multiple(n, _connect=MagicMock(return_value=conn), _ensure_schema=MagicMock())


class ToolAllowlist(unittest.TestCase):
    def test_phase1_set_is_the_five_read_and_run_tools(self):
        self.assertEqual(
            set(n.PHASE1_TOOLS),
            {"search_workflows", "get_workflow_details", "execute_workflow", "get_execution", "search_executions"},
        )

    def test_default_enabled_is_only_phase1_tools_the_server_offers(self):
        self.assertEqual(
            n._default_enabled_tools(ALL_TOOLS),
            ["search_workflows", "execute_workflow", "get_execution", "search_executions", "get_workflow_details"],
        )
        self.assertEqual(n._default_enabled_tools(["list_credentials", "search_workflows"]), ["search_workflows"])

    def test_everything_not_enabled_is_disabled_including_future_tools(self):
        disabled = n._effective_disabled_tools(TOOLS_JSON, list(n.PHASE1_TOOLS))
        for tool in DANGEROUS:
            self.assertIn(tool, disabled)
        for tool in n.PHASE1_TOOLS:
            self.assertNotIn(tool, disabled)
        self.assertEqual(len(disabled), len(ALL_TOOLS) - len(n.PHASE1_TOOLS))

    def test_a_row_holding_a_non_phase1_tool_still_never_exposes_it(self):
        disabled = n._effective_disabled_tools(TOOLS_JSON, list(n.PHASE1_TOOLS) + ["list_credentials", "update_workflow"])
        self.assertIn("list_credentials", disabled)
        self.assertIn("update_workflow", disabled)

    def test_user_switching_a_phase1_tool_off_disables_it(self):
        disabled = n._effective_disabled_tools(TOOLS_JSON, ["search_workflows", "get_workflow_details"])
        self.assertIn("execute_workflow", disabled)
        self.assertNotIn("search_workflows", disabled)

    def test_empty_enabled_list_disables_every_tool(self):
        self.assertEqual(len(n._effective_disabled_tools(TOOLS_JSON, [])), len(ALL_TOOLS))

    def test_tools_request_rejects_anything_outside_phase1(self):
        with self.assertRaises(Exception):
            n.ToolsRequest(enabled_tools=["search_workflows", "list_credentials"])
        with self.assertRaises(Exception):
            n.ToolsRequest(enabled_tools=["update_workflow"])
        self.assertEqual(
            n.ToolsRequest(enabled_tools=["execute_workflow", "execute_workflow"]).enabled_tools, ["execute_workflow"]
        )

    def test_agents_request_rejects_unknown_agent(self):
        with self.assertRaises(Exception):
            n.AgentsRequest(agent_types=["not_a_real_agent"])
        known = sorted(n.AGENT_TYPES)[0]
        self.assertEqual(n.AgentsRequest(agent_types=[known, known]).agent_types, [known])


class TokenNormalisation(unittest.TestCase):
    def test_bearer_prefix_and_whitespace_are_stripped(self):
        self.assertEqual(n.ConnectRequest(url="https://x", token="  Bearer abc.def ").token, "abc.def")
        self.assertEqual(n.TokenRequest(token="abc").token, "abc")

    def test_a_token_with_inner_whitespace_is_rejected(self):
        with self.assertRaises(Exception):
            n.ConnectRequest(url="https://x", token="abc def")
        with self.assertRaises(Exception):
            n.TokenRequest(token="Bearer ")


class CreditGate(unittest.TestCase):
    def test_superadmin_unlimited_passes(self):
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": True, "balance": None}):
            n._require_usable_credits("u1")

    def test_any_spendable_balance_passes_regardless_of_plan(self):
        # A one-time buyer or top-up-only account: no plan, but credits.
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": False, "balance": 40, "tier": "free"}):
            n._require_usable_credits("u1")

    def test_no_credits_is_refused_with_402(self):
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": False, "balance": 0}):
            with self.assertRaises(HTTPException) as ctx:
                n._require_usable_credits("u1")
        self.assertEqual(ctx.exception.status_code, 402)

    def test_a_paid_plan_with_an_empty_balance_is_refused_too(self):
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": False, "balance": 0, "tier": "business"}):
            with self.assertRaises(HTTPException) as ctx:
                n._require_usable_credits("u1")
        self.assertEqual(ctx.exception.status_code, 402)

    def test_credit_service_failure_is_503_not_a_pass(self):
        with patch.object(n.credit_service, "get_status", side_effect=RuntimeError("db down")):
            with self.assertRaises(HTTPException) as ctx:
                n._require_usable_credits("u1")
        self.assertEqual(ctx.exception.status_code, 503)


class VerifyRateLimit(unittest.TestCase):
    def setUp(self):
        n._verify_attempts.clear()

    def test_the_eleventh_check_in_a_window_is_429(self):
        for i in range(n._VERIFY_MAX_PER_WINDOW):
            n._check_verify_rate("u1", now=100.0 + i)
        with self.assertRaises(HTTPException) as ctx:
            n._check_verify_rate("u1", now=120.0)
        self.assertEqual(ctx.exception.status_code, 429)

    def test_other_users_are_unaffected(self):
        for i in range(n._VERIFY_MAX_PER_WINDOW):
            n._check_verify_rate("u1", now=100.0 + i)
        n._check_verify_rate("u2", now=120.0)

    def test_the_window_expires(self):
        for i in range(n._VERIFY_MAX_PER_WINDOW):
            n._check_verify_rate("u1", now=100.0 + i)
        n._check_verify_rate("u1", now=100.0 + n._VERIFY_WINDOW_SECONDS + 20)


class Connect(unittest.TestCase):
    def setUp(self):
        n._verify_attempts.clear()
        self.enc = patch.object(n.mcp_server_encryption, "encrypt_auth_header_value", return_value=b"enc")
        self.enc.start()
        self.addCleanup(self.enc.stop)

    def _body(self, url="https://n8n.example.com/mcp-server/http"):
        return n.ConnectRequest(url=url, token="tok-123")

    def test_http_url_is_refused_before_any_network_or_credit_call(self):
        with patch.object(n, "_run_verification") as verify, patch.object(n.credit_service, "get_status") as credits:
            with self.assertRaises(HTTPException) as ctx:
                n.connect(self._body("http://n8n.example.com/mcp-server/http"), user={"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 400)
        verify.assert_not_called()
        credits.assert_not_called()

    def test_no_credits_means_no_outbound_verification_call(self):
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": False, "balance": 0}), \
                patch.object(n, "_run_verification") as verify:
            with self.assertRaises(HTTPException) as ctx:
                n.connect(self._body(), user={"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 402)
        verify.assert_not_called()

    def test_successful_connect_stores_the_encrypted_token_and_defaults_to_phase1_tools(self):
        conn = _FakeConn(fetchone_results=[_public_row()], fetchall_results=[[("search_workflows",)]])
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": True}), \
                patch.object(n, "_run_verification", return_value={"tools": TOOLS_JSON}) as verify, _patched(conn):
            body = n.connect(self._body(), user={"user_id": "u1"})
        verify.assert_called_once_with("https://n8n.example.com/mcp-server/http", "Authorization", "Bearer tok-123")
        insert_sql, insert_params = conn.executed[0]
        self.assertIn("INSERT INTO product.user_n8n_mcp_connections", insert_sql)
        self.assertEqual(insert_params[2], b"enc")  # encrypted, never the plaintext
        self.assertNotIn("tok-123", repr(insert_params))
        self.assertEqual(insert_params[-1], n._default_enabled_tools(ALL_TOOLS))
        self.assertEqual(conn.commits, 1)
        self.assertEqual(body["status"], "verified")
        self.assertNotIn("tok-123", repr(body))

    def test_failed_verification_is_persisted_and_returned_as_422_without_the_token(self):
        conn = _FakeConn(fetchone_results=[_public_row(status="verification_failed", error="server returned HTTP 401")],
                         fetchall_results=[[]])
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": True}), \
                patch.object(n, "_run_verification", side_effect=GuardedFetchError("server returned HTTP 401")), \
                _patched(conn):
            with self.assertRaises(HTTPException) as ctx:
                n.connect(self._body(), user={"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 422)
        detail = ctx.exception.detail
        self.assertEqual(detail["error"], "verification_failed")
        self.assertEqual(detail["reason"], "server returned HTTP 401")
        self.assertEqual(detail["connection"]["status"], "verification_failed")
        self.assertNotIn("tok-123", repr(detail))
        self.assertEqual(conn.commits, 1)  # saved, so the user sees it on the next GET

    def test_ssrf_rejection_from_the_guarded_fetcher_is_reported_not_crashed(self):
        err = "'internal.example.com' resolves to a disallowed address"
        conn = _FakeConn(fetchone_results=[_public_row(status="verification_failed", error=err)], fetchall_results=[[]])
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": True}), \
                patch.object(n, "_run_verification", side_effect=GuardedFetchError(err)), _patched(conn):
            with self.assertRaises(HTTPException) as ctx:
                n.connect(self._body("https://internal.example.com/mcp-server/http"), user={"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("disallowed address", ctx.exception.detail["reason"])

    def test_the_rate_limit_applies_to_connect(self):
        with patch.object(n.credit_service, "get_status", return_value={"unlimited": True}), \
                patch.object(n, "_run_verification", side_effect=GuardedFetchError("x")), \
                patch.object(n, "_persist_verification", return_value={"status": "verification_failed"}):
            for _ in range(n._VERIFY_MAX_PER_WINDOW):
                with self.assertRaises(HTTPException):
                    n.connect(self._body(), user={"user_id": "u1"})
            with self.assertRaises(HTTPException) as ctx:
                n.connect(self._body(), user={"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 429)


class PublicShape(unittest.TestCase):
    def test_public_dict_never_carries_the_token_and_marks_unavailable_tools(self):
        public = n._row_to_public(_public_row(), ["leads_qualifier"])
        self.assertNotIn("auth", repr(sorted(public)))
        by_name = {t_["name"]: t_ for t_ in public["tools"]}
        self.assertTrue(by_name["execute_workflow"]["enabled"])
        self.assertTrue(by_name["execute_workflow"]["available"])
        self.assertFalse(by_name["list_credentials"]["available"])
        self.assertFalse(by_name["list_credentials"]["enabled"])
        self.assertEqual(public["agent_types"], ["leads_qualifier"])

    def test_a_non_phase1_tool_in_the_row_is_not_reported_enabled(self):
        public = n._row_to_public(_public_row(enabled=["list_credentials", "search_workflows"]), [])
        self.assertEqual(public["enabled_tools"], ["search_workflows"])


class InternalServersForAgent(unittest.TestCase):
    def setUp(self):
        self.dec = patch.object(n.mcp_server_encryption, "decrypt_auth_header_value", return_value="Bearer tok-123")
        self.dec.start()
        self.addCleanup(self.dec.stop)

    def _row(self, status="verified", error=None, enabled=None, risk_tier="reversible"):
        return ("https://n8n.example.com/mcp-server/http", b"enc", status, risk_tier, TOOLS_JSON,
                list(n.PHASE1_TOOLS) if enabled is None else enabled, error)

    def test_no_grant_means_nothing_is_handed_out(self):
        with _patched(_FakeConn(fetchone_results=[None])):
            self.assertEqual(n.internal_servers_for_agent("u1", "leads_qualifier"), ([], []))

    def test_granted_agent_gets_a_read_server_and_an_action_server(self):
        conn = _FakeConn(fetchone_results=[self._row()])
        with _patched(conn):
            servers, unavailable = n.internal_servers_for_agent("u1", "leads_qualifier")
        self.assertEqual(unavailable, [])
        by_name = {s["name"]: s for s in servers}
        self.assertEqual(set(by_name), {"aivory-n8n-read", "aivory-n8n"})
        for s in servers:
            self.assertEqual(s["transport"], "streamable-http")
            self.assertEqual(s["auth_header_name"], "Authorization")
            self.assertEqual(s["auth_header_value"], "Bearer tok-123")
            for tool in DANGEROUS:
                self.assertIn(tool, s["disabled_tools"])
        # Looking is free; acting parks for approval. Never 'reversible': on the
        # web Console that tier is refused outright instead of asked about.
        self.assertEqual(by_name["aivory-n8n-read"]["risk_tier"], "safe")
        self.assertEqual(by_name["aivory-n8n"]["risk_tier"], "irreversible")
        read_off, act_off = by_name["aivory-n8n-read"]["disabled_tools"], by_name["aivory-n8n"]["disabled_tools"]
        self.assertIn("execute_workflow", read_off)
        for tool in ("search_workflows", "get_workflow_details", "get_execution", "search_executions"):
            self.assertNotIn(tool, read_off)
            self.assertIn(tool, act_off)
        self.assertNotIn("execute_workflow", act_off)
        sql, params = conn.executed[0]
        self.assertIn("JOIN product.user_n8n_mcp_connection_agents", sql)
        self.assertEqual(params, ("u1", "leads_qualifier"))

    def test_stored_row_tier_does_not_leak_into_what_cerveau_is_told(self):
        # Rows written before this change carry 'reversible'.
        with _patched(_FakeConn(fetchone_results=[self._row(risk_tier="reversible")])):
            servers, _ = n.internal_servers_for_agent("u1", "leads_qualifier")
        self.assertEqual({s["name"]: s["risk_tier"] for s in servers}, {"aivory-n8n-read": "safe", "aivory-n8n": "irreversible"})

    def test_a_server_with_nothing_enabled_is_left_out(self):
        reads_only = ["search_workflows", "get_workflow_details", "get_execution", "search_executions"]
        with _patched(_FakeConn(fetchone_results=[self._row(enabled=reads_only)])):
            servers, _ = n.internal_servers_for_agent("u1", "leads_qualifier")
        self.assertEqual([s["name"] for s in servers], ["aivory-n8n-read"])
        with _patched(_FakeConn(fetchone_results=[self._row(enabled=["execute_workflow"])])):
            servers, _ = n.internal_servers_for_agent("u1", "leads_qualifier")
        self.assertEqual([s["name"] for s in servers], ["aivory-n8n"])
        with _patched(_FakeConn(fetchone_results=[self._row(enabled=[])])):
            self.assertEqual(n.internal_servers_for_agent("u1", "leads_qualifier"), ([], []))

    def test_unverified_connection_is_reported_unavailable_with_a_coarse_code(self):
        with _patched(_FakeConn(fetchone_results=[self._row(status="verification_failed", error="server returned HTTP 401")])):
            self.assertEqual(
                n.internal_servers_for_agent("u1", "leads_qualifier"),
                ([], [{"name": "aivory-n8n", "reason": "auth_rejected"}]),
            )
        with _patched(_FakeConn(fetchone_results=[self._row(status="verification_failed", error="<html>secret</html>")])):
            self.assertEqual(
                n.internal_servers_for_agent("u1", "leads_qualifier"),
                ([], [{"name": "aivory-n8n", "reason": "verification_failed"}]),
            )

    def test_undecryptable_token_is_not_handed_out(self):
        with patch.object(n.mcp_server_encryption, "decrypt_auth_header_value", side_effect=ValueError("bad")), \
                _patched(_FakeConn(fetchone_results=[self._row()])):
            self.assertEqual(n.internal_servers_for_agent("u1", "leads_qualifier"), ([], []))

    def test_database_trouble_never_breaks_the_agents_other_servers(self):
        with patch.object(n, "_connect", side_effect=RuntimeError("no db")):
            self.assertEqual(n.internal_servers_for_agent("u1", "leads_qualifier"), ([], []))

        class _Broken(_FakeConn):
            def cursor(self):
                raise RuntimeError("db down")

        with _patched(_Broken()):
            self.assertEqual(n.internal_servers_for_agent("u1", "leads_qualifier"), ([], []))


class MarkAuthRejected(unittest.TestCase):
    def test_flips_only_a_verified_row_outside_the_grace_window(self):
        conn = _FakeConn(fetchone_results=[("u1",)])
        with _patched(conn):
            self.assertTrue(n.mark_auth_rejected("u1", 401))
        sql, params = conn.executed[0]
        self.assertIn("SET status = 'verification_failed'", sql)
        self.assertIn("status = 'verified'", sql)
        self.assertIn("interval '6 minutes'", sql)
        self.assertIn("HTTP 401", params[0])
        self.assertEqual(params[1], "u1")

    def test_returns_false_when_nothing_matched_or_the_db_is_down(self):
        with _patched(_FakeConn(fetchone_results=[None])):
            self.assertFalse(n.mark_auth_rejected("u1", 403))
        with patch.object(n, "_connect", side_effect=RuntimeError("no db")):
            self.assertFalse(n.mark_auth_rejected("u1", 403))


class AuthFailureNames(unittest.TestCase):
    def test_both_server_names_are_recognised_as_the_n8n_connection(self):
        self.assertEqual(set(n.N8N_SERVER_NAMES), {"aivory-n8n", "aivory-n8n-read"})


class TenantRouteIntegration(unittest.TestCase):
    """app/routes/tenant_mcp_servers.py's Cerveau-facing routes pick the n8n
    connection up."""

    N8N_SERVER = {
        "name": "aivory-n8n", "url": "https://n8n.example.com/mcp-server/http", "transport": "streamable-http",
        "auth_header_name": "Authorization", "auth_header_value": "Bearer tok-123",
        "risk_tier": "reversible", "disabled_tools": ["list_credentials"],
    }

    def _list(self, rows, failed, n8n=(None, None)):
        conn = _FakeConn(fetchall_results=[rows, failed])
        with patch.object(t, "_connect", return_value=conn), patch.object(t, "_ensure_schema"), \
                patch.object(n, "internal_servers_for_agent", return_value=n8n):
            return t.internal_list_verified_servers("u1", "leads_qualifier")

    def test_n8n_connection_is_appended_to_the_tenants_servers(self):
        body = self._list([], [], ([self.N8N_SERVER], []))
        self.assertEqual([s["name"] for s in body["servers"]], ["aivory-n8n"])

    def test_existing_servers_are_untouched_and_come_first(self):
        row = ("orders", "https://orders.example.com/mcp", "streamable-http", None, None, "safe", [])
        body = self._list([row], [], ([self.N8N_SERVER], []))
        self.assertEqual([s["name"] for s in body["servers"]], ["orders", "aivory-n8n"])

    def test_a_tenant_server_with_the_same_name_wins(self):
        row = ("aivory-n8n", "https://mine.example.com/mcp", "streamable-http", None, None, "safe", [])
        body = self._list([row], [], ([self.N8N_SERVER], []))
        self.assertEqual(len(body["servers"]), 1)
        self.assertEqual(body["servers"][0]["url"], "https://mine.example.com/mcp")

    def test_an_unavailable_n8n_connection_is_reported_to_the_agent(self):
        body = self._list([], [], ([], [{"name": "aivory-n8n", "reason": "auth_rejected"}]))
        self.assertEqual(body["unavailable"], [{"name": "aivory-n8n", "reason": "auth_rejected"}])

    def test_auth_failure_report_for_the_n8n_name_flips_the_user_connection(self):
        t._auth_failure_seen.clear()
        conn = _FakeConn(fetchone_results=[None])  # no tenant row by that name
        with patch.object(t, "_connect", return_value=conn), patch.object(t, "_ensure_schema"), \
                patch.object(n, "mark_auth_rejected", return_value=True) as mark:
            result = t.internal_report_auth_failure("u1", "leads_qualifier", t.AuthFailureReport(name="aivory-n8n", status=401))
        self.assertEqual(result, {"flipped": True, "deduped": False})
        mark.assert_called_once_with("u1", 401)

    def test_auth_failure_report_for_the_read_server_name_also_flips_it(self):
        t._auth_failure_seen.clear()
        conn = _FakeConn(fetchone_results=[None])
        with patch.object(t, "_connect", return_value=conn), patch.object(t, "_ensure_schema"), \
                patch.object(n, "mark_auth_rejected", return_value=True) as mark:
            result = t.internal_report_auth_failure("u1", "leads_qualifier", t.AuthFailureReport(name="aivory-n8n-read", status=403))
        self.assertEqual(result, {"flipped": True, "deduped": False})
        mark.assert_called_once_with("u1", 403)

    def test_auth_failure_report_for_another_name_does_not_touch_the_n8n_connection(self):
        t._auth_failure_seen.clear()
        conn = _FakeConn(fetchone_results=[None])
        with patch.object(t, "_connect", return_value=conn), patch.object(t, "_ensure_schema"), \
                patch.object(n, "mark_auth_rejected") as mark:
            result = t.internal_report_auth_failure("u1", "leads_qualifier", t.AuthFailureReport(name="orders", status=401))
        self.assertEqual(result, {"flipped": False, "deduped": False})
        mark.assert_not_called()


class AivoryInstanceGuard(unittest.TestCase):
    """Aivory's own n8n (n8n.aivory.id) is public, so guarded_fetch's private
    address check does not stop it; non-superadmins must not connect to it."""

    def setUp(self):
        n._verify_attempts.clear()

    def test_aivory_hosts_are_recognised(self):
        for url in [
            "https://n8n.aivory.id/mcp-server/http",
            "https://N8N.AIVORY.ID/mcp-server/http",
            "https://n8n.aivory.id./mcp-server/http",
            "https://n8n.aivory.id:443/mcp-server/http",
            "https://user:pw@n8n.aivory.id/mcp-server/http",
            "https://aivory.id/x",
            "https://anything.deep.aivory.uk/x",
        ]:
            with self.subTest(url=url):
                self.assertTrue(n._is_aivory_n8n_host(url))

    def test_lookalike_and_other_hosts_are_not_blocked(self):
        for url in [
            "https://evil-aivory.id/mcp-server/http",
            "https://n8n.aivory.id.evil.com/mcp-server/http",
            "https://notaivory.uk/x",
            "https://my-n8n.example.com/mcp-server/http",
            "https://example.com/?next=https://n8n.aivory.id",
            "not a url",
            "",
        ]:
            with self.subTest(url=url):
                self.assertFalse(n._is_aivory_n8n_host(url))

    def test_hosts_from_the_deployment_env_are_blocked_too(self):
        with patch.dict("os.environ", {"N8N_BASE_URL": "https://flows.internal-corp.net:5678"}):
            self.assertTrue(n._is_aivory_n8n_host("https://flows.internal-corp.net/mcp-server/http"))
        with patch.dict("os.environ", {"N8N_MCP_BLOCKED_HOSTS": "a.example.org, b.example.org"}):
            self.assertTrue(n._is_aivory_n8n_host("https://b.example.org/mcp-server/http"))
            self.assertFalse(n._is_aivory_n8n_host("https://c.example.org/mcp-server/http"))
        # An unset or empty variable must not turn into a match on the empty host.
        with patch.dict("os.environ", {"N8N_BASE_URL": "", "N8N_MCP_BLOCKED_HOSTS": ""}):
            self.assertFalse(n._is_aivory_n8n_host("https:///x"))

    def test_regular_user_is_refused_and_nothing_goes_out(self):
        body = n.ConnectRequest(url="https://n8n.aivory.id/mcp-server/http", token="tok-123")
        with patch.object(n, "load_user_record", return_value={"user_id": "u1", "tier": "enterprise"}), \
                patch.object(n.credit_service, "get_status") as credits, \
                patch.object(n, "_run_verification") as verify:
            with self.assertRaises(HTTPException) as ctx:
                n.connect(body, user={"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("belongs to Aivory", ctx.exception.detail)
        verify.assert_not_called()
        credits.assert_not_called()

    def test_superadmin_may_connect_to_it(self):
        body = n.ConnectRequest(url="https://n8n.aivory.id/mcp-server/http", token="tok-123")
        conn = _FakeConn(fetchone_results=[_public_row()], fetchall_results=[[]])
        with patch.object(n, "load_user_record", return_value={"user_id": "u1", "account_type": "superadmin"}), \
                patch.object(n.credit_service, "get_status", return_value={"unlimited": True}), \
                patch.object(n.mcp_server_encryption, "encrypt_auth_header_value", return_value=b"enc"), \
                patch.object(n, "_run_verification", return_value={"tools": TOOLS_JSON}) as verify, _patched(conn):
            result = n.connect(body, user={"user_id": "u1"})
        self.assertEqual(result["status"], "verified")
        verify.assert_called_once()

    def test_an_ordinary_host_never_looks_up_the_user_record(self):
        # The lookup only happens when the host is Aivory's.
        with patch.object(n, "load_user_record") as lookup:
            n._reject_aivory_instance("https://my-n8n.example.com/mcp-server/http", "u1")
        lookup.assert_not_called()

    def test_reverify_and_replace_token_apply_the_same_rule(self):
        stored = ("https://n8n.aivory.id/mcp-server/http", "tok-123")
        for call in (
            lambda: n.reverify(user={"user_id": "u1"}),
            lambda: n.replace_token(n.TokenRequest(token="new-tok"), user={"user_id": "u1"}),
        ):
            with patch.object(n, "_load_secret", return_value=stored), \
                    patch.object(n, "load_user_record", return_value={"user_id": "u1"}), \
                    patch.object(n.credit_service, "get_status", return_value={"unlimited": True}), \
                    patch.object(n, "_run_verification") as verify:
                with self.assertRaises(HTTPException) as ctx:
                    call()
            self.assertEqual(ctx.exception.status_code, 403)
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
