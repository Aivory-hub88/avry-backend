#!/usr/bin/env python3
"""
Dedicated-bot support for Ivo / Juno / Mira: no hidden whitelist blocks the
new agent keys in the webhook path, get_bot(), or the setup script.

Run with `python3 -m unittest tests.test_telegram_agent_bots`.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-prod")

from app.routes import telegram as routes  # noqa: E402
from app.services import telegram_service as ts  # noqa: E402

NEW_KEYS = ("data_analyst", "operations_inventory", "marketing_content")
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "set_telegram_webhook.sh"


class NewAgentBotTests(unittest.TestCase):
    def test_keys_are_known_agent_types(self):
        for key in NEW_KEYS:
            self.assertIn(key, ts.AGENT_TYPES)

    def test_get_bot_uses_dedicated_env(self):
        for key in NEW_KEYS:
            suffix = key.upper()
            env = {f"TELEGRAM_BOT_TOKEN_{suffix}": "999:TOK", f"TELEGRAM_BOT_USERNAME_{suffix}": f"{key}_bot"}
            with patch.dict(os.environ, env):
                bot = ts.get_bot(key)
            self.assertEqual(bot["bot_id"], "999")
            self.assertEqual(bot["username"], f"{key}_bot")
            self.assertEqual(bot["agent_type"], key)

    def test_webhook_route_accepts_keys(self):
        for key in NEW_KEYS:
            req = MagicMock()
            with patch.object(routes, "get_bot", return_value={"bot_id": "1"}) as gb, \
                 patch.object(routes, "_handle_webhook", new=MagicMock(side_effect=lambda *a: asyncio.sleep(0, {"ok": True}))):
                out = asyncio.run(routes.telegram_webhook_per_bot(key, req, "s"))
            gb.assert_called_once_with(key)
            self.assertEqual(out, {"ok": True})

    def test_unknown_key_still_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(routes.telegram_webhook_per_bot("nope", MagicMock(), "s"))
        self.assertEqual(cm.exception.status_code, 404)

    def test_setup_script_lists_keys(self):
        text = SCRIPT.read_text()
        for key in NEW_KEYS:
            self.assertIn(f'"{key}|', text)


if __name__ == "__main__":
    unittest.main()
