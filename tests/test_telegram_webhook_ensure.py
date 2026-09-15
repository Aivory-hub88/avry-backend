#!/usr/bin/env python3
"""
Unit tests for Telegram webhook self-registration
(TelegramService.ensure_webhook + its hook in create_link_token).

Run with `python3 -m unittest tests.test_telegram_webhook_ensure`
from the avry-backend root. All Bot API calls are mocked — no real
network, no real tokens.

Locks in the end of the setWebhook tech debt:
  1. A bot whose webhook already matches is left untouched (no
     setWebhook call at all — notably no drop_pending_updates reset).
  2. A mismatched/empty webhook is corrected with the secret + a
     message/callback_query-only allowlist, and never with
     drop_pending_updates.
  3. Without TELEGRAM_WEBHOOK_SECRET the ensure is skipped (False)
     rather than registering an unsecured webhook.
  4. Bot API failures never break deploy-link creation (fail-open).
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests as real_requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import telegram_service as ts  # noqa: E402
from app.services.telegram_service import TelegramService  # noqa: E402

BOT = {
    "token": "123456:TESTTOKEN",
    "username": "TestBot",
    "bot_id": "123456",
    "agent_type": "autonomous",
}
EXPECTED_URL = (
    "https://backend.aivory.id/api/v1/telegram/webhook/autonomous"
)

# NOTE: requests.get/post are patched as attributes (not the whole
# requests module) so requests.RequestException stays a real exception
# class inside ensure_webhook's except clause.
GET = "app.services.telegram_service.requests.get"
POST = "app.services.telegram_service.requests.post"
SECRET = "app.services.telegram_service.settings"


def _resp(ok=True, payload=None, status=200):
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.text = "" if ok else "ERROR"
    r.json.return_value = payload or {}
    return r


class EnsureWebhook(unittest.TestCase):
    def setUp(self):
        self.svc = TelegramService(MagicMock())

    def test_webhook_url_shape(self):
        self.assertEqual(
            TelegramService.webhook_url_for("leads_qualifier"),
            "https://backend.aivory.id/api/v1/telegram/webhook/leads_qualifier",
        )

    @patch(SECRET + ".telegram_webhook_secret", "s3cr3t")
    @patch(POST)
    @patch(GET)
    def test_matching_webhook_is_untouched(
        self, mock_get, mock_post
    ):
        mock_get.return_value = _resp(True, {"result": {"url": EXPECTED_URL}})
        self.assertTrue(self.svc.ensure_webhook(BOT, "autonomous"))
        mock_post.assert_not_called()

    @patch(SECRET + ".telegram_webhook_secret", "s3cr3t")
    @patch(POST)
    @patch(GET)
    def test_mismatch_registers_without_dropping_pending(
        self, mock_get, mock_post
    ):
        mock_get.return_value = _resp(True, {"result": {"url": ""}})
        mock_post.return_value = _resp(True, {"ok": True})
        self.assertTrue(self.svc.ensure_webhook(BOT, "autonomous"))
        _, kwargs = mock_post.call_args
        body = kwargs["json"]
        self.assertEqual(body["url"], EXPECTED_URL)
        self.assertEqual(body["secret_token"], "s3cr3t")
        self.assertEqual(body["allowed_updates"], ["message", "callback_query"])
        self.assertNotIn("drop_pending_updates", body)

    @patch(SECRET + ".telegram_webhook_secret", "s3cr3t")
    @patch(POST)
    @patch(GET)
    def test_set_failure_returns_false(self, mock_get, mock_post):
        mock_get.return_value = _resp(True, {"result": {"url": ""}})
        mock_post.return_value = _resp(False, status=400)
        self.assertFalse(self.svc.ensure_webhook(BOT, "autonomous"))

    @patch(SECRET + ".telegram_webhook_secret", None)
    @patch(POST)
    @patch(GET)
    def test_no_secret_skips(self, mock_get, mock_post):
        self.assertFalse(self.svc.ensure_webhook(BOT, "autonomous"))
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_no_bot_returns_false(self):
        self.assertFalse(self.svc.ensure_webhook(None, "autonomous"))

    @patch(SECRET + ".telegram_webhook_secret", "s3cr3t")
    @patch(POST)
    @patch(GET)
    def test_network_error_returns_false(self, mock_get, mock_post):
        mock_get.side_effect = real_requests.ConnectionError("down")
        self.assertFalse(self.svc.ensure_webhook(BOT, "autonomous"))


class CreateLinkTokenFailOpen(unittest.TestCase):
    @patch(SECRET + ".telegram_webhook_secret", "s3cr3t")
    @patch(POST)
    @patch(GET)
    def test_link_creation_survives_webhook_failure(
        self, mock_get, mock_post
    ):
        mock_get.side_effect = real_requests.Timeout("slow")
        svc = TelegramService(MagicMock())
        svc._load_user = MagicMock(return_value={"tier": "enterprise"})
        with patch(
            "app.services.telegram_service.get_bot", return_value=BOT
        ), patch(
            "app.services.telegram_service.agent_tier_error", return_value=None
        ):
            out = svc.create_link_token("u1", "autonomous")
        self.assertIn("deep_link", out)
        self.assertIn("token", out)


if __name__ == "__main__":
    unittest.main()
