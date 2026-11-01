#!/usr/bin/env python3
"""
Console streaming: route_console_message_stream relays the bridge's SSE as
chunk/done frames and degrades to one chunk + done for plain JSON answers.

Run with `python3 -m unittest tests.test_console_stream` from the avry-backend root.
All network calls are mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import telegram_service as ts  # noqa: E402

USER = {"user_id": "u1", "account_type": "free"}


def sse_response(lines):
    resp = MagicMock()
    resp.ok = True
    resp.headers = {"content-type": "text/event-stream; charset=utf-8"}
    resp.iter_lines.return_value = iter(lines)
    return resp


def json_response(payload, ok=True, status=200):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status
    resp.text = "x"
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload
    return resp


def run_stream(resp, remember=None):
    svc = ts.TelegramService.__new__(ts.TelegramService)
    remember = remember or (lambda binding, result: result)
    with (
        patch.object(ts.settings, "telegram_agent_gateway_url", "http://bridge"),
        patch.object(ts.TelegramService, "_try_conversational_approval", return_value=None),
        patch.object(ts.TelegramService, "_team_active_agents", return_value=None),
        patch.object(ts.TelegramService, "_remember_pending_result", side_effect=remember),
        patch.object(ts.agent_run_tracker, "mark_running"),
        patch.object(ts.agent_run_tracker, "clear_running") as cleared,
        patch.object(ts.requests, "post", return_value=resp) as post,
    ):
        frames = list(svc.route_console_message_stream(USER, "leads_qualifier", "hi", "c1"))
    return frames, post, cleared


class ConsoleStreamTest(unittest.TestCase):
    def test_relays_chunks_then_done_and_asks_the_bridge_to_stream(self):
        resp = sse_response([
            'data: {"type":"chunk","content":"Hal"}', "",
            ": keep-alive", "",
            'data: {"type":"chunk","content":"o"}', "",
            'data: {"type":"done","pending_approval":null}', "",
        ])
        frames, post, cleared = run_stream(resp)
        self.assertEqual(
            frames,
            [
                {"type": "chunk", "content": "Hal"},
                {"type": "ping"},
                {"type": "chunk", "content": "o"},
                {"type": "done", "pending_approval": None},
            ],
        )
        kwargs = post.call_args.kwargs
        self.assertTrue(kwargs["json"]["stream"])
        self.assertEqual(kwargs["json"]["channel"], "console")
        self.assertEqual(kwargs["json"]["session_id"], "console_u1_c1")
        self.assertTrue(kwargs["stream"])
        resp.close.assert_called_once()
        cleared.assert_called_once()

    def test_tool_progress_frames_are_relayed_without_extra_fields(self):
        resp = sse_response([
            'data: {"type":"tool","id":"t1","name":"crm_update_lead","status":"running","args":{"email":"a@b.c"}}', "",
            'data: {"type":"tool","id":"t1","name":"crm_update_lead","status":"done"}', "",
            'data: {"type":"tool","id":"t2","name":"x","status":"weird"}', "",
            'data: {"type":"done","pending_approval":null}', "",
        ])
        frames, _, _ = run_stream(resp)
        self.assertEqual(
            frames,
            [
                {"type": "tool", "id": "t1", "name": "crm_update_lead", "status": "running"},
                {"type": "tool", "id": "t1", "name": "crm_update_lead", "status": "done"},
                {"type": "done", "pending_approval": None},
            ],
        )

    def test_pending_approval_hint_arrives_as_a_last_chunk(self):
        pending = {"id": "p1", "tool_name": "send_mail", "risk_tier": "high"}

        def remember(binding, result):
            return {**result, "reply": ts._APPROVAL_HINT}

        resp = sse_response(['data: {"type":"done","pending_approval":{"id":"p1","tool_name":"send_mail","risk_tier":"high"}}', ""])
        frames, _, _ = run_stream(resp, remember)
        self.assertEqual(frames[0], {"type": "chunk", "content": ts._APPROVAL_HINT})
        self.assertEqual(frames[-1], {"type": "done", "pending_approval": pending})

    def test_json_answer_becomes_one_chunk_and_done(self):
        frames, _, _ = run_stream(json_response({"reply": "Kredit habis", "pending_approval": None}))
        self.assertEqual(
            frames,
            [{"type": "chunk", "content": "Kredit habis"}, {"type": "done", "pending_approval": None}],
        )

    def test_gateway_error_status_is_a_friendly_reply_not_an_exception(self):
        frames, _, cleared = run_stream(json_response({}, ok=False, status=502))
        self.assertEqual(frames[0]["content"], ts._AGENT_UNAVAILABLE_REPLY)
        self.assertEqual(frames[-1]["type"], "done")
        cleared.assert_called_once()

    def test_upstream_error_frame_ends_with_error(self):
        frames, _, _ = run_stream(sse_response(['data: {"type":"error","message":"LLM request failed"}', ""]))
        self.assertEqual(frames, [{"type": "error", "message": "LLM request failed"}])

    def test_stream_closing_without_a_terminal_frame_is_an_error(self):
        frames, _, _ = run_stream(sse_response(['data: {"type":"chunk","content":"x"}', ""]))
        self.assertEqual(frames[-1]["type"], "error")


if __name__ == "__main__":
    unittest.main()
