"""Which kind of JWT a decoded payload is.

Access and refresh tokens are signed with the same secret, so a signature
check alone can't tell them apart. Before this module, any route that only
checked the signature also accepted a refresh token as a bearer — and a
refresh token lives 30 days and keeps working after logout, because logout
only deletes the server-side session that the refresh endpoint consults.

Tokens issued from now on carry "type" ("access" / "refresh"). Tokens issued
before that have no type: a legacy refresh token is recognised by its
session_id claim (access tokens never carry one). Impersonation tokens
(type "impersonation") are validated by ImpersonationTokenService and are
neither kind here.
"""


def is_refresh_payload(payload: dict) -> bool:
    kind = payload.get("type")
    if kind == "refresh":
        return True
    return kind is None and "session_id" in payload


def is_access_payload(payload: dict) -> bool:
    kind = payload.get("type")
    if kind == "access":
        return True
    return kind is None and "session_id" not in payload
