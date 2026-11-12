#!/usr/bin/env python3
"""AI agents (console agent chat, Discussion rooms, Telegram deploys) need a paid plan."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app.routes import telegram  # noqa: E402
from app.services import access  # noqa: E402


class PaidAgents(unittest.TestCase):
    def _status(self, state=None, exc=None):
        with patch.object(access, "read", return_value=state, side_effect=exc):
            try:
                telegram._require_paid_agents("u1")
                return 200
            except HTTPException as e:
                return e.status_code

    def test_free_account_gets_402(self):
        self.assertEqual(self._status(access.decide("free")), 402)

    def test_paid_one_time_and_staff_pass(self):
        self.assertEqual(self._status(access.decide("user", row_tier="operational")), 200)
        self.assertEqual(self._status(access.decide("user", features=["snapshot"])), 200)
        self.assertEqual(self._status(access.decide("admin")), 200)

    def test_unreadable_is_503_and_missing_is_401(self):
        self.assertEqual(self._status(exc=access.AccessUnavailable("down")), 503)
        self.assertEqual(self._status(None), 401)

    def test_agent_chat_checks_before_running(self):
        body = telegram.AgentChatRequest(agent_type=next(iter(telegram.AGENT_TYPES)), text="hi")
        with patch.object(access, "read", return_value=access.decide("free")), \
             patch.object(telegram.telegram_service, "route_console_message") as run:
            with self.assertRaises(HTTPException) as ctx:
                telegram.agent_chat(body, {"user_id": "u1"})
        self.assertEqual(ctx.exception.status_code, 402)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
