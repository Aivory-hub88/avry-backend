#!/usr/bin/env python3
"""LinkedIn is toggleable for office_assistant, autonomous and leads_qualifier only (must match
Cerveau's bundles). Run: python3 -m unittest tests.test_tool_scope_linkedin"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routes.agent_tool_scope import TOGGLEABLE_TOOLKITS as T  # noqa: E402


class LinkedInScopeTest(unittest.TestCase):
    def test_assignment(self):
        self.assertIn("linkedin", T["office_assistant"])
        self.assertIn("linkedin", T["autonomous"])
        self.assertIn("linkedin", T["leads_qualifier"])
        for agent in ("customer_service", "finance_invoice_ops"):
            self.assertNotIn("linkedin", T[agent])

    def test_no_duplicates(self):
        for agent, slugs in T.items():
            self.assertEqual(len(slugs), len(set(slugs)), agent)


if __name__ == "__main__":
    unittest.main()
