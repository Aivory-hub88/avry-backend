#!/usr/bin/env python3
"""
Long agent turns must keep the connection alive through Cloudflare (~120 s).
Run: JWT_SECRET=... python3 -m unittest tests.test_agent_chat_keepalive
"""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

from app.routes import telegram as tg  # noqa: E402


def run(coro):
    return asyncio.run(coro)


async def collect(resp):
    chunks = []
    async for c in resp.body_iterator:
        chunks.append(c if isinstance(c, bytes) else c.encode())
    return chunks


class KeepaliveTest(unittest.TestCase):
    def test_fast_call_is_a_plain_dict(self):
        out = run(tg._run_with_keepalive(lambda: {"reply": "ok"}, first_wait=1.0))
        self.assertEqual(out, {"reply": "ok"})

    def test_fast_validation_error_keeps_its_http_status(self):
        def bad():
            raise HTTPException(status_code=403, detail="nope")

        with self.assertRaises(HTTPException) as c:
            run(tg._run_with_keepalive(bad, first_wait=1.0))
        self.assertEqual(c.exception.status_code, 403)

    def test_slow_call_streams_spaces_then_the_json(self):
        def slow():
            time.sleep(0.35)
            return {"reply": "selesai", "pending_approval": None}

        async def go():
            resp = await tg._run_with_keepalive(slow, first_wait=0.05, interval=0.1)
            self.assertIsInstance(resp, StreamingResponse)
            return await collect(resp)

        chunks = run(go())
        self.assertEqual(chunks[0], b" ")                      # response starts immediately
        self.assertGreaterEqual(chunks[:-1].count(b" "), 3)    # and keeps beating while it waits
        body = b"".join(chunks)
        self.assertEqual(json.loads(body), {"reply": "selesai", "pending_approval": None})  # leading spaces are valid JSON

    def test_slow_failure_is_reported_in_the_body(self):
        def slow_bad():
            time.sleep(0.2)
            raise HTTPException(status_code=403, detail="tier")

        async def go():
            resp = await tg._run_with_keepalive(slow_bad, first_wait=0.05, interval=0.1)
            return await collect(resp)

        body = json.loads(b"".join(run(go())))
        self.assertEqual(body, {"detail": "tier", "status_code": 403})

    def test_slow_crash_does_not_leak_internals(self):
        def boom():
            time.sleep(0.2)
            raise RuntimeError("secret internal detail")

        async def go():
            resp = await tg._run_with_keepalive(boom, first_wait=0.05, interval=0.1)
            return await collect(resp)

        body = json.loads(b"".join(run(go())))
        self.assertEqual(body["status_code"], 500)
        self.assertNotIn("secret", json.dumps(body))

    def test_routes_are_wired_through_the_keepalive(self):
        paths = {r.path: r for r in tg.router.routes}
        for p in ("/api/v1/telegram/agent-chat", "/api/v1/telegram/discussion-turn"):
            self.assertTrue(asyncio.iscoroutinefunction(paths[p].endpoint), p)


if __name__ == "__main__":
    unittest.main()
