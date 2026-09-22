#!/usr/bin/env python3
"""
Unit tests for the Discussion deployment channel.

Discussion turns reach agents as a first-class channel (binding + shared
room session + conversational approval), exactly like console/telegram —
never via prompt coaching. One thread round fans out to several agents with
the same space/thread: they must share ONE downstream session id (ledger
grouping + shared transcript) while keeping per-agent binding ids (history
pointers and per-agent pending approvals must not merge).

Run with `python3 -m unittest tests.test_discussion_channel`
from the avry-backend root. All network calls are mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import telegram_service as ts  # noqa: E402


def make_service():
    svc = ts.TelegramService.__new__(ts.TelegramService)
    return svc


class DiscussionChannelTest(unittest.TestCase):
    def test_room_session_shared_across_agents(self):
        svc = make_service()
        user = {"user_id": "u1", "account_type": "free"}
        seen = {}

        def fake_route(self, binding, text, channel="telegram"):
            seen[binding["agent_type"]] = dict(binding)
            seen[binding["agent_type"]]["_channel"] = channel
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
            svc.route_discussion_message(user, "leads_qualifier", "space-1", "root-1", "hi")
            svc.route_discussion_message(user, "customer_service", "space-1", "root-1", "hi")

        lex = seen["leads_qualifier"]
        teo = seen["customer_service"]
        # History/pending identity stays per-agent ...
        self.assertNotEqual(lex["binding_id"], teo["binding_id"])
        self.assertIn("leads_qualifier", lex["binding_id"])
        self.assertIn("customer_service", teo["binding_id"])
        self.assertIn("space-1", lex["binding_id"])
        self.assertIn("root-1", lex["binding_id"])
        # ... the downstream turn session is shared per thread ...
        self.assertEqual(lex["room_session_id"], teo["room_session_id"])
        self.assertEqual(lex["room_session_id"], "discussion_u1_space-1_root-1")
        self.assertNotIn("leads_qualifier", lex["room_session_id"])
        # ... and the gateway sees the discussion channel, not console.
        self.assertEqual(lex["_channel"], "discussion")
        self.assertEqual(teo["_channel"], "discussion")

    def test_route_to_agent_prefers_room_session(self):
        svc = make_service()
        captured = {}

        class FakeResp:
            ok = True

            def json(self):
                return {"reply": "ok"}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json)
            return FakeResp()

        from unittest.mock import MagicMock

        with (
            patch("requests.post", side_effect=fake_post),
            patch.object(ts, "settings", MagicMock(telegram_agent_gateway_url="http://x")),
            patch.dict("os.environ", {}, clear=False),
        ):
            binding = {
                "user_id": "u1",
                "agent_type": "leads_qualifier",
                "account_type": "free",
                "chat_id": 0,
                "binding_id": "discussion_u1_space-1_root-1_leads_qualifier",
                "room_session_id": "discussion_u1_space-1_root-1",
            }
            out = svc._route_to_agent(binding, "hi", channel="discussion")

        self.assertEqual(out["reply"], "ok")
        self.assertEqual(captured["session_id"], "discussion_u1_space-1_root-1")
        self.assertEqual(captured["channel"], "discussion")

    def test_conversational_approval_short_circuits(self):
        svc = make_service()
        user = {"user_id": "u1", "account_type": "free"}
        with (
            patch.object(
                ts.TelegramService, "_try_conversational_approval",
                return_value={"reply": "Done.", "pending_approval": None},
            ),
            patch.object(
                ts.TelegramService, "_route_to_agent",
                side_effect=AssertionError("must not reach the gateway"),
            ),
        ):
            out = svc.route_discussion_message(user, "leads_qualifier", "s", "r", "Ya")
        self.assertEqual(out["reply"], "Done.")


if __name__ == "__main__":
    unittest.main()
