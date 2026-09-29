#!/usr/bin/env python3
"""
Unit tests for ADR-020 Teams: resolution, turn payload, and route validation.
DB and network are mocked.

Run with `python3 -m unittest tests.test_teams` from the avry-backend root.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app.routes import teams as troutes  # noqa: E402
from app.services import teams  # noqa: E402
from app.services import telegram_service as ts  # noqa: E402


class ActiveAgentsForTeamTest(unittest.TestCase):
    def test_isolated_team_narrows_to_members(self):
        t = {"isolated": True, "agent_types": ["leads_qualifier", "customer_service"]}
        self.assertEqual(teams.active_agents_for_team(t), ["leads_qualifier", "customer_service"])

    def test_isolated_is_the_default(self):
        self.assertEqual(teams.active_agents_for_team({"agent_types": ["autonomous"]}), ["autonomous"])

    def test_non_isolated_or_missing_team_means_no_filter(self):
        self.assertIsNone(teams.active_agents_for_team({"isolated": False, "agent_types": ["autonomous"]}))
        self.assertIsNone(teams.active_agents_for_team(None))
        self.assertIsNone(teams.active_agents_for_team({"isolated": True, "agent_types": []}))


class ResolveFailOpenTest(unittest.TestCase):
    def test_store_error_yields_none(self):
        with patch.object(teams, "get_team", side_effect=RuntimeError("db down")):
            self.assertIsNone(teams.resolve_active_agents("u1", team_id="team_x"))
        with patch.object(teams, "team_for_channel", side_effect=RuntimeError("db down")):
            self.assertIsNone(teams.resolve_active_agents("u1", kind="workspace", ref="s1"))

    def test_no_selector_is_none(self):
        self.assertIsNone(teams.resolve_active_agents("u1"))

    def test_channel_lookup(self):
        team = {"isolated": True, "agent_types": ["office_assistant", "autonomous"]}
        with patch.object(teams, "team_for_channel", return_value=team) as f:
            out = teams.resolve_active_agents("u1", kind="workspace", ref="s1")
        f.assert_called_once_with("u1", "workspace", "s1")
        self.assertEqual(out, ["office_assistant", "autonomous"])

    def test_team_id_is_scoped_to_the_user(self):
        with patch.object(teams, "get_team", return_value=None) as f:
            self.assertIsNone(teams.resolve_active_agents("u1", team_id="someone-elses"))
        f.assert_called_once_with("u1", "someone-elses")


class TurnPayloadTest(unittest.TestCase):
    """The Team's members ride to the bridge as active_agents."""

    def _post(self, binding, channel):
        svc = ts.TelegramService.__new__(ts.TelegramService)
        resp = MagicMock(ok=True)
        resp.json.return_value = {"reply": "ok"}
        with (
            patch.object(ts.settings, "telegram_agent_gateway_url", "http://gw"),
            patch.object(ts.requests, "post", return_value=resp) as post,
            patch.object(ts.agent_run_tracker, "mark_running"),
            patch.object(ts.agent_run_tracker, "clear_running"),
        ):
            svc._route_to_agent(binding, "hi", channel=channel)
        return post.call_args.kwargs["json"]

    def base(self, **kw):
        return {"user_id": "u1", "agent_type": "autonomous", "chat_id": 0, "binding_id": "b1", **kw}

    def test_workspace_turn_uses_the_space_team(self):
        with patch("app.services.teams.resolve_active_agents", return_value=["autonomous", "leads_qualifier"]) as r:
            body = self._post(self.base(space_id="s1"), "discussion")
        r.assert_called_once_with("u1", kind="workspace", ref="s1")
        self.assertEqual(body["active_agents"], ["autonomous", "leads_qualifier"])

    def test_console_turn_uses_the_explicit_team(self):
        with patch("app.services.teams.resolve_active_agents", return_value=["autonomous"]) as r:
            body = self._post(self.base(team_id="team_1"), "console")
        r.assert_called_once_with("u1", team_id="team_1")
        self.assertEqual(body["active_agents"], ["autonomous"])

    def test_telegram_turn_uses_the_binding_team(self):
        with patch("app.services.teams.resolve_active_agents", return_value=["customer_service"]) as r:
            body = self._post(self.base(binding_id="123_-456"), "telegram")
        r.assert_called_once_with("u1", kind="telegram", ref="123_-456")
        self.assertEqual(body["active_agents"], ["customer_service"])

    def test_no_team_omits_the_field(self):
        with patch("app.services.teams.resolve_active_agents", return_value=None):
            body = self._post(self.base(space_id="s1"), "discussion")
        self.assertNotIn("active_agents", body)

    def test_unscoped_channels_never_consult_teams(self):
        with patch("app.services.teams.resolve_active_agents") as r:
            body = self._post(self.base(), "console")
        r.assert_not_called()
        self.assertNotIn("active_agents", body)


class RouteValidationTest(unittest.TestCase):
    USER = {"user_id": "u1"}

    def test_unknown_agent_is_400(self):
        with self.assertRaises(HTTPException) as c:
            troutes.create_team(troutes.TeamBody(name="t", agent_types=["ghost"]), user=self.USER)
        self.assertEqual(c.exception.status_code, 400)

    def test_duplicate_agents_are_deduped(self):
        with patch.object(troutes.teams, "create_team", return_value={"id": "x"}) as f:
            troutes.create_team(
                troutes.TeamBody(name=" t ", agent_types=["autonomous", "autonomous", "leads_qualifier"]),
                user=self.USER,
            )
        f.assert_called_once_with("u1", "t", ["autonomous", "leads_qualifier"], True)

    def test_invalid_channel_kind_is_400(self):
        with self.assertRaises(HTTPException) as c:
            troutes.attach_channel("team_1", troutes.ChannelBody(kind="whatsapp", ref="x"), user=self.USER)
        self.assertEqual(c.exception.status_code, 400)

    def test_foreign_channel_is_403(self):
        with patch.object(troutes, "_owns_channel", return_value=False):
            with self.assertRaises(HTTPException) as c:
                troutes.attach_channel("team_1", troutes.ChannelBody(kind="workspace", ref="s1"), user=self.USER)
        self.assertEqual(c.exception.status_code, 403)

    def test_missing_team_is_404(self):
        with patch.object(troutes, "_owns_channel", return_value=True), patch.object(
            troutes.teams, "attach_channel", return_value=None
        ):
            with self.assertRaises(HTTPException) as c:
                troutes.attach_channel("nope", troutes.ChannelBody(kind="workspace", ref="s1"), user=self.USER)
        self.assertEqual(c.exception.status_code, 404)

    def test_workspace_ownership_check(self):
        facts = {"doc_owner": "u1", "acl_role": None, "member_role": None}
        with patch.object(troutes.workspace_acting, "fetch_space_facts", return_value=facts):
            self.assertTrue(troutes._owns_channel("u1", "workspace", "s1"))
            self.assertFalse(troutes._owns_channel("u2", "workspace", "s1"))
        with patch.object(troutes.workspace_acting, "fetch_space_facts", side_effect=RuntimeError("x")):
            self.assertFalse(troutes._owns_channel("u1", "workspace", "s1"))

    def test_telegram_ownership_check(self):
        with patch.object(troutes.telegram_service, "list_bindings", return_value=[{"binding_id": "1_2"}]):
            self.assertTrue(troutes._owns_channel("u1", "telegram", "1_2"))
            self.assertFalse(troutes._owns_channel("u1", "telegram", "9_9"))

    def test_console_channel_rows_are_not_a_thing(self):
        self.assertFalse(troutes._owns_channel("u1", "console", "x"))


if __name__ == "__main__":
    unittest.main()
