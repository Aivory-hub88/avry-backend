"""
Impersonation Token Service — handles creation and validation of impersonation-specific JWT tokens.

The impersonation token carries dual identity context: the admin's real identity for
audit attribution, and the target user's identity for data resolution.
"""

import os
import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass


# JWT configuration — reuses the same secret as the main auth service
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM = "HS256"
IMPERSONATION_TOKEN_TTL_MINUTES = 60  # 60-minute TTL


@dataclass
class ImpersonationTokenPayload:
    """Validated impersonation token payload."""
    type: str
    admin_user_id: str
    target_user_id: str
    access_mode: str
    session_id: str
    iat: int
    exp: int


class ImpersonationTokenService:
    """Handles creation and validation of impersonation-specific JWT tokens."""

    REQUIRED_CLAIMS = {"type", "admin_user_id", "target_user_id", "access_mode", "session_id", "iat", "exp"}

    def create_token(
        self,
        admin_user_id: str,
        target_user_id: str,
        access_mode: str,
        session_id: str,
    ) -> str:
        """
        Generate a JWT impersonation token with all required claims.

        Args:
            admin_user_id: The superadmin's user ID.
            target_user_id: The user being impersonated.
            access_mode: "read_only" or "full_access".
            session_id: Reference to the impersonation_sessions record.

        Returns:
            Encoded JWT token string.
        """
        now = datetime.now(timezone.utc)
        payload = {
            "type": "impersonation",
            "admin_user_id": admin_user_id,
            "target_user_id": target_user_id,
            "access_mode": access_mode,
            "session_id": session_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=IMPERSONATION_TOKEN_TTL_MINUTES)).timestamp()),
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    def validate_token(self, token: str) -> Optional[ImpersonationTokenPayload]:
        """
        Decode and validate an impersonation token.

        Checks:
            - Token signature is valid
            - Token has not expired
            - All required claims are present
            - Token type is "impersonation"

        Args:
            token: The JWT token string.

        Returns:
            ImpersonationTokenPayload if valid, None otherwise.
        """
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None

        # Verify all required claims are present
        if not self.REQUIRED_CLAIMS.issubset(payload.keys()):
            return None

        # Verify token type
        if payload.get("type") != "impersonation":
            return None

        return ImpersonationTokenPayload(
            type=payload["type"],
            admin_user_id=payload["admin_user_id"],
            target_user_id=payload["target_user_id"],
            access_mode=payload["access_mode"],
            session_id=payload["session_id"],
            iat=payload["iat"],
            exp=payload["exp"],
        )

    def decode_token(self, token: str) -> Optional[dict]:
        """
        Raw decoding of a token without full validation.

        Decodes the token and verifies signature/expiration but does not
        check for required claims or token type.

        Args:
            token: The JWT token string.

        Returns:
            Raw decoded payload dict if valid, None otherwise.
        """
        try:
            return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None
