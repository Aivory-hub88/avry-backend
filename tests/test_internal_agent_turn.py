#!/usr/bin/env python3
"""
Server-to-server agent turn for a tenant (used by the dashboard's WhatsApp drafts).
Run: JWT_SECRET=... python3 -m unittest tests.test_internal_agent_turn
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app.routes import telegram as tg  # noqa: E402


def req(**kw):
    base = {"tenant_user_id": "user_1", "agent_type": "leads_qualifier", "text": "draft a reply", "conversation_id": "wa_abc"}
    base.update(kw)
    return tg.InternalAgentTurnRequest(**base)


class TokenTest(unittest.TestCase):
    def test_not_configured_is_503(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DASHBOARD_AGENT_TURN_TOKEN", None)
            with self.assertRaises(HTTPException) as c:
                tg.require_agent_turn_token("x")
            self.assertEqual(c.exception.status_code, 503)

    def test_wrong_or_missing_token_is_403_and_right_one_passes(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_AGENT_TURN_TOKEN": "s3cret-token"}):
            for bad in (None, "", "nope", "s3cret-toke"):
                with self.assertRaises(HTTPException) as c:
                    tg.require_agent_turn_token(bad)
                self.assertEqual(c.exception.status_code, 403)
            self.assertIsNone(tg.require_agent_turn_token("s3cret-token"))


class TurnTest(unittest.TestCase):
    def test_runs_the_tenants_agent_and_returns_the_reply(self):
        svc = mock.Mock()
        svc._load_user.return_value = {"user_id": "user_1", "tier": "enterprise"}
        svc.route_console_message.return_value = {"reply": "Halo!", "pending_approval": None}
        with mock.patch.object(tg, "telegram_service", svc), mock.patch.object(tg, "agent_tier_error", return_value=None):
            out = tg.internal_agent_turn(req())
        self.assertEqual(out["reply"], "Halo!")
        self.assertEqual(out["agent_name"], tg.AGENT_TYPES["leads_qualifier"])
        svc.route_console_message.assert_called_once_with({"user_id": "user_1", "tier": "enterprise"}, "leads_qualifier", "draft a reply", "wa_abc")

    def test_unknown_agent_unknown_tenant_bad_conversation_and_tier_are_refused(self):
        svc = mock.Mock()
        svc._load_user.return_value = {"user_id": "user_1"}
        with mock.patch.object(tg, "telegram_service", svc), mock.patch.object(tg, "agent_tier_error", return_value=None):
            with self.assertRaises(HTTPException) as c:
                tg.internal_agent_turn(req(agent_type="nope"))
            self.assertEqual(c.exception.status_code, 400)
            with self.assertRaises(HTTPException) as c:
                tg.internal_agent_turn(req(conversation_id="../etc/passwd"))
            self.assertEqual(c.exception.status_code, 400)
            with self.assertRaises(HTTPException) as c:
                tg.internal_agent_turn(req(text="   "))
            self.assertEqual(c.exception.status_code, 400)
        svc._load_user.return_value = None
        with mock.patch.object(tg, "telegram_service", svc):
            with self.assertRaises(HTTPException) as c:
                tg.internal_agent_turn(req())
            self.assertEqual(c.exception.status_code, 404)
        svc._load_user.return_value = {"user_id": "user_1"}
        with mock.patch.object(tg, "telegram_service", svc), mock.patch.object(tg, "agent_tier_error", return_value="Upgrade"):
            with self.assertRaises(HTTPException) as c:
                tg.internal_agent_turn(req())
            self.assertEqual(c.exception.status_code, 403)
            svc.route_console_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
