#!/usr/bin/env python3
"""
Unit tests for Telegram voice-note transcription.

Run with `python3 -m unittest tests.test_telegram_voice_notes`.
No network: the Groq STT call and Bot API file download are mocked.

  1. A voice note is transcribed and reaches the agent as a transcript.
  2. A missing key / STT failure degrades to a note, never an exception.
  3. Over-long voice notes are refused without calling the STT API.
  4. Text and photo messages are unchanged.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import attachment_extractor as ax  # noqa: E402
from app.services.telegram_service import TelegramService  # noqa: E402

BOT = {"token": "123456:T", "username": "AivoryBot", "bot_id": "123456", "agent_type": "autonomous"}
PRIVATE = {"id": 55, "type": "private"}
ALICE = {"id": 7, "is_bot": False, "first_name": "Alice"}


def _resp(ok=True, body=None, status=200):
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.text = "err"
    r.json.return_value = body or {}
    return r


def voice_msg(duration=5, **extra):
    m = {
        "message_id": 1,
        "chat": PRIVATE,
        "from": ALICE,
        "voice": {"file_id": "F1", "duration": duration, "mime_type": "audio/ogg"},
    }
    m.update(extra)
    return m


class TranscribeAudioTests(unittest.TestCase):
    def test_returns_transcript(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test"}), patch.object(
            ax.requests, "post", return_value=_resp(body={"text": " Halo dunia "})
        ) as post:
            self.assertEqual(ax.transcribe_audio(b"ogg"), "Halo dunia")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer gsk_test")
        self.assertEqual(kwargs["data"]["model"], ax.STT_MODEL)
        self.assertNotIn("language", kwargs["data"])  # auto-detect

    def test_no_key_returns_none_without_calling(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(ax.requests, "post") as post:
            os.environ.pop("GROQ_API_KEY", None)
            self.assertIsNone(ax.transcribe_audio(b"ogg"))
        post.assert_not_called()

    def test_http_error_returns_none(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test"}), patch.object(
            ax.requests, "post", return_value=_resp(ok=False, status=500)
        ):
            self.assertIsNone(ax.transcribe_audio(b"ogg"))

    def test_network_error_returns_none(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test"}), patch.object(
            ax.requests, "post", side_effect=ax.requests.ConnectionError("down")
        ):
            self.assertIsNone(ax.transcribe_audio(b"ogg"))

    def test_too_large_or_long_not_sent(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test"}), patch.object(
            ax.requests, "post"
        ) as post:
            big = ax.transcribe_audio(b"x" * (ax.MAX_AUDIO_BYTES + 1))
            long_ = ax.transcribe_audio(b"ogg", duration=ax.MAX_AUDIO_SECONDS + 1)
        self.assertIn("too long", big)
        self.assertIn("too long", long_)
        post.assert_not_called()


class VoicePromptTests(unittest.TestCase):
    def setUp(self):
        self.svc = TelegramService(MagicMock())

    def test_voice_becomes_transcript_prompt(self):
        with patch.object(self.svc, "_download_file", return_value=b"ogg"), patch.object(
            ax, "transcribe_audio", return_value="tolong cek stok"
        ):
            prompt = self.svc._build_prompt(BOT, voice_msg())
        self.assertIn("[Voice note transcript]\ntolong cek stok", prompt)
        self.assertIn("what you heard", prompt)

    def test_stt_failure_degrades_to_note(self):
        with patch.object(self.svc, "_download_file", return_value=b"ogg"), patch.object(
            ax, "transcribe_audio", return_value=None
        ):
            prompt = self.svc._build_prompt(BOT, voice_msg())
        self.assertIn("could not be transcribed", prompt)

    def test_download_failure_degrades_to_note(self):
        with patch.object(self.svc, "_download_file", return_value=None):
            prompt = self.svc._build_prompt(BOT, voice_msg())
        self.assertIn("could not be transcribed", prompt)

    def test_overlong_voice_skips_download_and_stt(self):
        with patch.object(self.svc, "_download_file") as dl, patch.object(
            ax, "transcribe_audio"
        ) as stt:
            prompt = self.svc._build_prompt(BOT, voice_msg(duration=ax.MAX_AUDIO_SECONDS + 1))
        dl.assert_not_called()
        stt.assert_not_called()
        self.assertIn("too long", prompt)

    def test_audio_file_uses_its_name_and_mime(self):
        m = {
            "chat": PRIVATE,
            "from": ALICE,
            "audio": {"file_id": "A1", "duration": 30, "file_name": "memo.m4a", "mime_type": "audio/mp4"},
        }
        with patch.object(self.svc, "_download_file", return_value=b"m4a"), patch.object(
            ax, "transcribe_audio", return_value="ok"
        ) as stt:
            self.svc._build_prompt(BOT, m)
        stt.assert_called_once_with(b"m4a", "memo.m4a", "audio/mp4", 30)

    def test_plain_text_unchanged(self):
        prompt = self.svc._build_prompt(BOT, {"chat": PRIVATE, "from": ALICE, "text": "halo"})
        self.assertEqual(prompt, "halo")


class VoiceRoutingTests(unittest.TestCase):
    def test_voice_message_reaches_handle_message(self):
        svc = TelegramService(MagicMock())
        with patch.object(svc, "_handle_message") as handle:
            svc.process_update({"message": voice_msg()}, BOT)
        handle.assert_called_once()

    def test_voice_skips_conversational_approval(self):
        svc = TelegramService(MagicMock())
        binding = {"user_id": "u1", "agent_type": "autonomous", "chat_id": 55}
        m = voice_msg(caption="ya")
        with patch.object(svc, "get_binding", return_value=binding), patch.object(
            svc, "_load_user", return_value={"id": "u1"}
        ), patch.object(svc, "_is_active", return_value=True), patch.object(
            svc, "_try_conversational_approval"
        ) as approval, patch.object(svc, "send_typing"), patch.object(
            svc, "_build_prompt", return_value="p"
        ), patch.object(
            svc, "_route_to_agent", return_value={"reply": "r", "pending_approval": None, "team_actions": []}
        ), patch.object(
            svc, "_remember_pending_result", side_effect=lambda b, r: r
        ), patch.object(svc, "send_message"):
            svc._handle_message(55, m, BOT)
        approval.assert_not_called()


if __name__ == "__main__":
    unittest.main()
