"""Juno (operations_inventory): roster entry, tier, tool scope and Odoo methods stay consistent."""
import unittest

from app.routes.agent_roster import AGENT_ROSTER
from app.routes.agent_tool_scope import TOGGLEABLE_TOOLKITS
from app.routes.tenant_mcp_servers import _odoo_methods_for
from app.services.telegram_service import AGENT_MIN_TIER, AGENT_TYPES


class JunoWiring(unittest.TestCase):
    def test_in_roster_with_name_and_title(self):
        entry = next(a for a in AGENT_ROSTER if a["agent_type"] == "operations_inventory")
        self.assertEqual(entry["name"], "Juno")
        self.assertEqual(entry["title"], "Operations & Inventory Agent")

    def test_roster_types_and_names_are_unique(self):
        types = [a["agent_type"] for a in AGENT_ROSTER]
        names = [a["name"] for a in AGENT_ROSTER]
        self.assertEqual(len(types), len(set(types)))
        self.assertEqual(len(names), len(set(names)))

    def test_derived_agent_types_include_juno(self):
        self.assertIn("operations_inventory", AGENT_TYPES)

    def test_available_on_every_paid_tier(self):
        self.assertNotIn("operations_inventory", AGENT_MIN_TIER)

    def test_toolkits_subset_of_autonomous_union(self):
        self.assertTrue(set(TOGGLEABLE_TOOLKITS["operations_inventory"]) <= set(TOGGLEABLE_TOOLKITS["autonomous"]))

    def test_no_social_or_crm_toolkits(self):
        scope = set(TOGGLEABLE_TOOLKITS["operations_inventory"])
        self.assertFalse(scope & {"linkedin", "smartlead", "hubspot", "slack", "asana"})

    def test_odoo_methods_are_exactly_the_three_gated_ones(self):
        self.assertEqual(
            sorted(_odoo_methods_for("operations_inventory")),
            ["purchase.order.button_cancel", "purchase.order.button_confirm", "stock.picking.button_validate"],
        )

    def test_other_agents_do_not_get_ops_methods(self):
        for t in ("data_analyst", "finance_invoice_ops", "leads_qualifier"):
            self.assertFalse([m for m in _odoo_methods_for(t) if m.startswith(("purchase.", "stock."))], t)

    def test_generalist_keeps_the_union(self):
        self.assertIn("purchase.order.button_confirm", _odoo_methods_for("autonomous"))
        self.assertIn("account.move.action_post", _odoo_methods_for("autonomous"))


if __name__ == "__main__":
    unittest.main()
