#!/usr/bin/env python3
"""
Unit tests for ADR-020 P1 active-agents aggregation.

Run with `python3 -m unittest tests.test_active_agents` from the
avry-backend root. All sources are mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.routes import active_agents as aa  # noqa: E402


class MergeSourcesTest(unittest.TestCase):
    def test_union_with_sources_in_roster_order(self):
        out = aa.merge_sources(
            {
                "telegram": ["leads_qualifier", "customer_service"],
                "slack": ["customer_service"],
                "api_key": [],
            }
        )
        self.assertEqual(
            out,
            [
                {"agent_type": "customer_service", "sources": ["slack", "telegram"]},
                {"agent_type": "leads_qualifier", "sources": ["telegram"]},
            ],
        )

    def test_unknown_and_empty_values_dropped(self):
        out = aa.merge_sources({"telegram": ["ghost", None, "autonomous"]})
        self.assertEqual(out, [{"agent_type": "autonomous", "sources": ["telegram"]}])

    def test_nothing_active_is_empty(self):
        self.assertEqual(aa.merge_sources({}), [])


class CollectFailOpenTest(unittest.TestCase):
    def test_failing_source_is_skipped(self):
        def boom(_uid):
            raise RuntimeError("db down")

        with patch.dict(
            aa._SOURCES,
            {
                "telegram": lambda _u: ["finance_invoice_ops"],
                "slack": boom,
                "api_key": lambda _u: ["finance_invoice_ops"],
            },
        ):
            out = aa.collect_active_agents("u1")
        self.assertEqual(
            out,
            [{"agent_type": "finance_invoice_ops", "sources": ["api_key", "telegram"]}],
        )

    def test_all_sources_failing_returns_empty(self):
        def boom(_uid):
            raise RuntimeError("down")

        with patch.dict(aa._SOURCES, {"telegram": boom, "slack": boom, "api_key": boom}):
            self.assertEqual(aa.collect_active_agents("u1"), [])


if __name__ == "__main__":
    unittest.main()
