"""Passkey (WebAuthn) sign-in.

A passkey is an extra way to prove who you are; it never replaces the
password. Login verifies the signed challenge and then hands the user to
AuthService.complete_login, so the Policy A payment gate and session
bookkeeping are identical to a password login.

Challenges are stateless: a short-lived JWT of type "passkey_challenge"
carries the challenge between the options call and the verify call. It is
never accepted as an access or refresh token (see token_kinds).
"""
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.database import pg_service as pg
from app.services.jwt_secret import JWT_SECRET

CHALLENGE_TYPE = "passkey_challenge"
CHALLENGE_TTL_SECONDS = 300
MAX_PASSKEYS_PER_USER = 10


def rp_id() -> str:
    # Registrable domain, so a passkey also works on subdomains (mail.aivory.uk).
    return os.getenv("PASSKEY_RP_ID", "aivory.uk").strip()


def rp_name() -> str:
    return os.getenv("PASSKEY_RP_NAME", "Aivory").strip()


def origins() -> list:
    raw = os.getenv("PASSKEY_ORIGINS", "https://aivory.uk,https://www.aivory.uk")
    return [o.strip() for o in raw.split(",") if o.strip()]


class PasskeyError(ValueError):
    """Anything the caller should see as a 400/401 with a plain message."""


def _sign_challenge(challenge: bytes, purpose: str, user_id: Optional[str] = None) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "type": CHALLENGE_TYPE,
            "purpose": purpose,
            "challenge": bytes_to_base64url(challenge),
            "user_id": user_id,
            "iat": now,
            "exp": now + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def _open_challenge(token: str, purpose: str, user_id: Optional[str] = None) -> bytes:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise PasskeyError("Passkey request expired, try again")
    if payload.get("type") != CHALLENGE_TYPE or payload.get("purpose") != purpose:
        raise PasskeyError("Invalid passkey request")
    if user_id is not None and payload.get("user_id") != user_id:
        raise PasskeyError("Invalid passkey request")
    return base64url_to_bytes(payload["challenge"])


async def registration_options(user: dict) -> dict:
    user_id = user.get("user_id") or user.get("id")
    existing = await pg.list_passkeys(user_id)
    if len(existing) >= MAX_PASSKEYS_PER_USER:
        raise PasskeyError("Passkey limit reached, remove one first")
    options = generate_registration_options(
        rp_id=rp_id(),
        rp_name=rp_name(),
        user_id=user_id.encode(),
        user_name=user["email"],
        user_display_name=user.get("full_name") or user["email"],
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(p["credential_id"]))
            for p in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    return {
        "options": json.loads(options_to_json(options)),
        "challenge_token": _sign_challenge(options.challenge, "register", user_id),
    }


async def verify_registration(user: dict, challenge_token: str, credential: dict, name: str) -> None:
    user_id = user.get("user_id") or user.get("id")
    challenge = _open_challenge(challenge_token, "register", user_id)
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=origins(),
            require_user_verification=False,
        )
    except Exception:
        raise PasskeyError("Passkey could not be verified")
    transports = (credential.get("response") or {}).get("transports") or []
    await pg.insert_passkey(
        user_id=user_id,
        credential_id=bytes_to_base64url(verified.credential_id),
        public_key=bytes_to_base64url(verified.credential_public_key),
        sign_count=verified.sign_count,
        transports=[t for t in transports if isinstance(t, str)],
        name=(name or "Passkey").strip()[:60] or "Passkey",
    )


def login_options() -> dict:
    # No allow_credentials: the authenticator offers its own resident keys, so
    # the user does not have to type an email first and we don't reveal which
    # emails have passkeys.
    options = generate_authentication_options(
        rp_id=rp_id(),
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return {
        "options": json.loads(options_to_json(options)),
        "challenge_token": _sign_challenge(options.challenge, "login"),
    }


async def verify_login(challenge_token: str, credential: dict) -> dict:
    """Return the user row that owns the asserted passkey."""
    challenge = _open_challenge(challenge_token, "login")
    stored = await pg.get_passkey(credential.get("id") or "")
    if not stored:
        raise PasskeyError("Passkey not recognised")
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id(),
            expected_origin=origins(),
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=False,
        )
    except Exception:
        raise PasskeyError("Passkey could not be verified")
    await pg.touch_passkey(stored["credential_id"], verified.new_sign_count)
    user = await pg.get_user_by_id(stored["user_id"])
    if not user or user.get("is_active") is False:
        raise PasskeyError("Passkey not recognised")
    user = dict(user)
    user.setdefault("user_id", user.get("id"))
    return user
