#!/usr/bin/env python3
"""
Unit tests for server-side Team Space reminder firing (ADR-021 §4.1) and the
turn_origin / run_outcome relay of discussion turns (ADR-021 §4.3).

Run with `JWT_SECRET=test-only-secret python -m unittest discover -s tests -p 'test_room_reminder_sweeper.py'`
from the avry-backend root. No database or network: the sweeper runs against
a scripted cursor and a fake fire function.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jwt  # noqa: E402

from app.services import room_reminder_sweeper as sweeper  # noqa: E402
from app.services import telegram_service as ts  # noqa: E402
from app.services.jwt_secret import JWT_SECRET  # noqa: E402
from app.services.token_kinds import is_access_payload  # noqa: E402


class FakeCursor:
    """Answers the sweeper's queries from in-memory state and records writes."""

    def __init__(self, *, lock=True, pairs=(), users=None):
        self.lock = lock
        self.pairs = list(pairs)
        self.users = users or {}
        self.handed_back = []
        self.unlocked = False
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if "pg_try_advisory_lock" in sql:
            self._result = [(self.lock,)]
        elif "pg_advisory_unlock" in sql:
            self.unlocked = True
            self._result = [(True,)]
        elif "GROUP BY space_id, credential_owner" in sql:
            self._result = self.pairs
        elif "FROM users" in sql:
            row = self.users.get(params[0])
            self._result = [row] if row else []
        elif "SET credential_owner = NULL" in sql:
            self.handed_back.append(params)
            self._result = []
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return self._result


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


class MintTokenTest(unittest.TestCase):
    def test_token_is_a_short_lived_access_token_for_the_owner(self):
        now = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
        token = sweeper.mint_room_token(
            {"user_id": "u1", "email": "a@b.c", "account_type": "business"}, now=now
        )
        payload = jwt.decode(
            token, JWT_SECRET, algorithms=["HS256"], options={"verify_exp": False, "verify_iat": False}
        )
        self.assertEqual(payload["user_id"], "u1")
        self.assertEqual(payload["via"], "room-reminder")
        self.assertTrue(is_access_payload(payload))
        self.assertNotIn("session_id", payload)
        self.assertEqual(payload["exp"] - payload["iat"], sweeper.TOKEN_TTL_MINUTES * 60)


class SweepTest(unittest.TestCase):
    def test_fires_each_due_room_with_its_owners_token(self):
        cur = FakeCursor(
            pairs=[("space-1", "u1"), ("space-2", "u2")],
            users={"u1": ("u1", "a@x", "business", True), "u2": ("u2", "b@x", "operational", True)},
        )
        calls = []

        def fake_fire(base, space, token):
            calls.append((base, space, jwt.decode(token, JWT_SECRET, algorithms=["HS256"])["user_id"]))
            return 200

        fired = sweeper.sweep_once(FakeConn(cur), "http://dash:9001/dashboard", fake_fire)
        self.assertEqual(fired, 2)
        self.assertEqual(
            calls,
            [("http://dash:9001/dashboard", "space-1", "u1"), ("http://dash:9001/dashboard", "space-2", "u2")],
        )
        self.assertTrue(cur.unlocked)
        self.assertEqual(cur.handed_back, [])

    def test_another_worker_holding_the_lock_means_no_sweep(self):
        cur = FakeCursor(lock=False, pairs=[("space-1", "u1")])
        fire = MagicMock()
        self.assertEqual(sweeper.sweep_once(FakeConn(cur), "http://d", fire), 0)
        fire.assert_not_called()

    def test_gone_inactive_or_forbidden_owners_are_handed_back_to_the_room_poll(self):
        cur = FakeCursor(
            pairs=[("s-gone", "gone"), ("s-off", "off"), ("s-403", "left")],
            users={"off": ("off", "o@x", "business", False), "left": ("left", "l@x", "business", True)},
        )
        fired = sweeper.sweep_once(FakeConn(cur), "http://d", lambda *_: 403)
        self.assertEqual(fired, 0)
        self.assertEqual(cur.handed_back, [("s-gone", "gone"), ("s-off", "off"), ("s-403", "left")])

    def test_unreachable_dashboard_stops_the_tick_and_keeps_the_reminders(self):
        cur = FakeCursor(
            pairs=[("s1", "u1"), ("s2", "u1")],
            users={"u1": ("u1", "a@x", "business", True)},
        )
        calls = []

        def down(base, space, token):
            calls.append(space)
            raise sweeper.requests.ConnectionError("refused")

        self.assertEqual(sweeper.sweep_once(FakeConn(cur), "http://d", down), 0)
        self.assertEqual(calls, ["s1"])
        self.assertEqual(cur.handed_back, [])
        self.assertTrue(cur.unlocked)

    def test_tick_is_idle_unless_enabled_and_configured(self):
        for enabled, url in [(False, "http://d"), (True, None)]:
            with (
                patch.object(sweeper, "settings", MagicMock(room_reminder_sweeper_enabled=enabled, dashboard_internal_url=url)),
                patch.object(sweeper, "_connect", side_effect=AssertionError("must not connect")),
            ):
                sweeper._tick()

    def test_fire_posts_to_the_rooms_reminder_route(self):
        with patch.object(sweeper.requests, "post", return_value=MagicMock(status_code=202)) as post:
            status = sweeper.fire("http://dash:9001/dashboard/", "workspace:abc/1", "tok")
        self.assertEqual(status, 202)
        url = post.call_args.args[0]
        self.assertEqual(url, "http://dash:9001/dashboard/api/workspace/workspace%3Aabc%2F1/reminders/fire")
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})


class TurnOriginRelayTest(unittest.TestCase):
    def test_only_unattended_origins_are_kept(self):
        self.assertEqual(ts.unattended_turn_origin("reminder"), "reminder")
        self.assertEqual(ts.unattended_turn_origin(" Wake "), "wake")
        self.assertIsNone(ts.unattended_turn_origin("human"))
        self.assertIsNone(ts.unattended_turn_origin(None))

    def test_run_outcome_is_relayed_only_when_well_formed(self):
        self.assertEqual(
            ts.run_outcome_fields({"run_outcome": "failed", "run_failure_reason": "Odoo down"}),
            {"run_outcome": "failed", "run_failure_reason": "Odoo down"},
        )
        self.assertEqual(
            ts.run_outcome_fields({"run_outcome": "quiet", "run_failure_reason": "x"}),
            {"run_outcome": "quiet", "run_failure_reason": None},
        )
        self.assertEqual(
            ts.run_outcome_fields({"run_outcome": "bogus"}),
            {"run_outcome": None, "run_failure_reason": None},
        )

    def test_discussion_turn_forwards_turn_origin_and_relays_the_outcome(self):
        svc = ts.TelegramService.__new__(ts.TelegramService)
        captured = {}

        class FakeResp:
            ok = True

            def json(self):
                return {"reply": "Nothing new.", "team_actions": [], "run_outcome": "quiet"}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured.update(json)
            return FakeResp()

        with (
            patch("requests.post", side_effect=fake_post),
            patch.object(ts, "settings", MagicMock(telegram_agent_gateway_url="http://x")),
            patch.object(ts.TelegramService, "_try_conversational_approval", return_value=None),
            patch.object(
                ts.TelegramService, "_remember_pending_result",
                side_effect=lambda binding, result: result,
            ),
        ):
            out = svc.route_discussion_message(
                {"user_id": "u1"}, "autonomous", "space-1", "root-1", "⏰ check leads",
                turn_origin="reminder",
            )
            self.assertEqual(captured["turn_origin"], "reminder")
            self.assertEqual(out["run_outcome"], "quiet")

            captured.clear()
            svc.route_discussion_message(
                {"user_id": "u1"}, "autonomous", "space-1", "root-1", "hi", turn_origin="human",
            )
            self.assertNotIn("turn_origin", captured)


if __name__ == "__main__":
    unittest.main()
