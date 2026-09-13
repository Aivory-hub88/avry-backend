"""
Tenant-scoped download tokens for Aivory-built MCP server images (e.g.
`od-mcp`, the first-party Odoo MCP server — see
docs/CERVEAU-ODOO-INTEGRATION-PLAN.md). These images are proprietary,
private-repo builds (not published to a public registry), so a tenant who
wants to self-host one needs an authenticated way to fetch the built
artifact without being handed a GitHub credential.

Table: product.mcp_image_download_tokens (avry-postgres) — same shape as
product.agent_api_keys (hashed, uniquely-constrained, atomically revocable),
for the same reason: a leaked download token is a bearer credential.

Dashboard-facing (JWT auth):
    POST   /api/v1/mcp-image-tokens              -> create (plaintext token shown ONCE)
    GET    /api/v1/mcp-image-tokens               -> list (never plaintext/hash)
    DELETE /api/v1/mcp-image-tokens/{id}          -> revoke

Tenant-infra-facing (X-Aivory-Download-Key — a dedicated header, same
convention as agent_api_keys.py's X-Aivory-Api-Key, never Authorization:
Bearer so it can't collide with the JWT path):
    GET /api/v1/mcp-images/{image_name}/download[?version=1.2.0]

The artifact itself is a `docker save | gzip` tarball dropped on local disk
by each image repo's own release CI (e.g. Od-MCP's GitHub Actions, on tag
push) — no object storage, no container registry, no new standing process.
Deliberately not a Docker registry: these images are self-hosted once per
tenant and rarely redeployed, so `curl | docker load` on setup is the right
amount of mechanism, not `docker pull`. See CERVEAU-ODOO-INTEGRATION-PLAN.md
for why the registry-pull-through alternative was considered and dropped.
"""

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.routes.agent_actions import get_current_user_payload
from app.utils.cache import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["mcp-image-downloads"])

# Releases land here as <image_name>-<version>.tar.gz, uploaded by each
# image's own release CI over the restricted deploy key (see
# docs/OD-MCP-RELEASE-PIPELINE.md). A `<image_name>-latest.txt` file next to
# them holds the current version string, rewritten atomically by the same
# upload step — no symlink dependency, works identically however the CI
# transfers files.
_RELEASES_DIR = Path(os.getenv("MCP_IMAGE_RELEASES_DIR", "/data/mcp-image-releases"))

# Loose allowlist: what CI actually produces, not user input — but this
# still ends up in a filesystem path, so keep it strict regardless.
_IMAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

_KNOWN_IMAGES = {"od-mcp"}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.mcp_image_download_tokens (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        TEXT NOT NULL,
    image_name     TEXT NOT NULL,
    key_prefix     TEXT NOT NULL,
    key_hash       TEXT NOT NULL,
    label          TEXT,
    status         TEXT NOT NULL DEFAULT 'active',
    last_pulled_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at     TIMESTAMPTZ,
    CONSTRAINT mcp_image_download_tokens_status_check CHECK (status IN ('active','revoked'))
);
CREATE INDEX IF NOT EXISTS mcp_image_download_tokens_user_image_idx
    ON product.mcp_image_download_tokens (user_id, image_name);
CREATE UNIQUE INDEX IF NOT EXISTS mcp_image_download_tokens_hash_idx
    ON product.mcp_image_download_tokens (key_hash);
"""

_schema_ready = False

# Soft cap — mirrors agent_api_keys.py's reasoning: a natural ceiling against
# runaway token creation, not a hard product limit worth tiering by plan yet
# (unlike agent_api_keys, this isn't a monetized capability on its own).
_MAX_TOKENS_PER_IMAGE = 10

# Per-token download rate ceiling — a tarball read is much heavier than a
# chat message, so this window is wider and the limit lower than
# agent_api_keys.py's per-minute message ceiling.
_RATE_LIMIT_PER_WINDOW = 5
_RATE_LIMIT_WINDOW_SECONDS = 3600


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — MCP image download tokens require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


def _check_image_name(image_name: str) -> None:
    if image_name not in _KNOWN_IMAGES or not _IMAGE_NAME_RE.match(image_name):
        raise HTTPException(status_code=404, detail=f"Unknown image '{image_name}'")


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class CreateTokenRequest(BaseModel):
    image_name: str
    label: Optional[str] = Field(default=None, max_length=200)


@router.post("/mcp-image-tokens", status_code=201)
def create_token(body: CreateTokenRequest, user: dict = Depends(get_current_user_payload)):
    _check_image_name(body.image_name)

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM product.mcp_image_download_tokens"
                " WHERE user_id = %s AND image_name = %s AND status = 'active'",
                (user["user_id"], body.image_name),
            )
            (active_count,) = cur.fetchone()
            if active_count >= _MAX_TOKENS_PER_IMAGE:
                raise HTTPException(
                    status_code=400,
                    detail=f"Token limit reached ({_MAX_TOKENS_PER_IMAGE} active tokens for this image). Revoke one first.",
                )

            raw_key = f"odmcp_live_{secrets.token_urlsafe(32)}"
            key_prefix = raw_key[:14]
            key_hash = _hash_key(raw_key)
            cur.execute(
                """
                INSERT INTO product.mcp_image_download_tokens (user_id, image_name, key_prefix, key_hash, label)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (user["user_id"], body.image_name, key_prefix, key_hash, body.label),
            )
            row_id, created_at = cur.fetchone()
        conn.commit()
        logger.info(f"MCP image download token created: {body.image_name} for {user['user_id']} (prefix {key_prefix})")
        return {
            "id": str(row_id),
            "token": raw_key,  # shown exactly once — never retrievable again
            "key_prefix": key_prefix,
            "label": body.label,
            "image_name": body.image_name,
            "created_at": created_at.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"mcp image token create failed: {e}")
        raise HTTPException(status_code=503, detail="Token store unavailable")
    finally:
        conn.close()


@router.get("/mcp-image-tokens")
def list_tokens(image_name: Optional[str] = None, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            if image_name:
                _check_image_name(image_name)
                cur.execute(
                    "SELECT id, image_name, key_prefix, label, status, last_pulled_at, created_at"
                    " FROM product.mcp_image_download_tokens WHERE user_id = %s AND image_name = %s"
                    " ORDER BY created_at DESC",
                    (user["user_id"], image_name),
                )
            else:
                cur.execute(
                    "SELECT id, image_name, key_prefix, label, status, last_pulled_at, created_at"
                    " FROM product.mcp_image_download_tokens WHERE user_id = %s ORDER BY created_at DESC",
                    (user["user_id"],),
                )
            rows = cur.fetchall()
        return {
            "tokens": [
                {
                    "id": str(r[0]),
                    "image_name": r[1],
                    "key_prefix": r[2],
                    "label": r[3],
                    "status": r[4],
                    "last_pulled_at": r[5].isoformat() if r[5] else None,
                    "created_at": r[6].isoformat(),
                }
                for r in rows
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"mcp image token list failed: {e}")
        raise HTTPException(status_code=503, detail="Token store unavailable")
    finally:
        conn.close()


@router.delete("/mcp-image-tokens/{token_id}")
def revoke_token(token_id: str, user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE product.mcp_image_download_tokens
                SET status = 'revoked', revoked_at = now()
                WHERE id = %s AND user_id = %s AND status = 'active'
                RETURNING id
                """,
                (token_id, user["user_id"]),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            raise HTTPException(status_code=404, detail="Token not found or already revoked")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"mcp image token revoke failed: {e}")
        raise HTTPException(status_code=503, detail="Token store unavailable")
    finally:
        conn.close()


# ============================================================================
# TENANT-INFRA-FACING: the actual tarball download
# ============================================================================


def _resolve_token(raw_key: str) -> Optional[dict]:
    """Returns {id, user_id, image_name} for a valid, active token, else None.
    Fire-and-forget last_pulled_at update — never blocks the caller on it."""
    if not raw_key or not raw_key.startswith("odmcp_live_"):
        return None
    conn = _connect()
    try:
        _ensure_schema(conn)
        key_hash = _hash_key(raw_key)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, image_name FROM product.mcp_image_download_tokens"
                " WHERE key_hash = %s AND status = 'active'",
                (key_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "UPDATE product.mcp_image_download_tokens SET last_pulled_at = now() WHERE id = %s",
                (row[0],),
            )
        conn.commit()
        return {"id": str(row[0]), "user_id": row[1], "image_name": row[2]}
    except Exception as e:
        logger.error(f"mcp image token resolve failed: {e}")
        return None
    finally:
        conn.close()


def _release_path(image_name: str, version: str) -> Path:
    return _RELEASES_DIR / f"{image_name}-{version}.tar.gz"


def _resolve_latest_version(image_name: str) -> Optional[str]:
    marker = _RELEASES_DIR / f"{image_name}-latest.txt"
    if not marker.is_file():
        return None
    version = marker.read_text().strip()
    return version if _VERSION_RE.match(version) else None


@router.get("/mcp-images/{image_name}/download")
def download_image(
    image_name: str,
    version: Optional[str] = None,
    x_aivory_download_key: str = Header(default=""),
):
    _check_image_name(image_name)

    token_info = _resolve_token(x_aivory_download_key)
    if not token_info or token_info["image_name"] != image_name:
        raise HTTPException(status_code=401, detail="Invalid or revoked download key")

    try:
        within_limit = check_rate_limit(
            f"mcp_image_download:{token_info['id']}",
            limit=_RATE_LIMIT_PER_WINDOW,
            window=_RATE_LIMIT_WINDOW_SECONDS,
        )
    except Exception as e:
        logger.error(f"rate limit check failed for download token {token_info['id']}: {e}")
        within_limit = True  # fail open — Redis being down must not block a legitimate download
    if not within_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({_RATE_LIMIT_PER_WINDOW} downloads/hour for this token).",
        )

    if version:
        if not _VERSION_RE.match(version):
            raise HTTPException(status_code=400, detail="Invalid version format")
    else:
        version = _resolve_latest_version(image_name)
        if not version:
            raise HTTPException(status_code=404, detail=f"No published release found for '{image_name}'")

    path = _release_path(image_name, version)
    # Defense in depth against the regex above, not the primary control:
    # resolve() + is_relative_to() guarantees the final path can't have
    # escaped _RELEASES_DIR regardless of how `version` was constructed.
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(_RELEASES_DIR.resolve()) or not resolved.is_file():
            raise HTTPException(status_code=404, detail=f"Release '{version}' not found for '{image_name}'")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail=f"Release '{version}' not found for '{image_name}'")

    logger.info(f"MCP image download: {image_name}:{version} for token {token_info['id']}")
    return FileResponse(
        path=resolved,
        media_type="application/gzip",
        filename=f"{image_name}-{version}.tar.gz",
    )
