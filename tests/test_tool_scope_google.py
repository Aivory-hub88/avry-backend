#!/usr/bin/env python3
"""Google Workspace toolkits per agent type (must match Cerveau's
[agents.<type>] mcp_bundles: storage-googledrive / sheets-googlesheets /
docs-googledocs). Run: python3 -m unittest tests.test_tool_scope_google"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routes.agent_tool_scope import TOGGLEABLE_TOOLKITS as T  # noqa: E402

GOOGLE = {"googledrive", "googlesheets", "googledocs"}


class GoogleWorkspaceScopeTest(unittest.TestCase):
    def google(self, agent):
        return set(T.get(agent, [])) & GOOGLE

    def test_assignment(self):
        self.assertEqual(self.google("autonomous"), GOOGLE)
        self.assertEqual(self.google("office_assistant"), GOOGLE)
        self.assertEqual(self.google("finance_invoice_ops"), GOOGLE)
        self.assertEqual(self.google("leads_qualifier"), GOOGLE)
        self.assertEqual(self.google("data_analyst"), GOOGLE)
        self.assertEqual(self.google("chief_of_staff"), GOOGLE)

    def test_support_gets_none(self):
        self.assertEqual(self.google("customer_service"), set())

    def test_no_duplicates(self):
        for agent, slugs in T.items():
            self.assertEqual(len(slugs), len(set(slugs)), agent)


if __name__ == "__main__":
    unittest.main()
