#!/usr/bin/env python3
"""
Unit tests for Telegram group handling.

Run with `python3 -m unittest tests.test_telegram_group_handling`.
No network, no real DB: Bot API calls and the agent gateway are mocked.

  1. In groups the bot answers only when mentioned, replied to, or commanded.
  2. Messages from bot senders are ignored.
  3. The sender's name is prefixed to the prompt in groups (not in DMs).
  4. A group -> supergroup upgrade moves the binding to the new chat id.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import telegram_service as ts  # noqa: E402
from app.services.telegram_service import TelegramService  # noqa: E402

BOT = {"token": "123456:T", "username": "AivoryBot", "bot_id": "123456", "agent_type": "autonomous"}
GROUP = {"id": -100, "type": "group", "title": "Team"}
SUPER = {"id": -1001, "type": "supergroup", "title": "Team"}
PRIVATE = {"id": 55, "type": "private"}
ALICE = {"id": 7, "is_bot": False, "first_name": "Alice", "last_name": "Lee"}


class FakeDB:
    def __init__(self):
        self.data = {}

    def save_json(self, c, i, d):
        self.data[(c, i)] = d
        return True

    def load_json(self, c, i):
        return self.data.get((c, i))

    def delete_json(self, c, i):
        return self.data.pop((c, i), None) is not None


def msg(text, chat=GROUP, sender=ALICE, **extra):
    m = {"message_id": 1, "chat": chat, "from": sender, "text": text}
    m.update(extra)
    return m


def mention(text="@AivoryBot hi"):
    return msg(text, entities=[{"type": "mention", "offset": 0, "length": 10}])


class AddressedToBotTests(unittest.TestCase):
    def test_mention_matches_case_insensitively(self):
        self.assertTrue(ts.addressed_to_bot(
            msg("hey @aivorybot", entities=[{"type": "mention", "offset": 4, "length": 10}]), BOT))

    def test_mention_of_other_bot_rejected(self):
        self.assertFalse(ts.addressed_to_bot(
            msg("@OtherBot hi", entities=[{"type": "mention", "offset": 0, "length": 9}]), BOT))

    def test_text_mention_by_id(self):
        m = msg("you", entities=[{"type": "text_mention", "offset": 0, "length": 3, "user": {"id": 123456}}])
        self.assertTrue(ts.addressed_to_bot(m, BOT))

    def test_reply_to_bot(self):
        m = msg("ok", reply_to_message={"from": {"id": 123456, "is_bot": True}})
        self.assertTrue(ts.addressed_to_bot(m, BOT))

    def test_reply_to_human_rejected(self):
        m = msg("ok", reply_to_message={"from": {"id": 9}})
        self.assertFalse(ts.addressed_to_bot(m, BOT))

    def test_plain_and_targeted_commands(self):
        ent = lambda n: [{"type": "bot_command", "offset": 0, "length": n}]
        self.assertTrue(ts.addressed_to_bot(msg("/help", entities=ent(5)), BOT))
        self.assertTrue(ts.addressed_to_bot(msg("/help@AivoryBot", entities=ent(15)), BOT))
        self.assertFalse(ts.addressed_to_bot(msg("/help@OtherBot", entities=ent(14)), BOT))

    def test_plain_chatter_rejected(self):
        self.assertFalse(ts.addressed_to_bot(msg("lunch?"), BOT))

    def test_caption_mention(self):
        m = {"chat": GROUP, "from": ALICE, "caption": "@AivoryBot see",
             "caption_entities": [{"type": "mention", "offset": 0, "length": 10}]}
        self.assertTrue(ts.addressed_to_bot(m, BOT))


class SenderNameTests(unittest.TestCase):
    def test_full_name(self):
        self.assertEqual(ts.sender_display_name(msg("x")), "Alice Lee")

    def test_username_fallback_and_unknown(self):
        self.assertEqual(ts.sender_display_name({"from": {"username": "al"}}), "al")
        self.assertEqual(ts.sender_display_name({}), "Unknown")

    def test_sanitised(self):
        n = ts.sender_display_name({"from": {"first_name": "A]\n[B"}})
        self.assertNotIn("\n", n)
        self.assertNotIn("]", n)


class ProcessUpdateTests(unittest.TestCase):
    def setUp(self):
        self.svc = TelegramService(FakeDB())
        self.svc._handle_message = MagicMock()
        self.svc._handle_start = MagicMock()

    def run_update(self, message):
        self.svc.process_update({"message": message}, BOT)

    def test_group_chatter_ignored(self):
        self.run_update(msg("lunch?"))
        self.svc._handle_message.assert_not_called()

    def test_group_mention_handled(self):
        self.run_update(mention())
        self.svc._handle_message.assert_called_once()

    def test_group_start_command_handled(self):
        self.run_update(msg("/start tok", entities=[{"type": "bot_command", "offset": 0, "length": 6}]))
        self.svc._handle_start.assert_called_once()

    def test_private_needs_no_mention(self):
        self.run_update(msg("hello", chat=PRIVATE))
        self.svc._handle_message.assert_called_once()

    def test_bot_sender_ignored_everywhere(self):
        bot_sender = {"id": 8, "is_bot": True, "first_name": "Other"}
        self.run_update(mention())  # sanity: would be handled
        self.svc._handle_message.reset_mock()
        m = mention()
        m["from"] = bot_sender
        self.run_update(m)
        self.run_update(msg("hi", chat=PRIVATE, sender=bot_sender))
        self.svc._handle_message.assert_not_called()


class PromptTests(unittest.TestCase):
    def _run(self, message):
        db = FakeDB()
        svc = TelegramService(db)
        binding = {"binding_id": "123456_-100", "user_id": "u1", "agent_type": "autonomous", "chat_id": -100}
        svc.get_binding = MagicMock(return_value=binding)
        svc._load_user = MagicMock(return_value={"user_id": "u1"})
        svc.send_typing = MagicMock()
        svc.send_message = MagicMock()
        svc._try_conversational_approval = MagicMock(return_value=None)
        svc._remember_pending_result = lambda b, r: r
        svc._route_to_agent = MagicMock(return_value={"reply": "ok"})
        svc._handle_message(message["chat"]["id"], message, BOT)
        return svc._route_to_agent.call_args[0][1]

    def test_group_prompt_has_sender(self):
        self.assertEqual(self._run(mention()), "[Alice Lee]: @AivoryBot hi")

    def test_private_prompt_unchanged(self):
        self.assertEqual(self._run(msg("hello", chat=PRIVATE)), "hello")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.svc = TelegramService(self.db)
        self.old = "123456_-100"
        self.new = "123456_-1001"
        self.db.save_json(ts.BINDINGS_COLLECTION, self.old, {
            "binding_id": self.old, "bot_id": "123456", "chat_id": -100,
            "chat_type": "group", "user_id": "u1", "agent_type": "autonomous"})
        self.db.save_json(ts.PENDING_COLLECTION, self.old, {"pending_id": "pa_1"})

    def _migrate_to(self):
        self.svc.process_update({"message": {"chat": GROUP, "migrate_to_chat_id": -1001}}, BOT)

    @patch("app.services.teams.rename_channel_ref")
    def test_binding_moves(self, rename):
        self._migrate_to()
        self.assertIsNone(self.db.load_json(ts.BINDINGS_COLLECTION, self.old))
        b = self.db.load_json(ts.BINDINGS_COLLECTION, self.new)
        self.assertEqual((b["chat_id"], b["chat_type"], b["binding_id"]), (-1001, "supergroup", self.new))
        self.assertEqual(b["session_id"], self.old)  # session history preserved
        self.assertEqual(self.db.load_json(ts.PENDING_COLLECTION, self.new), {"pending_id": "pa_1"})
        self.assertIsNone(self.db.load_json(ts.PENDING_COLLECTION, self.old))
        rename.assert_called_once_with("u1", "telegram", self.old, self.new)

    @patch("app.services.teams.rename_channel_ref", side_effect=RuntimeError("no db"))
    def test_team_failure_does_not_block(self, _):
        self._migrate_to()
        self.assertIsNotNone(self.db.load_json(ts.BINDINGS_COLLECTION, self.new))

    @patch("app.services.teams.rename_channel_ref")
    def test_migrate_from_is_idempotent(self, _):
        self._migrate_to()
        self.svc.process_update({"message": {"chat": SUPER, "migrate_from_chat_id": -100}}, BOT)
        b = self.db.load_json(ts.BINDINGS_COLLECTION, self.new)
        self.assertEqual(b["chat_id"], -1001)

    def test_unbound_group_is_noop(self):
        self.svc.process_update({"message": {"chat": {"id": -5, "type": "group"}, "migrate_to_chat_id": -6}}, BOT)
        self.assertIsNone(self.db.load_json(ts.BINDINGS_COLLECTION, "123456_-6"))

    def test_session_id_used_for_routing(self):
        with patch.object(ts, "settings") as st, patch.object(ts.requests, "post") as post, \
             patch.object(ts, "agent_run_tracker"):
            st.telegram_agent_gateway_url = "http://gw"
            post.return_value = MagicMock(ok=True, json=lambda: {"reply": "r"})
            b = {"binding_id": self.new, "session_id": self.old, "user_id": "u1",
                 "agent_type": "autonomous", "chat_id": -1001}
            with patch.object(TelegramService, "_team_active_agents", return_value=None):
                self.svc._route_to_agent(b, "hi")
            self.assertEqual(post.call_args.kwargs["json"]["session_id"], self.old)


if __name__ == "__main__":
    unittest.main()
