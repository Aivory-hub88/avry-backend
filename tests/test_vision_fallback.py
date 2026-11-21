#!/usr/bin/env python3
"""describe_image falls back to another model when the primary is rate-limited.

Run with `JWT_SECRET=x python3 -m unittest tests.test_vision_fallback`. No network.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import attachment_extractor as ax  # noqa: E402


def resp(status=200, content="a description"):
    r = MagicMock()
    r.ok = status < 400
    r.status_code = status
    r.text = "err"
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    return r


@patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"})
class VisionFallback(unittest.TestCase):
    def test_falls_back_after_429(self):
        with patch.object(ax.requests, "post", side_effect=[resp(429), resp(200, "from fallback")]) as post:
            self.assertEqual(ax.describe_image(b"x", "image/png"), "from fallback")
        models = [c.kwargs["json"]["model"] for c in post.call_args_list]
        self.assertEqual(models[0], ax.VISION_MODEL)
        self.assertNotEqual(models[1], models[0])

    def test_stops_on_client_error(self):
        with patch.object(ax.requests, "post", return_value=resp(401)) as post:
            self.assertIsNone(ax.describe_image(b"x", "image/png"))
        self.assertEqual(post.call_count, 1)

    def test_all_models_down_returns_none(self):
        with patch.object(ax.requests, "post", return_value=resp(429)) as post:
            self.assertIsNone(ax.describe_image(b"x", "image/png"))
        self.assertEqual(post.call_count, 1 + len([m for m in ax.VISION_FALLBACK_MODELS if m != ax.VISION_MODEL]))

    def test_primary_success_makes_one_call(self):
        with patch.object(ax.requests, "post", return_value=resp(200)) as post:
            self.assertEqual(ax.describe_image(b"x", "image/png"), "a description")
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
