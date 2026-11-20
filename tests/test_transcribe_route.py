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
        self.assertEqual(m.call_args.kwargs["language"], "id")  # hint beats misdetection
        self.assertTrue(m.call_args.kwargs["prompt"])

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


if __name__ == "__main__":
    unittest.main()
