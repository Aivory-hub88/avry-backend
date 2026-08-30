"""
Agent identity profiles — per-user customization of the prebuilt deployable agents.

Each user can give every agent type its own identity (agent name, business
name, tone, knowledge/FAQ, extra instructions). The bridge runtime injects the
profile into the system prompt PER REQUEST, so different operators' identities
can never collide: there is no shared mutable identity state anywhere.

Values are operator-authored and end up inside an LLM prompt, so they are
treated as untrusted: length-capped and control-char-stripped here, and
wrapped in a non-overridable "operator configuration" data block by the bridge.

Table: product.agent_profiles (avry-postgres).

Dashboard-facing (JWT auth):
    GET /api/v1/agent-profiles                   -> all of the caller's profiles
    GET /api/v1/agent-profiles/{agent_type}      -> one profile (or defaults)
    PUT /api/v1/agent-profiles/{agent_type}      -> upsert
    DELETE /api/v1/agent-profiles/{agent_type}   -> reset to default identity
    POST /api/v1/agent-profiles/{agent_type}/knowledge/document
        -> extract text from an uploaded document (PDF/Word/Excel/CSV/text).
           On Cerveau it is chunked into the tenant's own embedded memory and
           persisted immediately; on the legacy engine it comes back merged
           into the knowledge field for the dashboard to save via PUT

Internal (bridge-facing, X-Internal-Token):
    GET /api/v1/agent-profiles/internal/{user_id}/{agent_type}

The internal response also carries `engine` ('legacy' | 'cerveau') — the
Phase 6.1 per-tenant rollout flag for whether the bridge should route this
tenant's messages to Aivory Cerveau instead of the legacy agent loop. It is
deliberately NOT exposed on the dashboard-facing GET/PUT/DELETE routes above:
there is no UI for it yet, and an operator's own JWT should never be able to
read or flip which engine serves them.
"""

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.routes.agent_actions import get_current_user_payload, require_internal_token
from app.services import attachment_extractor, cerveau_memory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-profiles", tags=["agent-profiles"])

AGENT_TYPES = {"autonomous", "customer_service", "leads_qualifier", "finance_invoice_ops", "office_assistant"}

# Per-field length caps: generous enough for a real business identity, small
# enough that a profile can't blow up prompt size or hide a jailbreak essay.
FIELD_CAPS = {
    "agent_name": 80,
    "business_name": 120,
    "tone": 200,
    "language_pref": 200,  # comma-separated list from the dashboard multi-select
    "business_description": 1500,
    # Matches attachment_extractor.MAX_EXTRACTED_CHARS -- the knowledge
    # field is the one place meant to hold a whole uploaded document, not
    # just a short FAQ, so it gets real headroom the other fields don't.
    "knowledge": 12000,
    "custom_instructions": 1500,
    "greeting": 300,
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.agent_profiles (
    user_id              TEXT NOT NULL,
    agent_type           TEXT NOT NULL,
    agent_name           TEXT,
    business_name        TEXT,
    tone                 TEXT,
    language_pref        TEXT,
    business_description TEXT,
    knowledge            TEXT,
    custom_instructions  TEXT,
    greeting             TEXT,
    engine               TEXT NOT NULL DEFAULT 'legacy',
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, agent_type)
);
"""

# CREATE TABLE IF NOT EXISTS above only helps a fresh install — the table
# already exists in production, so the new column needs its own additive
# migration (same idiom as pg_service.py's ADD COLUMN IF NOT EXISTS calls).
_ALTER_SQL = """
ALTER TABLE product.agent_profiles
    ADD COLUMN IF NOT EXISTS engine TEXT NOT NULL DEFAULT 'legacy';
"""

_schema_ready = False

# Strip ASCII control chars except newline/tab (keeps multi-line FAQ readable,
# kills zero-width/escape-sequence smuggling).
_CONTROL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


def _sanitize(value: Optional[str], cap: int) -> Optional[str]:
    if value is None:
        return None
    cleaned = _CONTROL_RE.sub("", str(value)).strip()
    return cleaned[:cap] if cleaned else None


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — agent profiles require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
        cur.execute(_ALTER_SQL)
    conn.commit()
    _schema_ready = True


_COLUMNS = [
    "agent_name", "business_name", "tone", "language_pref",
    "business_description", "knowledge", "custom_instructions", "greeting",
]

# Internal-only superset — adds `engine`, the Phase 6.1 rollout flag. It is
# never RETURNED by a dashboard-facing route; the document-upload route does
# read it, to decide whether the upload goes into Cerveau memory or the flat
# knowledge field, but the value itself stays server-side.
_INTERNAL_COLUMNS = _COLUMNS + ["engine"]


def _row_to_profile(row, columns=_COLUMNS) -> dict:
    profile = dict(zip(columns, row[:-1]))
    profile["updated_at"] = row[-1].isoformat() if row[-1] else None
    return profile


def load_profile(user_id: str, agent_type: str) -> Optional[dict]:
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)}, updated_at FROM product.agent_profiles"
                " WHERE user_id = %s AND agent_type = %s",
                (user_id, agent_type),
            )
            row = cur.fetchone()
        return _row_to_profile(row) if row else None
    finally:
        conn.close()


def load_profile_internal(user_id: str, agent_type: str) -> Optional[dict]:
    """Same as load_profile(), plus `engine`. The value is bridge-facing only —
    see the module docstring for why it is never exposed on a dashboard route."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_INTERNAL_COLUMNS)}, updated_at FROM product.agent_profiles"
                " WHERE user_id = %s AND agent_type = %s",
                (user_id, agent_type),
            )
            row = cur.fetchone()
        return _row_to_profile(row, _INTERNAL_COLUMNS) if row else None
    finally:
        conn.close()


class ProfileUpdate(BaseModel):
    agent_name: Optional[str] = Field(default=None, max_length=500)
    business_name: Optional[str] = Field(default=None, max_length=500)
    tone: Optional[str] = Field(default=None, max_length=1000)
    language_pref: Optional[str] = Field(default=None, max_length=500)
    business_description: Optional[str] = Field(default=None, max_length=8000)
    knowledge: Optional[str] = Field(default=None, max_length=20000)
    custom_instructions: Optional[str] = Field(default=None, max_length=8000)
    greeting: Optional[str] = Field(default=None, max_length=1000)


def _check_agent_type(agent_type: str) -> None:
    if agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown agent type '{agent_type}'")


@router.get("/internal/{user_id}/{agent_type}", dependencies=[Depends(require_internal_token)])
def internal_get(user_id: str, agent_type: str):
    _check_agent_type(agent_type)
    try:
        profile = load_profile_internal(user_id, agent_type)
    except Exception as e:
        logger.error(f"profile lookup failed for {user_id}/{agent_type}: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    return {"profile": profile}


@router.get("")
def list_profiles(user: dict = Depends(get_current_user_payload)):
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT agent_type, {', '.join(_COLUMNS)}, updated_at FROM product.agent_profiles"
                " WHERE user_id = %s",
                (user["user_id"],),
            )
            rows = cur.fetchall()
        return {"profiles": {row[0]: _row_to_profile(row[1:]) for row in rows}}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"profile list failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    finally:
        conn.close()


@router.get("/{agent_type}")
def get_profile(agent_type: str, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(agent_type)
    try:
        profile = load_profile(user["user_id"], agent_type)
    except Exception as e:
        logger.error(f"profile lookup failed: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    return {"agent_type": agent_type, "profile": profile}


_SUPPORTED_DOCUMENT_EXTS = {".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".txt", ".md"}


@router.post("/{agent_type}/knowledge/document")
async def upload_knowledge_document(
    agent_type: str,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user_payload),
):
    """Take an uploaded document into this agent's knowledge.

    Two paths, chosen by which engine serves the agent:

    - **Cerveau** (`engine = 'cerveau'`): the text is chunked and stored as
      embedded, tenant-scoped memories via Cerveau's `POST /api/memory`. It is
      retrieved by relevance at recall time, so a long document is no longer
      truncated to 12 000 chars and no longer rides along in every prompt.
      Persisted on upload -- there is nothing for the dashboard to save
      afterwards.

    - **Legacy**: unchanged. The extracted text is merged into the knowledge
      field and returned for the dashboard to place there and save via the
      normal PUT, exactly like typing it in by hand would. The legacy agent
      loop has no tenant memory to read from, so the flat field is still the
      only mechanism it has.

    The response carries `ingested` so the dashboard can tell the two apart;
    `knowledge` is always present, and on the Cerveau path it is the stored
    value unchanged (nothing to merge)."""
    _check_agent_type(agent_type)
    filename = file.filename or "document"
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _SUPPORTED_DOCUMENT_EXTS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Try PDF, Word (.docx), Excel (.xlsx), CSV, or plain text.",
        )

    try:
        current = load_profile_internal(user["user_id"], agent_type)
    except Exception as e:
        logger.error(f"profile lookup failed before knowledge upload: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")

    to_cerveau = ((current or {}).get("engine") or "legacy") == "cerveau"
    # Only the Cerveau path can afford the large extraction: on the legacy path
    # the text is headed for a 12 000-char prompt field, so extracting more
    # would just be thrown away.
    max_chars = cerveau_memory.MAX_INGEST_CHARS if to_cerveau else attachment_extractor.MAX_EXTRACTED_CHARS

    data = await file.read()
    extracted = attachment_extractor.extract_document_text(
        filename, data, file.content_type, max_chars=max_chars
    )
    if not extracted:
        raise HTTPException(status_code=422, detail=f"Could not extract any text from '{filename}'.")
    if extracted.startswith("[") and extracted.endswith("]"):
        # extractor's own placeholder for "too large" / "could not read" cases
        raise HTTPException(status_code=422, detail=extracted[1:-1])

    existing = ((current or {}).get("knowledge") or "").strip()

    if to_cerveau:
        try:
            chunks, truncated = await run_in_threadpool(
                cerveau_memory.ingest_document,
                user["user_id"], agent_type, filename, extracted,
            )
        except cerveau_memory.IngestError as e:
            logger.error(f"cerveau document ingest failed for {agent_type}: {e}")
            raise HTTPException(
                status_code=503,
                detail="Could not add that document to the agent's memory. Please try again.",
            )
        return {
            "ingested": True,
            "chunks": chunks,
            "filename": filename,
            # Unchanged -- the document did not go into this field, and handing
            # back anything else would make the dashboard overwrite it.
            "knowledge": existing,
            "truncated": truncated,
            "extracted_chars": len(extracted),
        }

    section = f"--- From {filename} ---\n{extracted}"
    combined = f"{existing}\n\n{section}" if existing else section

    cap = FIELD_CAPS["knowledge"]
    truncated = len(combined) > cap
    combined = _sanitize(combined, cap) or ""

    return {
        "ingested": False,
        "knowledge": combined,
        "truncated": truncated,
        "extracted_chars": len(extracted),
    }


@router.put("/{agent_type}")
def upsert_profile(agent_type: str, body: ProfileUpdate, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(agent_type)
    values = {field: _sanitize(getattr(body, field), cap) for field, cap in FIELD_CAPS.items()}

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cols = list(values.keys())
            assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            cur.execute(
                f"""
                INSERT INTO product.agent_profiles (user_id, agent_type, {', '.join(cols)}, updated_at)
                VALUES (%s, %s, {', '.join(['%s'] * len(cols))}, now())
                ON CONFLICT (user_id, agent_type)
                DO UPDATE SET {assignments}, updated_at = now()
                """,
                [user["user_id"], agent_type, *[values[c] for c in cols]],
            )
        conn.commit()
        logger.info(f"Agent profile saved: {agent_type} for {user['user_id']}")
        return {"ok": True, "agent_type": agent_type, "profile": values}
    except Exception as e:
        logger.error(f"profile save failed: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    finally:
        conn.close()


@router.delete("/{agent_type}")
def delete_profile(agent_type: str, user: dict = Depends(get_current_user_payload)):
    _check_agent_type(agent_type)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM product.agent_profiles WHERE user_id = %s AND agent_type = %s",
                (user["user_id"], agent_type),
            )
        conn.commit()
        return {"ok": True}
    except Exception as e:
        logger.error(f"profile delete failed: {e}")
        raise HTTPException(status_code=503, detail="Profile store unavailable")
    finally:
        conn.close()
