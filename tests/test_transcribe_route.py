"""Unit tests for POST /api/v1/transcribe (console speech-to-prompt).

Run with `python3 -m unittest tests.test_transcribe_route`.
No network: transcribe_audio is mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.routes import transcribe as tr  # noqa: E402
from app.routes.agent_actions import get_current_user_payload  # noqa: E402


def _client(authed=True):
    app = FastAPI()
    app.include_router(tr.router)
    if authed:
        app.dependency_overrides[get_current_user_payload] = lambda: {"user_id": "u1"}
    return TestClient(app)


def _post(client, data=b"abc", ctype="audio/webm;codecs=opus"):
    return client.post("/api/v1/transcribe", files={"file": ("voice.webm", data, ctype)})


class TranscribeRouteTests(unittest.TestCase):
    def test_returns_transcript(self):
        with patch.object(tr.ax, "transcribe_audio", return_value="halo dunia") as m, \
             patch.object(tr, "check_rate_limit", return_value=True):
            r = _post(_client())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"text": "halo dunia"})
        self.assertEqual(m.call_args.args[2], "audio/webm")  # codecs suffix stripped
        self.assertIsNone(m.call_args.kwargs.get("language"))  # no forced language
        self.assertEqual(m.call_args.kwargs["fallback_language"], "id")
        self.assertTrue(m.call_args.kwargs["prompt"])

    def test_uses_accurate_model_and_folds_terms_into_prompt(self):
        with patch.object(tr.ax, "transcribe_audio", return_value="ok") as m, \
             patch.object(tr, "check_rate_limit", return_value=True):
            r = _client().post(
                "/api/v1/transcribe",
                files={"file": ("voice.webm", b"abc", "audio/webm")},
                data={"terms": "Lex, Geno,Aira"},
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(m.call_args.kwargs["model"], "whisper-large-v3")
        self.assertIn("Lex, Geno, Aira", m.call_args.kwargs["prompt"])

    def test_requires_auth(self):
        r = _post(_client(authed=False))
        self.assertEqual(r.status_code, 401)

    def test_empty_audio_is_400(self):
        with patch.object(tr, "check_rate_limit", return_value=True):
            r = _post(_client(), data=b"")
        self.assertEqual(r.status_code, 400)

    def test_oversize_is_413_without_calling_stt(self):
        with patch.object(tr.ax, "MAX_AUDIO_BYTES", 4), \
             patch.object(tr.ax, "transcribe_audio") as m, \
             patch.object(tr, "check_rate_limit", return_value=True):
            r = _post(_client(), data=b"12345")
        self.assertEqual(r.status_code, 413)
        m.assert_not_called()

    def test_rate_limited_is_429(self):
        with patch.object(tr, "check_rate_limit", return_value=False):
            r = _post(_client())
        self.assertEqual(r.status_code, 429)

    def test_stt_failure_is_502(self):
        with patch.object(tr.ax, "transcribe_audio", return_value=None), \
             patch.object(tr, "check_rate_limit", return_value=True):
            r = _post(_client())
        self.assertEqual(r.status_code, 502)


class LanguageGuardTests(unittest.TestCase):
    """transcribe_audio(fallback_language=...): flexible detect, retry only on odd languages."""

    def _run(self, detected_then):
        from app.services import attachment_extractor as ax

        calls = []

        def fake(api_key, data, filename, mime, *, language=None, prompt=None, verbose=False, model=None):
            calls.append({"language": language, "verbose": verbose})
            return detected_then[len(calls) - 1]

        with patch.dict("os.environ", {"GROQ_API_KEY": "k"}), \
             patch.object(ax, "_stt_call", side_effect=fake):
            out = ax.transcribe_audio(b"x", fallback_language="id")
        return out, calls

    def test_english_is_kept_without_retry(self):
        out, calls = self._run([("send the report", "english")])
        self.assertEqual(out, "send the report")
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["language"])
        self.assertTrue(calls[0]["verbose"])

    def test_indonesian_is_kept_without_retry(self):
        out, calls = self._run([("terima kasih", "indonesian")])
        self.assertEqual(out, "terima kasih")
        self.assertEqual(len(calls), 1)

    def test_odd_language_retries_with_fallback(self):
        out, calls = self._run([("Hvala lengs.", "croatian"), ("terima kasih", "indonesian")])
        self.assertEqual(out, "terima kasih")
        self.assertEqual([c["language"] for c in calls], [None, "id"])

    def test_empty_retry_keeps_first_text(self):
        out, _ = self._run([("Hvala lengs.", "croatian"), ("", "indonesian")])
        self.assertEqual(out, "Hvala lengs.")

    def test_no_fallback_is_legacy_single_call(self):
        from app.services import attachment_extractor as ax

        with patch.dict("os.environ", {"GROQ_API_KEY": "k"}), \
             patch.object(ax, "_stt_call", return_value=("hola", "")) as m:
            self.assertEqual(ax.transcribe_audio(b"x"), "hola")
        self.assertFalse(m.call_args.kwargs["verbose"])


class BuildPromptTests(unittest.TestCase):
    def test_no_terms_is_the_base_prompt(self):
        self.assertEqual(tr.build_prompt(""), tr._PROMPT)

    def test_dedupes_and_drops_junk(self):
        p = tr.build_prompt("Lex, lex, <script>, " + "x" * 80 + ", ,Geno")
        self.assertIn("Lex, Geno", p)
        self.assertNotIn("script", p)
        self.assertNotIn("xxxx", p)

    def test_caps_term_count_and_length(self):
        p = tr.build_prompt(",".join(f"Name{i}" for i in range(100)))
        self.assertIn("Name19", p)
        self.assertNotIn("Name20", p)
        self.assertLessEqual(len(p), tr._MAX_PROMPT_CHARS)


if __name__ == "__main__":
    unittest.main()
