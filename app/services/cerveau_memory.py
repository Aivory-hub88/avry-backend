"""
Document ingestion into a tenant's Aivory Cerveau memory.

Why this exists: `product.agent_profiles.knowledge` is a flat 12 000-char
field that the bridge injects into EVERY prompt. Uploading a document used to
mean merging its text into that field — so a long document was truncated, and
every turn paid for the whole blob whether or not it was relevant.

Cerveau's `POST /api/memory` became tenant-aware on 2026-08-30: with
`X-Tenant-Id`/`X-Agent-Type` it writes into `t_<user_id>.<agent_type>`, the
same structurally-jailed scope a tenant's own turn reads from, and each row is
embedded on the way in. That makes a document retrievable by relevance instead
of resident in every prompt.

Two things about that endpoint shape this module:

  1. **It stores one row per call and does NOT chunk.** `chunker.rs` is only
     used by an unrelated hardware-datasheet RAG; the memory store path embeds
     whatever content it is given, whole. So chunking is ours to do.
  2. **The store is an upsert on `(agent_id, key)`.** Deterministic keys mean
     re-uploading the same filename updates its chunks in place instead of
     duplicating them.

Category: `document`, its own tier rather than `core`. What actually runs in
production is `/usr/local/bin/cerveau-lifecycle.sh` (daily via
`cerveau-lifecycle.timer`), mirroring `PostgresMemory::run_lifecycle`: it
age-prunes `conversation` and `daily`, and budget-evicts `core`/`daily`/
`conversation` down to a per-tenant row cap ordered by
`importance DESC NULLS LAST, created_at DESC`. The Postgres store never writes
`importance`, so that ordering is pure recency — under `core`, an uploaded
document would be evicted before newer conversational memories once a tenant
passed 2 000 core rows. Documents are what an operator deliberately put there,
so they get a tier that is never age-pruned and carries its own generous cap
(see the `document` entry in that script).

(`[memory] purge_after_days` and `[memory.policy.retention_days_by_category]`
belong to the SQLite/filesystem hygiene path, which never touches the Postgres
backend at all — so the 30-day purge once feared for ingested documents was
never the real risk. See docs/CERVEAU-TECHNICAL-REFERENCE.md §3.3.)
"""

import logging
import os
import re
from typing import List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Both HA instances share one Postgres, so a document only needs to be written
# ONCE — unlike pending approvals, which live in per-instance stores and have
# to be queried on both. The second base is a failover, not a fan-out.
CERVEAU_BASES = [
    os.getenv("CERVEAU_APPROVAL_BASE_1", "http://host.docker.internal:3100"),
    os.getenv("CERVEAU_APPROVAL_BASE_2", "http://host.docker.internal:3101"),
]

# The HOST agent alias whose memory backend and embedding provider the tenant
# overlay borrows. It has to match the alias that serves the tenant's own
# turns, or the document lands in a store the agent never reads:
# `Config::resolved_runtime_agent_alias()` picks the alias literally named
# `default`, else the alphabetically-first ENABLED one — `analyst_brain` on
# :3100 today.
CERVEAU_HOST_AGENT = os.getenv("CERVEAU_HOST_AGENT", "analyst_brain")

# Ingestion ceiling, deliberately far above the 12 000-char knowledge-field cap
# this feature exists to escape. One embedding call per chunk, so the chunk cap
# is also the cost cap for a single upload.
MAX_INGEST_CHARS = 120_000
MAX_CHUNKS = 200

# ~1 500 chars ≈ 375 tokens, comfortably inside Cerveau's own
# `chunk_max_tokens = 512` sizing and well inside the embedding model's window.
CHUNK_CHARS = 1_500
CHUNK_OVERLAP = 150

_TIMEOUT = httpx.Timeout(30.0)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class IngestError(RuntimeError):
    """Ingestion could not be completed — the caller decides what the operator sees."""


def slug_for(filename: str) -> str:
    """Stable, key-safe identity for a document. Same filename -> same slug ->
    the upsert updates that document's chunks instead of duplicating them."""
    slug = _SLUG_RE.sub("-", (filename or "document").lower()).strip("-")
    return (slug or "document")[:48]


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split on paragraph boundaries where possible, hard-split where a single
    paragraph is longer than one chunk. Overlap only applies to the hard-split
    case: paragraph boundaries are already natural seams, and duplicating text
    across them would just pay for the same content twice at recall."""
    text = (text or "").strip()
    if not text:
        return []

    chunks: List[str] = []
    current = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > size:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(para):
                chunks.append(para[start:start + size])
                if start + size >= len(para):
                    break
                start += size - overlap
            continue
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= size:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks[:MAX_CHUNKS]


def _post_memory(base: str, payload: dict, user_id: str, agent_type: str) -> None:
    with httpx.Client(timeout=_TIMEOUT) as client:
        res = client.post(
            f"{base}/api/memory",
            json=payload,
            headers={"X-Tenant-Id": user_id, "X-Agent-Type": agent_type},
        )
    # The endpoint is fail-closed by design: malformed tenant headers or a
    # missing host agent are 400s, never a silent write to the install-wide
    # store. Surfacing the body keeps that distinction readable in the logs.
    if res.status_code != 200:
        raise IngestError(f"{res.status_code} from {base}/api/memory: {res.text[:300]}")


def ingest_document(user_id: str, agent_type: str, filename: str, text: str) -> Tuple[int, bool]:
    """Store `text` as embedded, per-chunk memories owned by this tenant.

    Returns (chunks_stored, truncated). Raises IngestError if nothing could be
    stored — a partial write is reported as success with the count, because the
    chunks that landed are genuinely usable and re-uploading is idempotent."""
    truncated = len(text) > MAX_INGEST_CHARS
    if truncated:
        text = text[:MAX_INGEST_CHARS]

    chunks = chunk_text(text)
    if not chunks:
        raise IngestError("no text to store")
    if len(chunks) == MAX_CHUNKS:
        truncated = True

    slug = slug_for(filename)
    total = len(chunks)
    stored = 0
    last_error: Optional[Exception] = None

    for idx, chunk in enumerate(chunks, start=1):
        payload = {
            "key": f"doc:{slug}:{idx:03d}",
            # Self-describing content: a recall hit is shown to the model on its
            # own, without the filename it came from unless it is in the text.
            "content": f"[Document: {filename} — part {idx} of {total}]\n{chunk}",
            "category": "document",
            "agent": CERVEAU_HOST_AGENT,
        }
        for base in CERVEAU_BASES:
            try:
                _post_memory(base, payload, user_id, agent_type)
                stored += 1
                break
            except Exception as e:  # noqa: BLE001 — try the failover base before giving up
                last_error = e
                logger.warning("cerveau memory write failed on %s (%s): %s", base, payload["key"], e)

    if stored == 0:
        raise IngestError(f"no chunk could be stored: {last_error}")
    if stored < total:
        logger.warning(
            "cerveau document ingest partial for %s/%s: %d of %d chunks",
            user_id, agent_type, stored, total,
        )
    return stored, truncated
