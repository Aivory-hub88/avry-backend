"""
Unit tests for app/services/tiers.py — the canonical tier vocabulary.

Run with `python3 -m unittest tests.test_tiers` from the avry-backend root.

These lock in two things that were previously wrong and are easy to get wrong
again:

  1. An account with no live plan must NOT clear a paid gate. The old ladder
     gave unknown tiers rung 0 — the same rung as the base paid tier — so
     `meets(tier, "operational")` was true for literally every caller, and the
     loader compounded it by resolving a missing or lapsed entitlement to the
     base PAID tier.
  2. Closing that gate must NOT change anyone's Intelligence Credit
     allowance. Gating reads `account_tier`; the credit path reads
     `normalise`, which keeps the base-tier fallback.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import tiers  # noqa: E402


class Normalisation(unittest.TestCase):
    def test_legacy_ids_map_onto_the_rebrand_names(self):
        self.assertEqual(tiers.normalise("foundation"), "operational")
        self.assertEqual(tiers.normalise("pro"), "business")
        self.assertEqual(tiers.normalise("acceleration"), "business")
        self.assertEqual(tiers.normalise("intelligence"), "enterprise")

    def test_casing_and_padding_tolerated(self):
        self.assertEqual(tiers.normalise("  Business "), "business")

    def test_canonical_ids_are_unchanged(self):
        for tier in tiers.CANONICAL_TIERS:
            self.assertEqual(tiers.normalise(tier), tier)


class AccountTier(unittest.TestCase):
    def test_no_plan_resolves_to_free(self):
        # None is what the loader passes for a missing entitlement row and for
        # a lapsed subscription.
        for value in (None, "", "free", "not-a-tier"):
            self.assertEqual(tiers.account_tier(value), tiers.FREE_TIER)

    def test_paid_tiers_resolve_to_themselves(self):
        self.assertEqual(tiers.account_tier("foundation"), "operational")
        self.assertEqual(tiers.account_tier("pro"), "business")
        self.assertEqual(tiers.account_tier("enterprise"), "enterprise")


class Gating(unittest.TestCase):
    def test_free_is_rejected_by_every_paid_gate(self):
        for minimum in tiers.CANONICAL_TIERS:
            self.assertFalse(
                tiers.meets(tiers.FREE_TIER, minimum),
                f"free must not clear the {minimum} gate",
            )

    def test_lapsed_or_absent_entitlement_is_rejected(self):
        self.assertFalse(tiers.meets(tiers.account_tier(None), "operational"))

    def test_base_paid_tier_clears_the_lowest_gate(self):
        self.assertTrue(tiers.meets("operational", "operational"))
        self.assertTrue(tiers.meets("foundation", "operational"))

    def test_ladder_is_ordered(self):
        self.assertFalse(tiers.meets("operational", "business"))
        self.assertTrue(tiers.meets("business", "operational"))
        self.assertTrue(tiers.meets("enterprise", "business"))
        self.assertFalse(tiers.meets("business", "enterprise"))


class CreditAllowanceUnaffected(unittest.TestCase):
    """The access fix must leave every allowance exactly where it was."""

    def _allowance(self, tier):
        return tiers.TIER_ALLOWANCES.get(
            tiers.normalise(tier), tiers.TIER_ALLOWANCES[tiers.BASE_TIER]
        )

    def test_allowances_per_tier(self):
        self.assertEqual(self._allowance("operational"), 1500)
        self.assertEqual(self._allowance("foundation"), 1500)
        self.assertEqual(self._allowance("business"), 6500)
        self.assertEqual(self._allowance("pro"), 6500)
        self.assertEqual(self._allowance("enterprise"), 90000)

    def test_accounts_with_no_plan_keep_the_base_allowance(self):
        for value in (None, "", "free"):
            self.assertEqual(self._allowance(value), 1500)


class Display(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(tiers.display_name("foundation"), "Operational")
        self.assertEqual(tiers.display_name("pro"), "Business")
        self.assertEqual(tiers.display_name(None), "Free")

class Seats(unittest.TestCase):
    def test_published_plans(self):
        self.assertEqual(tiers.seat_limit("operational"), 1)
        self.assertEqual(tiers.seat_limit("business"), 5)
        self.assertEqual(tiers.seat_limit("enterprise"), 15)

    def test_legacy_ids(self):
        self.assertEqual(tiers.seat_limit("pro"), 5)
        self.assertEqual(tiers.seat_limit("intelligence"), 15)

    def test_no_live_plan_is_one_seat(self):
        for t in (None, "", "free", "blueprint", "nonsense"):
            self.assertEqual(tiers.seat_limit(t), 1, t)

    def test_contract_override(self):
        self.assertEqual(tiers.seat_limit("enterprise", 40), 40)
        self.assertEqual(tiers.seat_limit("enterprise", 0), 15)
        self.assertEqual(tiers.seat_limit("enterprise", True), 15)

    def test_every_plan_has_seats(self):
        self.assertEqual(set(tiers.SEAT_LIMITS), set(tiers.CANONICAL_TIERS))



if __name__ == "__main__":
    unittest.main()
