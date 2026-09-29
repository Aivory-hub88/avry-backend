#!/usr/bin/env python3
"""
Unit tests for ADR-020 acting-as on Workspace agent turns.

Run with `python3 -m unittest tests.test_workspace_acting` from the
avry-backend root. DB and network are mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app.routes import telegram as tg  # noqa: E402
from app.services import workspace_acting as wa  # noqa: E402


def facts(owner="leader", acl=None, member=None):
    return {"doc_owner": owner, "acl_role": acl, "member_role": member}


class DecideTest(unittest.TestCase):
    def d(self, requested_by="member", acting_as="leader", **kw):
        return wa.decide(requested_by=requested_by, acting_as=acting_as, **facts(**kw))

    def test_editor_member_may_act_as_owner(self):
        self.assertTrue(self.d(member="editor"))
        self.assertTrue(self.d(member="owner"))

    def test_acl_editor_may(self):
        self.assertTrue(self.d(acl="editor"))

    def test_viewer_may_not(self):
        self.assertFalse(self.d(member="viewer"))
        self.assertFalse(self.d(acl="viewer"))

    def test_acl_overrides_membership(self):
        # explicit doc ACL wins over workspace membership (dashboard precedence)
        self.assertFalse(self.d(acl="viewer", member="editor"))

    def test_stranger_may_not(self):
        self.assertFalse(self.d())

    def test_cannot_name_a_non_owner(self):
        # a member forging acting_as = some other user, even a real one
        self.assertFalse(self.d(acting_as="victim", member="editor"))

    def test_ownerless_or_missing_doc_denies(self):
        self.assertFalse(self.d(owner=None, member="editor"))

    def test_owner_acting_as_self(self):
        self.assertTrue(self.d(requested_by="leader"))

    def test_empty_ids_deny(self):
        self.assertFalse(self.d(requested_by=""))
        self.assertFalse(self.d(acting_as=""))


class CanActAsFailClosedTest(unittest.TestCase):
    def test_lookup_error_denies(self):
        with patch.object(wa, "fetch_space_facts", side_effect=RuntimeError("db down")):
            self.assertFalse(wa.can_act_as("s1", "member", "leader"))

    def test_happy_path(self):
        with patch.object(wa, "fetch_space_facts", return_value=facts(member="editor")):
            self.assertTrue(wa.can_act_as("s1", "member", "leader"))


class DiscussionTurnRouteTest(unittest.TestCase):
    def call(self, acting_as, can_act=True, leader_exists=True, member_id="member"):
        body = tg.DiscussionTurnRequest(
            agent_type="leads_qualifier",
            space_id="s1",
            thread_root="t1",
            text="hi",
            acting_as=acting_as,
        )
        seen = {}

        def load_user(uid):
            if uid == "leader" and not leader_exists:
                return None
            return {"user_id": uid, "account_type": "business"}

        def route(record, agent_type, space, root, text):
            seen["tenant"] = record["user_id"]
            return {"reply": "ok", "pending_approval": None}

        with (
            patch.object(tg.workspace_acting, "can_act_as", return_value=can_act) as gate,
            patch.object(tg.telegram_service, "_load_user", side_effect=load_user),
            patch.object(tg, "agent_tier_error", return_value=None),
            patch.object(tg.telegram_service, "route_discussion_message", side_effect=route),
        ):
            out = tg.discussion_turn(body, user={"user_id": member_id})
        return out, seen, gate

    def test_member_turn_runs_in_leader_tenant(self):
        out, seen, gate = self.call("leader")
        self.assertEqual(seen["tenant"], "leader")
        gate.assert_called_once_with("s1", "member", "leader")
        self.assertEqual(out["reply"], "ok")

    def test_no_acting_as_keeps_caller_tenant(self):
        _, seen, gate = self.call(None)
        self.assertEqual(seen["tenant"], "member")
        gate.assert_not_called()

    def test_acting_as_self_is_a_noop(self):
        _, seen, gate = self.call("member")
        self.assertEqual(seen["tenant"], "member")
        gate.assert_not_called()

    def test_denied_is_403_and_never_routes(self):
        with self.assertRaises(HTTPException) as ctx:
            self.call("leader", can_act=False)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_missing_leader_account_is_403(self):
        with self.assertRaises(HTTPException) as ctx:
            self.call("leader", leader_exists=False)
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
