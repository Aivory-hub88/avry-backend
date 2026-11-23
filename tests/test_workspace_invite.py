#!/usr/bin/env python3
"""
app/services/workspace_invite.py — the "you were added to a workspace" email.

Run from the avry-backend root:
    python3 -m unittest tests.test_workspace_invite
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import workspace_invite as wi  # noqa: E402

FACTS = {
    "role": "editor", "added_by": "u-owner", "workspace_name": "Acme <HQ>",
    "email": "new@x.com", "full_name": "Newbie", "inviter_name": "Boss",
}


def run(coro):
    return asyncio.run(coro)


class Notify(unittest.TestCase):
    def test_sends_to_the_member_from_the_db(self):
        with patch.object(wi, "fetch_invite_facts", return_value=FACTS), \
             patch.object(wi.email_service, "send_email", new=AsyncMock(return_value=True)) as send:
            self.assertEqual(run(wi.notify_member("w1", "u2", "u-owner")), "sent")
        to, subject, html, _ = send.call_args.args
        self.assertEqual(send.call_args.kwargs["from_address"], "noreply@aivory.uk")
        self.assertEqual(to, "new@x.com")
        self.assertIn("Boss added you to Acme <HQ>", subject)
        self.assertIn("Acme &lt;HQ&gt;", html)  # escaped in html

    def test_only_the_person_who_added_them_can_trigger_it(self):
        with patch.object(wi, "fetch_invite_facts", return_value=FACTS), \
             patch.object(wi.email_service, "send_email", new=AsyncMock()) as send:
            self.assertEqual(run(wi.notify_member("w1", "u2", "someone-else")), "forbidden")
        send.assert_not_called()

    def test_unknown_member(self):
        with patch.object(wi, "fetch_invite_facts", return_value=None):
            self.assertEqual(run(wi.notify_member("w1", "u2", "u-owner")), "not_member")

    def test_mail_failure_is_reported_not_raised(self):
        with patch.object(wi, "fetch_invite_facts", return_value=FACTS), \
             patch.object(wi.email_service, "send_email", new=AsyncMock(return_value=False)):
            self.assertEqual(run(wi.notify_member("w1", "u2", "u-owner")), "failed")


if __name__ == "__main__":
    unittest.main()
