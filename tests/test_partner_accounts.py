#!/usr/bin/env python3
"""
Unit tests for distributor-partner accounts: who may create one, what it is
granted, and that the auth response reports it as Enterprise.

Postgres is replaced by an in-memory fake pool. Run via
`python3 -m unittest tests.test_partner_accounts` from the avry-backend root
with the project's requirements installed.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.routes.admin_users as admin_users  # noqa: E402
from app.services.auth_service import AuthService  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeConn:
    def __init__(self, log):
        self.log = log

    async def execute(self, sql, *args):
        self.log.append((" ".join(sql.split()), args))

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Tx()


class _Expiry:
    def isoformat(self):
        return "2027-03-17T00:00:00+00:00"


class _FakePool:
    def __init__(self, target=None):
        self.log = []
        self.target = target  # row returned for a lookup by id

    async def fetchrow(self, sql, *args):
        sql = " ".join(sql.split())
        if sql.startswith("SELECT id FROM users WHERE email"):
            return None  # no existing user with this email
        if sql.startswith("SELECT id, account_type FROM users"):
            return self.target
        self.log.append((sql, args))
        return {"expires_at": _Expiry()}

    def acquire(self):
        conn = _FakeConn(self.log)

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _token(account_type):
    return "Bearer " + jwt.encode(
        {"user_id": "user_admin", "account_type": account_type},
        admin_users.JWT_SECRET,
        algorithm=admin_users.JWT_ALGORITHM,
    )


def _run(pool, coro_fn):
    async def get_pool():
        return pool

    with patch.object(admin_users.pg, "get_pool", get_pool):
        return asyncio.run(coro_fn())


def _create(body, account_type="superadmin"):
    pool = _FakePool()
    result = _run(pool, lambda: admin_users.create_admin_account(body, _token(account_type)))
    return result, pool.log


def _extend(body, target_type="partner", account_type="superadmin"):
    pool = _FakePool(target={"id": "user_p", "account_type": target_type})
    result = _run(
        pool,
        lambda: admin_users.extend_partner_access("user_p", body, _token(account_type)),
    )
    return result, pool.log


PARTNER_BODY = {
    "email": "ops@distributor.example",
    "password": "Str0ng!pass",
    "accountType": "partner",
    "companyName": "PT Distributor Contoh",
    "freeMonths": 3,
}


class CreatePartner(unittest.TestCase):
    def test_superadmin_creates_partner_with_enterprise_entitlement(self):
        result, log = _create(dict(PARTNER_BODY))
        self.assertEqual(result["admin"]["accountType"], "partner")
        self.assertEqual(result["admin"]["companyName"], "PT Distributor Contoh")

        user_insert, tier_insert = log
        self.assertIn("INSERT INTO users", user_insert[0])
        self.assertEqual(user_insert[1][3], "partner")
        self.assertEqual(user_insert[1][5], "PT Distributor Contoh")

        self.assertIn("INSERT INTO user_tiers", tier_insert[0])
        self.assertIn("make_interval(months => $4)", tier_insert[0])
        self.assertEqual(tier_insert[1][1], "enterprise")
        self.assertEqual(json.loads(tier_insert[1][2]), ["snapshot", "blueprint"])
        self.assertEqual(tier_insert[1][3], 3)

    def test_free_months_must_be_an_offered_option(self):
        for months in (None, 0, 2, 12, "3", True):
            body = dict(PARTNER_BODY, freeMonths=months)
            with self.assertRaises(HTTPException) as ctx:
                _create(body)
            self.assertEqual(ctx.exception.status_code, 400, months)

    def test_admin_cannot_create_partner(self):
        with self.assertRaises(HTTPException) as ctx:
            _create(dict(PARTNER_BODY), account_type="admin")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_partner_requires_company_name(self):
        body = dict(PARTNER_BODY, companyName="   ")
        with self.assertRaises(HTTPException) as ctx:
            _create(body)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_demo_account_gets_no_tier_row(self):
        body = {"email": "demo@example.com", "password": "Str0ng!pass", "accountType": "demo"}
        _, log = _create(body, account_type="admin")
        self.assertEqual(len(log), 1)
        self.assertIn("INSERT INTO users", log[0][0])


class ExtendPartner(unittest.TestCase):
    def test_extends_from_later_of_expiry_and_now(self):
        result, log = _extend({"freeMonths": 5})
        self.assertEqual(result["partnerExpiresAt"], "2027-03-17T00:00:00+00:00")
        (sql, args), = log
        self.assertIn("GREATEST(COALESCE(user_tiers.expires_at, now()), now())", sql)
        self.assertEqual(args[1], "enterprise")
        self.assertEqual(args[3], 5)

    def test_only_superadmin(self):
        with self.assertRaises(HTTPException) as ctx:
            _extend({"freeMonths": 1}, account_type="admin")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_only_partner_accounts(self):
        with self.assertRaises(HTTPException) as ctx:
            _extend({"freeMonths": 1}, target_type="free")
        self.assertEqual(ctx.exception.status_code, 400)


class PartnerTier(unittest.TestCase):
    def _tier(self, live):
        svc = AuthService.__new__(AuthService)
        svc.db = type("Db", (), {"load_all_json": staticmethod(lambda name: [])})()
        with patch("app.services.entitlement_state.get_entitlements", return_value=live):
            return svc._compute_tier("user_x", "partner")

    def test_partner_in_free_period_is_enterprise(self):
        info = self._tier({"tier": "enterprise", "features": [], "found": True})
        self.assertEqual(info["tier"], "enterprise")
        self.assertTrue(info["is_subscribed"])
        self.assertTrue(info["has_snapshot"] and info["has_blueprint"])

    def test_lapsed_partner_is_free(self):
        info = self._tier({"tier": None, "features": [], "expired": True, "found": True})
        self.assertEqual(info["tier"], "free")

    def test_unreadable_entitlements_keep_access(self):
        self.assertEqual(self._tier(None)["tier"], "enterprise")


if __name__ == "__main__":
    unittest.main()
