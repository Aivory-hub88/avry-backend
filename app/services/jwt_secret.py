"""The one JWT signing secret, read once, fail-closed.

Every module used to read JWT_SECRET itself with a hard-coded fallback
("your-secret-key-change-in-production"). A container started without the
env var then signed and accepted tokens with a secret that is public in this
repo, so anyone could mint an admin token. Now a missing, blank or
placeholder secret raises at import time and the service refuses to start.
"""
import os

# Known placeholders that must never sign a real token.
_PLACEHOLDERS = {
    "your-secret-key-change-in-production",
    "your_secret_key_here",  # README example
    "change-me",
    "changeme",
    "secret",
}


class MissingJwtSecretError(RuntimeError):
    pass


def load_jwt_secret() -> str:
    # Returned as-is: the satellite services verify with the raw env value.
    secret = os.getenv("JWT_SECRET") or ""
    if not secret.strip():
        raise MissingJwtSecretError("JWT_SECRET is not set; refusing to start")
    if secret.strip().lower() in _PLACEHOLDERS:
        raise MissingJwtSecretError("JWT_SECRET is a placeholder value; refusing to start")
    return secret


JWT_SECRET = load_jwt_secret()
