#!/usr/bin/env python3
"""
Unit tests for console room session sharing.

One room round fans out to several agents with the same conversation_id.
They must share ONE downstream session id (ledger grouping + shared room
transcript) while keeping per-agent binding ids (history pointers and
per-agent pending approvals must not merge).

Run with `python3 -m unittest tests.test_console_room_session`
from the avry-backend root. All network calls are mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import telegram_service as ts  # noqa: E402


def make_service():
    svc = ts.TelegramService.__new__(ts.TelegramService)
    return svc


class ConsoleRoomSessionTest(unittest.TestCase):
    def test_room_session_shared_across_agents(self):
        svc = make_service()
        user = {"user_id": "u1", "account_type": "free"}
        seen = {}

        def fake_route(self, binding, text, channel="telegram"):
            seen[binding["agent_type"]] = dict(binding)
            return {"reply": "ok", "pending_approval": None}

        with (
            patch.object(ts.TelegramService, "_route_to_agent", fake_route),
            patch.object(
                ts.TelegramService, "_try_conversational_approval", return_value=None
            ),
            patch.object(
                ts.TelegramService, "_remember_pending_result",
                side_effect=lambda binding, result: result,
            ),
        ):
            svc.route_console_message(user, "leads_qualifier", "hi", "conv1")
            svc.route_console_message(user, "customer_service", "hi", "conv1")

        lex = seen["leads_qualifier"]
        teo = seen["customer_service"]
        # History/pending identity stays per-agent ...
        self.assertNotEqual(lex["binding_id"], teo["binding_id"])
        self.assertIn("leads_qualifier", lex["binding_id"])
        self.assertIn("customer_service", teo["binding_id"])
        # ... but the downstream turn session is shared.
        self.assertEqual(lex["room_session_id"], teo["room_session_id"])
        self.assertEqual(lex["room_session_id"], "console_u1_conv1")
        self.assertNotIn("leads_qualifier", lex["room_session_id"])

    def test_route_to_agent_prefers_room_session(self):
        svc = make_service()
        captured = {}

        class FakeResp:
            ok = True

            def json(self):
                return {"reply": "ok"}

        with (
            patch("requests.post", return_value=FakeResp()) as post,
            patch.object(ts, "settings", MagicMock(telegram_agent_gateway_url="http://x")),
            patch.dict("os.environ", {}, clear=False),
        ):
            # _route_to_agent reads settings + env; keep it simple: call the
            # session-selection logic via a console pseudo-binding.
            binding = {
                "user_id": "u1",
                "agent_type": "leads_qualifier",
                "account_type": "free",
                "chat_id": 0,
                "binding_id": "console_u1_leads_qualifier_c1",
                "room_session_id": "console_u1_c1",
            }
            with patch.object(
                ts.agent_run_tracker, "mark_running", return_value=None
            ), patch.object(ts.agent_run_tracker, "clear_running", return_value=None):
                svc._route_to_agent(binding, "hi", channel="console")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["session_id"], "console_u1_c1")

    def test_route_to_agent_falls_back_without_room_session(self):
        svc = make_service()

        class FakeResp:
            ok = True

            def json(self):
                return {"reply": "ok"}

        with (
            patch("requests.post", return_value=FakeResp()) as post,
            patch.object(ts, "settings", MagicMock(telegram_agent_gateway_url="http://x")),
        ):
            binding = {
                "user_id": "u1",
                "agent_type": "customer_service",
                "account_type": "free",
                "chat_id": 99,
                "binding_id": "tgbind_1",
            }
            with patch.object(
                ts.agent_run_tracker, "mark_running", return_value=None
            ), patch.object(ts.agent_run_tracker, "clear_running", return_value=None):
                svc._route_to_agent(binding, "hi", channel="telegram")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["json"]["session_id"], "tgbind_1")


if __name__ == "__main__":
    unittest.main()
