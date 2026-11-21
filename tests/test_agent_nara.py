"""Nara (deep_researcher): roster entry, Business-tier gate and read-only scope stay consistent."""
import unittest

from app.routes.agent_roster import AGENT_ROSTER
from app.routes.agent_tool_scope import TOGGLEABLE_TOOLKITS
from app.routes.tenant_mcp_servers import _odoo_methods_for
from app.services.telegram_service import AGENT_MIN_TIER, AGENT_TYPES, agent_tier_error


class NaraWiring(unittest.TestCase):
    def test_in_roster_with_name_and_title(self):
        entry = next(a for a in AGENT_ROSTER if a["agent_type"] == "deep_researcher")
        self.assertEqual(entry["name"], "Nara")
        self.assertEqual(entry["title"], "Research & Evidence Agent")

    def test_roster_types_and_names_are_unique(self):
        types = [a["agent_type"] for a in AGENT_ROSTER]
        names = [a["name"] for a in AGENT_ROSTER]
        self.assertEqual(len(types), len(set(types)))
        self.assertEqual(len(names), len(set(names)))

    def test_derived_agent_types_include_nara(self):
        self.assertIn("deep_researcher", AGENT_TYPES)

    def test_minimum_tier_is_business(self):
        self.assertEqual(AGENT_MIN_TIER["deep_researcher"], "business")

    def test_operational_is_refused_business_and_enterprise_pass(self):
        self.assertIn("Business", agent_tier_error({"tier": "operational"}, "deep_researcher") or "")
        self.assertIsNone(agent_tier_error({"tier": "business"}, "deep_researcher"))
        self.assertIsNone(agent_tier_error({"tier": "enterprise"}, "deep_researcher"))

    def test_superadmin_bypasses_the_gate(self):
        self.assertIsNone(agent_tier_error({"tier": "operational", "is_superadmin": True}, "deep_researcher"))

    def test_enterprise_message_for_ofira_is_unchanged_in_meaning(self):
        msg = agent_tier_error({"tier": "operational"}, "office_assistant") or ""
        self.assertIn("Enterprise", msg)

    def test_read_only_no_external_toolkits(self):
        # Present (the roster-vs-scope invariant) but empty: nothing to toggle.
        self.assertEqual(TOGGLEABLE_TOOLKITS["deep_researcher"], [])

    def test_no_odoo_execute_methods(self):
        self.assertEqual(_odoo_methods_for("deep_researcher"), [])


class GenoRescope(unittest.TestCase):
    """ADR-025 D1: Geno keeps agent_type and name, the title becomes task and follow-up keeper."""

    def test_geno_title_and_identity(self):
        entry = next(a for a in AGENT_ROSTER if a["agent_type"] == "autonomous")
        self.assertEqual(entry["name"], "Geno")
        self.assertEqual(entry["title"], "Task & Follow-up Agent")
        self.assertEqual(AGENT_TYPES["autonomous"], "Task & Follow-up Agent")


if __name__ == "__main__":
    unittest.main()
