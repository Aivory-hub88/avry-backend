#!/usr/bin/env python3
"""
Annual subscription products grant the same tier as the monthly ones, for a
year instead of a month. Postgres is replaced by a recording fake cursor. Run
via `python3 -m unittest discover -s tests -p test_annual_billing.py`.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.routes.entitlements as ent  # noqa: E402


class _Cursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1
        self._next = None

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.calls.append((sql, params))
        if sql.startswith("SELECT id FROM identity.users"):
            self._next = ("user_x",)
        elif "RETURNING tier, expires_at" in sql:
            import datetime

            self._next = (params[1], datetime.datetime(2027, 10, 19))
        else:
            self._next = None

    def fetchone(self):
        return self._next

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def __init__(self):
        self.cur = _Cursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _grant(product):
    conn = _Conn()
    with patch.object(ent, "_connect", return_value=conn), patch.object(ent, "_ensure_schema"):
        ent.grant_entitlement(ent.GrantRequest(user_id="user_x", product=product, order_id="o1"))
    (sql, params), = [c for c in conn.cur.calls if "INSERT INTO identity.user_tiers" in c[0]]
    return params


class AnnualBilling(unittest.TestCase):
    def test_annual_products_grant_a_year_of_the_same_tier(self):
        self.assertEqual(_grant("operational_annual")[1:], ("operational", 365, 365))
        self.assertEqual(_grant("business_annual")[1:], ("business", 365, 365))

    def test_monthly_products_still_grant_31_days(self):
        self.assertEqual(_grant("operational")[1:], ("operational", 31, 31))
        self.assertEqual(_grant("business")[1:], ("business", 31, 31))


if __name__ == "__main__":
    unittest.main()
