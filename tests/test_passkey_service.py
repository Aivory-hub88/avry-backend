"""Passkey service tests with a software authenticator (ES256, 'none' attestation)."""
import asyncio
import hashlib
import json
import os
import struct
import unittest
from unittest import mock

os.environ.setdefault("JWT_SECRET", "test-secret-for-passkey-tests-0123456789abcdef")

import cbor2
import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

from app.services import passkey_service as ps
from app.services.token_kinds import is_access_payload, is_refresh_payload

ORIGIN = "https://aivory.uk"
RP = "aivory.uk"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeAuthenticator:
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.count = 0

    def _client_data(self, typ, challenge_b64, origin):
        return json.dumps({"type": typ, "challenge": challenge_b64, "origin": origin}).encode()

    def register(self, options, origin=ORIGIN):
        nums = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth = (hashlib.sha256(RP.encode()).digest() + bytes([0x45]) + struct.pack(">I", 0)
                + bytes(16) + struct.pack(">H", len(self.cred_id)) + self.cred_id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        cid = bytes_to_base64url(self.cred_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(self._client_data("webauthn.create", options["challenge"], origin)),
                "attestationObject": bytes_to_base64url(att),
                "transports": ["internal"],
            },
        }

    def assert_(self, options, origin=ORIGIN):
        self.count += 1
        auth = hashlib.sha256(RP.encode()).digest() + bytes([0x05]) + struct.pack(">I", self.count)
        cd = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        cid = bytes_to_base64url(self.cred_id)
        return {
            "id": cid, "rawId": cid, "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(cd),
                "authenticatorData": bytes_to_base64url(auth),
                "signature": bytes_to_base64url(sig),
            },
        }


class FakePg:
    def __init__(self, user):
        self.user = user
        self.rows = {}

    async def list_passkeys(self, user_id):
        return [r for r in self.rows.values() if r["user_id"] == user_id]

    async def insert_passkey(self, user_id, credential_id, public_key, sign_count, transports, name):
        self.rows[credential_id] = dict(user_id=user_id, credential_id=credential_id, public_key=public_key,
                                        sign_count=sign_count, transports=transports, name=name)

    async def get_passkey(self, credential_id):
        return self.rows.get(credential_id)

    async def touch_passkey(self, credential_id, sign_count):
        self.rows[credential_id]["sign_count"] = sign_count

    async def get_user_by_id(self, user_id):
        return self.user if self.user and self.user["id"] == user_id else None


USER = {"id": "user_1", "email": "a@example.com", "full_name": "A", "is_active": True}


class PasskeyServiceTest(unittest.TestCase):
    def setUp(self):
        self.pg = FakePg(dict(USER))
        p = mock.patch.object(ps, "pg", self.pg)
        p.start()
        self.addCleanup(p.stop)
        self.auth = FakeAuthenticator()

    def _register(self):
        opts = _run(ps.registration_options(USER))
        cred = self.auth.register(opts["options"])
        _run(ps.verify_registration(USER, opts["challenge_token"], cred, "Laptop"))

    def test_register_then_login_returns_the_owner(self):
        self._register()
        self.assertEqual(len(self.pg.rows), 1)
        opts = ps.login_options()
        user = _run(ps.verify_login(opts["challenge_token"], self.auth.assert_(opts["options"])))
        self.assertEqual(user["user_id"], "user_1")
        self.assertEqual(self.pg.rows[bytes_to_base64url(self.auth.cred_id)]["sign_count"], 1)

    def test_wrong_origin_is_rejected(self):
        opts = _run(ps.registration_options(USER))
        cred = self.auth.register(opts["options"], origin="https://evil.example")
        with self.assertRaises(ps.PasskeyError):
            _run(ps.verify_registration(USER, opts["challenge_token"], cred, "x"))
        self.assertEqual(self.pg.rows, {})

    def test_replayed_assertion_is_rejected(self):
        self._register()
        opts = ps.login_options()
        assertion = self.auth.assert_(opts["options"])
        _run(ps.verify_login(opts["challenge_token"], assertion))
        opts2 = ps.login_options()
        with self.assertRaises(ps.PasskeyError):
            # same counter value as last time: cloned authenticator signal
            self.auth.count -= 1
            _run(ps.verify_login(opts2["challenge_token"], self.auth.assert_(opts2["options"])))

    def test_challenge_cannot_cross_purposes_or_users(self):
        opts = ps.login_options()
        with self.assertRaises(ps.PasskeyError):
            ps._open_challenge(opts["challenge_token"], "register", "user_1")
        reg = _run(ps.registration_options(USER))
        with self.assertRaises(ps.PasskeyError):
            ps._open_challenge(reg["challenge_token"], "register", "someone_else")

    def test_unknown_credential_is_rejected(self):
        opts = ps.login_options()
        with self.assertRaises(ps.PasskeyError):
            _run(ps.verify_login(opts["challenge_token"], self.auth.assert_(opts["options"])))

    def test_challenge_token_is_never_a_session_token(self):
        token = ps.login_options()["challenge_token"]
        payload = jwt.decode(token, ps.JWT_SECRET, algorithms=["HS256"])
        self.assertFalse(is_access_payload(payload))
        self.assertFalse(is_refresh_payload(payload))

    def test_inactive_user_cannot_log_in(self):
        self._register()
        self.pg.user["is_active"] = False
        opts = ps.login_options()
        with self.assertRaises(ps.PasskeyError):
            _run(ps.verify_login(opts["challenge_token"], self.auth.assert_(opts["options"])))


if __name__ == "__main__":
    unittest.main()
