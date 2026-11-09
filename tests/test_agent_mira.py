"""Mira (marketing_content): roster entry, tier and tool scope stay consistent."""
import unittest

from app.routes.agent_roster import AGENT_ROSTER
from app.routes.agent_tool_scope import TOGGLEABLE_TOOLKITS
from app.routes.tenant_mcp_servers import _odoo_methods_for
from app.services.telegram_service import AGENT_MIN_TIER, AGENT_TYPES


class MiraWiring(unittest.TestCase):
    def test_in_roster_with_name_and_title(self):
        entry = next(a for a in AGENT_ROSTER if a["agent_type"] == "marketing_content")
        self.assertEqual(entry["name"], "Mira")
        self.assertEqual(entry["title"], "Marketing & Content Agent")

    def test_roster_types_and_names_are_unique(self):
        types = [a["agent_type"] for a in AGENT_ROSTER]
        names = [a["name"] for a in AGENT_ROSTER]
        self.assertEqual(len(types), len(set(types)))
        self.assertEqual(len(names), len(set(names)))

    def test_derived_agent_types_include_mira(self):
        self.assertIn("marketing_content", AGENT_TYPES)

    def test_available_on_every_paid_tier(self):
        self.assertNotIn("marketing_content", AGENT_MIN_TIER)

    def test_toolkits_subset_of_autonomous_union(self):
        self.assertTrue(set(TOGGLEABLE_TOOLKITS["marketing_content"]) <= set(TOGGLEABLE_TOOLKITS["autonomous"]))

    def test_no_social_posting_or_erp_toolkits_in_v1(self):
        scope = set(TOGGLEABLE_TOOLKITS["marketing_content"])
        self.assertFalse(scope & {"linkedin", "smartlead", "slack", "erpnext", "hubspot"})

    def test_has_the_drafting_toolkits(self):
        self.assertTrue({"googledocs", "googlesheets", "googledrive", "gmail"} <= set(TOGGLEABLE_TOOLKITS["marketing_content"]))

    def test_mira_gets_no_odoo_execute_methods(self):
        self.assertEqual(_odoo_methods_for("marketing_content"), [])


if __name__ == "__main__":
    unittest.main()
