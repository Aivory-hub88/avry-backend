"""Ivo (data_analyst): roster entry, tier, and tool scope stay consistent."""
import unittest

from app.routes.agent_roster import AGENT_ROSTER
from app.routes.agent_tool_scope import TOGGLEABLE_TOOLKITS
from app.services.telegram_service import AGENT_MIN_TIER, AGENT_TYPES


class IvoWiring(unittest.TestCase):
    def test_in_roster_with_name_and_title(self):
        entry = next(a for a in AGENT_ROSTER if a["agent_type"] == "data_analyst")
        self.assertEqual(entry["name"], "Ivo")
        self.assertEqual(entry["title"], "Data & Reporting Analyst")

    def test_roster_types_and_names_are_unique(self):
        types = [a["agent_type"] for a in AGENT_ROSTER]
        names = [a["name"] for a in AGENT_ROSTER]
        self.assertEqual(len(types), len(set(types)))
        self.assertEqual(len(names), len(set(names)))

    def test_derived_agent_types_include_ivo(self):
        self.assertIn("data_analyst", AGENT_TYPES)

    def test_available_on_every_paid_tier(self):
        # Decided 2026-11-09: no tier gate for the three new agents.
        self.assertNotIn("data_analyst", AGENT_MIN_TIER)

    def test_toolkits_are_read_or_google_only(self):
        scope = set(TOGGLEABLE_TOOLKITS["data_analyst"])
        self.assertFalse(scope & {"gmail", "outlook", "smartlead", "linkedin", "asana", "trello", "linear"})
        self.assertTrue({"googlesheets", "googledrive", "erpnext", "hubspot"} <= scope)

    def test_toolkits_subset_of_autonomous_union(self):
        self.assertTrue(set(TOGGLEABLE_TOOLKITS["data_analyst"]) <= set(TOGGLEABLE_TOOLKITS["autonomous"]))

    def test_every_roster_worker_has_a_tool_scope_entry_except_coordinator(self):
        missing = [a["agent_type"] for a in AGENT_ROSTER
                   if a["agent_type"] not in TOGGLEABLE_TOOLKITS and a["agent_type"] != "chief_of_staff"]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
