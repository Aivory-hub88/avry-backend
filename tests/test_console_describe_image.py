#!/usr/bin/env python3
"""Tests for POST /telegram/describe-image (vision pass for Console image attachments).

Run with `python3 -m unittest tests.test_console_describe_image`. No network.
"""

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app.routes import telegram as tg  # noqa: E402

USER = {"user_id": "u1"}
PNG_B64 = base64.b64encode(b"\x89PNG fake bytes").decode()


def call(**kw):
    body = tg.DescribeImageRequest(**{"data_b64": PNG_B64, "mime": "image/png", **kw})
    return tg.describe_console_image(body, USER)


class DescribeConsoleImage(unittest.TestCase):
    @patch.object(tg, "_require_paid_agents")
    @patch("app.services.attachment_extractor.describe_image", return_value="A screenshot of a website")
    def test_returns_description(self, describe, _paid):
        out = call(caption="lo bisa lihat ini?")
        self.assertEqual(out, {"description": "A screenshot of a website"})
        data, mime, caption = describe.call_args.args
        self.assertEqual(data, b"\x89PNG fake bytes")
        self.assertEqual(mime, "image/png")
        self.assertEqual(caption, "lo bisa lihat ini?")

    @patch.object(tg, "_require_paid_agents")
    def test_rejects_non_image_mime(self, _paid):
        with self.assertRaises(HTTPException) as e:
            call(mime="application/pdf")
        self.assertEqual(e.exception.status_code, 400)

    @patch.object(tg, "_require_paid_agents")
    def test_rejects_bad_base64(self, _paid):
        with self.assertRaises(HTTPException) as e:
            call(data_b64="not base64!!")
        self.assertEqual(e.exception.status_code, 400)

    @patch.object(tg, "_require_paid_agents")
    @patch("app.services.attachment_extractor.describe_image", return_value=None)
    def test_vision_failure_is_502(self, _d, _paid):
        with self.assertRaises(HTTPException) as e:
            call()
        self.assertEqual(e.exception.status_code, 502)

    def test_paid_gate_applies(self):
        with patch.object(tg, "_require_paid_agents", side_effect=HTTPException(status_code=402, detail="x")):
            with self.assertRaises(HTTPException) as e:
                call()
        self.assertEqual(e.exception.status_code, 402)


if __name__ == "__main__":
    unittest.main()
