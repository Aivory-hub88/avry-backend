"""
Tenant scheduled runs — lets a paying operator say "every Monday at 08:00,
summarise last week's tickets" and have their Cerveau agent do it unattended.
See docs/ADR-009-CERVEAU-SCHEDULED-RUNS.md, Phase 2.

Table: product.tenant_scheduled_runs (avry-postgres). Mirrors
tenant_mcp_servers.py's shape and conventions throughout — same connection
helper, same idempotent `_ensure_schema`, same tier gate, same
internal-token seam for Cerveau.

Dashboard-facing (JWT auth):
    POST   /api/v1/tenant-scheduled-runs                  -> create
    GET    /api/v1/tenant-scheduled-runs?agent_type=...    -> list
    PATCH  /api/v1/tenant-scheduled-runs/{id}              -> pause / resume / edit
    DELETE /api/v1/tenant-scheduled-runs/{id}              -> soft-delete

Internal (Cerveau-facing, X-Internal-Token):
    GET /api/v1/tenant-scheduled-runs/internal/{user_id}/{agent_type}
        -> that tenant's live schedules
    GET /api/v1/tenant-scheduled-runs/internal/all
        -> every live schedule, for the scheduler's own reconcile pass

*** These schedules do not run yet. ***
ADR-009 Phase 1 built the runtime half (a cron job can carry a tenant and
Cerveau resolves it at fire time, verified live); this is the store and the
API. What does not exist yet is the sync that copies rows from here into
Cerveau's own cron store. Until it does, a row created here stays
`pending_activation` forever and nothing fires. That is why `status` is a
real column rather than a derived "enabled" boolean: the record must never
claim to be live before Cerveau owns it. Do not surface this in the
dashboard (Phase 3) before that sync exists — "appears to work in the UI and
silently never runs" is the exact failure ADR-009 §6 was written about.

Two deliberate hard requirements, both learned the expensive way:

1. `timezone` is REQUIRED and must be a real IANA zone. Cerveau resolves a
   cron expression with no timezone against the *runtime host's* OS zone —
   Asia/Shanghai on the current VPS — which is meaningless to a tenant and
   silently produces a schedule hours away from the one they asked for
   (ADR-009 §6a). A tenant-facing schedule may never inherit that.

2. Recurring unattended agent runs are the single easiest way to generate
   surprise LLM spend in this product (ADR-009 §8), so the quota is per-tier
   and small, and `_reject_runaway_frequency` refuses the most common
   runaway shapes outright.
"""

import logging
import os
import re
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.database.db_service import DatabaseService
from app.routes.agent_actions import get_current_user_payload, require_internal_token
from app.routes.agent_profiles import AGENT_TYPES, load_profile_internal
from app.services import tiers
from app.services.telegram_service import is_superadmin, load_user_record

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tenant-scheduled-runs", tags=["tenant-scheduled-runs"])

_db_service = DatabaseService()

_MIN_TIER = "operational"

_NAME_RE = re.compile(r"^[a-zA-Z0-9 _-]{1,60}$")
_MAX_PROMPT_LEN = 2000

# Per-(user_id, agent_type) quota, by plan. Deliberately smaller than the
# custom-MCP ladder: an MCP server costs nothing until a turn calls it,
# whereas every schedule here is a recurring, unattended LLM turn that bills
# whether or not anyone reads the result.
_MAX_SCHEDULES_BY_TIER = {
    "operational": 1,
    "business": 5,
    "enterprise": 20,
}

#: Smallest step allowed in the minute field. See `_reject_runaway_frequency`.
_MIN_MINUTE_STEP = 15

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS product.tenant_scheduled_runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          TEXT NOT NULL,
    agent_type       TEXT NOT NULL,
    name             TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    cron_expression  TEXT NOT NULL,
    timezone         TEXT NOT NULL,
    enabled          BOOLEAN NOT NULL DEFAULT true,
    status           TEXT NOT NULL DEFAULT 'pending_activation',
    status_detail    TEXT,
    cerveau_job_id   TEXT,
    last_synced_at   TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at       TIMESTAMPTZ,
    CONSTRAINT tenant_scheduled_runs_status_check
        CHECK (status IN ('pending_activation','active','paused','failed'))
);
CREATE UNIQUE INDEX IF NOT EXISTS tenant_scheduled_runs_user_agent_name_idx
    ON product.tenant_scheduled_runs (user_id, agent_type, name)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS tenant_scheduled_runs_live_idx
    ON product.tenant_scheduled_runs (user_id, agent_type)
    WHERE deleted_at IS NULL;
"""

_schema_ready = False

#: Columns every dashboard-facing response is built from. `prompt` is
#: included: unlike an MCP auth header there is nothing secret in it, and a
#: tenant editing a schedule needs to see what it actually asks.
_LIST_COLUMNS = (
    "id, agent_type, name, prompt, cron_expression, timezone, enabled, status, "
    "status_detail, last_synced_at, created_at, updated_at"
)


def _connect():
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — tenant scheduled runs require Postgres")
    return psycopg2.connect(dsn, connect_timeout=5)


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()
    _schema_ready = True


def _check_agent_type(agent_type: str) -> None:
    if agent_type not in AGENT_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown agent type '{agent_type}'")


def _require_paid_tier(user_id: str) -> str:
    # Same reasoning as tenant_mcp_servers._require_paid_tier: the JWT never
    # carries `tier`, so the live record has to be re-read.
    record = load_user_record(_db_service, user_id) or {"user_id": user_id}
    if is_superadmin(record):
        return "enterprise"
    tier = tiers.account_tier(record.get("tier"))
    if not tiers.meets(tier, _MIN_TIER):
        raise HTTPException(
            status_code=403,
            detail=(
                "Scheduled runs are available on paid plans (Operational, Business, "
                "or Enterprise). Upgrade to schedule one."
            ),
        )
    return tier


def _require_cerveau_engine(user_id: str, agent_type: str) -> None:
    profile = load_profile_internal(user_id, agent_type)
    engine = (profile or {}).get("engine") or "legacy"
    if engine != "cerveau":
        raise HTTPException(
            status_code=403,
            detail="Scheduled runs require this agent to be running on Aivory Cerveau.",
        )


# ── Validation ───────────────────────────────────────────────────────────

_FIELD_RANGES = {
    0: (0, 59),   # minute
    1: (0, 23),   # hour
    2: (1, 31),   # day of month
    3: (1, 12),   # month
    # 0 *and* 7 are Sunday in standard crontab, and Cerveau's
    # `normalize_weekday_field` accepts both (its own error reads
    # "expected 0-7"). Capping this at 6 would reject a spelling the
    # runtime handles fine — this boundary is meant to accept exactly what
    # the runtime accepts, not less.
    4: (0, 7),    # day of week, 0 and 7 = Sunday
}
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
_TERM_RE = re.compile(r"^(\*|\d+|\d+-\d+)(/\d+)?$")

#: Fields where cron also accepts three-letter names, which Cerveau passes
#: straight through to the `cron` crate (its weekday normaliser bails out
#: early on any alphabetic field for exactly this reason). Validated by name
#: set rather than waved through, so a typo still fails here instead of at
#: fire time.
_NAME_TERMS = {
    3: {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"},
    4: {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"},
}
_NAME_TERM_RE = re.compile(r"^([A-Za-z]{3})(-([A-Za-z]{3}))?$")


def _validate_cron_expression(expr: str) -> str:
    """Accept exactly the 5-field Unix form Cerveau accepts, nothing looser.

    Cerveau normalises this into the `cron` crate's 6-field form (prepending
    a seconds field and shifting day-of-week), so anything accepted here has
    to be expressible there. Validating by hand rather than reaching for a
    general cron library is deliberate: the goal is to accept exactly what
    the runtime accepts, not everything some parser tolerates.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise HTTPException(
            status_code=400,
            detail=(
                f"cron_expression must have exactly 5 fields "
                f"(minute hour day-of-month month day-of-week), got {len(fields)}"
            ),
        )
    for i, field in enumerate(fields):
        lo, hi = _FIELD_RANGES[i]
        for term in field.split(","):
            if i in _NAME_TERMS and any(c.isalpha() for c in term):
                nm = _NAME_TERM_RE.match(term)
                names = [nm.group(1).upper(), nm.group(3).upper()] if nm and nm.group(3) else (
                    [nm.group(1).upper()] if nm else []
                )
                if not names or any(n not in _NAME_TERMS[i] for n in names):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"cron_expression {_FIELD_NAMES[i]} field: '{term}' is not a valid "
                            f"name (expected one of {', '.join(sorted(_NAME_TERMS[i]))}, "
                            f"optionally as a RANGE-RANGE)"
                        ),
                    )
                continue
            m = _TERM_RE.match(term)
            if not m:
                raise HTTPException(
                    status_code=400,
                    detail=f"cron_expression {_FIELD_NAMES[i]} field: '{term}' is not a valid term",
                )
            base, step = m.group(1), m.group(2)
            if step is not None and int(step[1:]) < 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"cron_expression {_FIELD_NAMES[i]} field: step must be >= 1",
                )
            if base == "*":
                continue
            bounds = [int(x) for x in base.split("-")]
            for value in bounds:
                if not lo <= value <= hi:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"cron_expression {_FIELD_NAMES[i]} field: {value} is outside {lo}-{hi}"
                        ),
                    )
            if len(bounds) == 2 and bounds[0] > bounds[1]:
                raise HTTPException(
                    status_code=400,
                    detail=f"cron_expression {_FIELD_NAMES[i]} field: range '{base}' is inverted",
                )
    return expr


def _reject_runaway_frequency(expr: str) -> None:
    """Refuse the schedules most likely to produce a surprise bill.

    This is a floor on the minute field, not a general minimum-interval
    calculation — computing the true spacing of an arbitrary cron expression
    means expanding it, and claiming a guarantee this does not provide would
    be worse than the narrow check it actually is. It catches the two shapes
    that cause runaway spend in practice: `*` (every minute) and a fine
    step like `*/5`. Everything coarser is allowed through.
    """
    minute = expr.split()[0]
    for term in minute.split(","):
        if term == "*":
            raise HTTPException(
                status_code=400,
                detail=(
                    "cron_expression runs every minute. A scheduled run is a full "
                    "agent turn and bills like one — pick a specific minute, or a "
                    f"step of at least */{_MIN_MINUTE_STEP}."
                ),
            )
        if "/" in term:
            # Both call sites run `_validate_cron_expression` first, which
            # guarantees a numeric step — but depending on call order for
            # something that would otherwise 500 is not worth the two lines
            # it costs to not depend on it.
            raw = term.split("/", 1)[1]
            if not raw.isdigit():
                raise HTTPException(
                    status_code=400,
                    detail=f"cron_expression minute field: step '{raw}' is not a number",
                )
            step = int(raw)
            if step < _MIN_MINUTE_STEP:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"cron_expression runs every {step} minutes. A scheduled run "
                        f"is a full agent turn and bills like one — the minimum step "
                        f"is */{_MIN_MINUTE_STEP}."
                    ),
                )


def _validate_timezone(tz: str) -> str:
    """Require a real IANA zone — see this module's docstring, point 1."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise HTTPException(
            status_code=400,
            detail=(
                f"timezone '{tz}' is not a known IANA zone (e.g. 'Asia/Jakarta', "
                f"'Europe/London'). It is required: without it the schedule would "
                f"resolve against the server's own timezone, not yours."
            ),
        )
    return tz


# ── Request models ───────────────────────────────────────────────────────


class CreateScheduledRunRequest(BaseModel):
    agent_type: str
    name: str = Field(..., min_length=1, max_length=60)
    prompt: str = Field(..., min_length=1, max_length=_MAX_PROMPT_LEN)
    cron_expression: str = Field(..., min_length=1, max_length=200)
    timezone: str = Field(..., min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name may contain letters, numbers, spaces, hyphens and underscores only")
        return v


class UpdateScheduledRunRequest(BaseModel):
    """Every field optional — this is both the pause/resume and the edit route."""

    enabled: Optional[bool] = None
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    prompt: Optional[str] = Field(None, min_length=1, max_length=_MAX_PROMPT_LEN)
    cron_expression: Optional[str] = Field(None, min_length=1, max_length=200)
    timezone: Optional[str] = Field(None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _NAME_RE.match(v):
            raise ValueError("name may contain letters, numbers, spaces, hyphens and underscores only")
        return v


def _row_to_dict(row) -> dict:
    (
        row_id, agent_type, name, prompt, cron_expression, tz,
        enabled, status, status_detail, last_synced_at, created_at, updated_at,
    ) = row
    return {
        "id": str(row_id),
        "agent_type": agent_type,
        "name": name,
        "prompt": prompt,
        "cron_expression": cron_expression,
        "timezone": tz,
        "enabled": enabled,
        "status": status,
        "status_detail": status_detail,
        "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
        "created_at": created_at.isoformat() if created_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


# ── Dashboard-facing routes ──────────────────────────────────────────────


@router.post("")
def create_scheduled_run(
    body: CreateScheduledRunRequest,
    user: dict = Depends(get_current_user_payload),
):
    _check_agent_type(body.agent_type)
    tier = _require_paid_tier(user["user_id"])
    _require_cerveau_engine(user["user_id"], body.agent_type)
    _validate_cron_expression(body.cron_expression)
    _reject_runaway_frequency(body.cron_expression)
    _validate_timezone(body.timezone)

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM product.tenant_scheduled_runs"
                " WHERE user_id = %s AND agent_type = %s AND deleted_at IS NULL",
                (user["user_id"], body.agent_type),
            )
            (active_count,) = cur.fetchone()
            quota = _MAX_SCHEDULES_BY_TIER.get(tier, 1)
            if active_count >= quota:
                plural = "run" if quota == 1 else "runs"
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Your {tiers.display_name(tier)} plan allows {quota} scheduled "
                        f"{plural} per agent. Remove one before scheduling another."
                    ),
                )
            cur.execute(
                "SELECT 1 FROM product.tenant_scheduled_runs"
                " WHERE user_id = %s AND agent_type = %s AND name = %s AND deleted_at IS NULL",
                (user["user_id"], body.agent_type, body.name),
            )
            if cur.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail=f"A scheduled run named '{body.name}' already exists for this agent.",
                )
            cur.execute(
                f"""
                INSERT INTO product.tenant_scheduled_runs
                    (user_id, agent_type, name, prompt, cron_expression, timezone)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_LIST_COLUMNS}
                """,
                (
                    user["user_id"], body.agent_type, body.name,
                    body.prompt, body.cron_expression, body.timezone,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"scheduled-run create failed for {user['user_id']}/{body.agent_type}: {e}")
        raise HTTPException(status_code=503, detail="Scheduled-run store unavailable")
    finally:
        conn.close()

    return {"scheduled_run": _row_to_dict(row)}


@router.get("")
def list_scheduled_runs(
    agent_type: Optional[str] = None,
    user: dict = Depends(get_current_user_payload),
):
    if agent_type is not None:
        _check_agent_type(agent_type)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            if agent_type:
                cur.execute(
                    f"SELECT {_LIST_COLUMNS} FROM product.tenant_scheduled_runs"
                    " WHERE user_id = %s AND agent_type = %s AND deleted_at IS NULL"
                    " ORDER BY created_at",
                    (user["user_id"], agent_type),
                )
            else:
                cur.execute(
                    f"SELECT {_LIST_COLUMNS} FROM product.tenant_scheduled_runs"
                    " WHERE user_id = %s AND deleted_at IS NULL ORDER BY agent_type, created_at",
                    (user["user_id"],),
                )
            rows = cur.fetchall()
    except Exception as e:
        logger.error(f"scheduled-run list failed for {user['user_id']}: {e}")
        raise HTTPException(status_code=503, detail="Scheduled-run store unavailable")
    finally:
        conn.close()
    return {"scheduled_runs": [_row_to_dict(r) for r in rows]}


@router.patch("/{run_id}")
def update_scheduled_run(
    run_id: str,
    body: UpdateScheduledRunRequest,
    user: dict = Depends(get_current_user_payload),
):
    updates: list[tuple[str, object]] = []
    if body.enabled is not None:
        updates.append(("enabled", body.enabled))
    if body.name is not None:
        updates.append(("name", body.name))
    if body.prompt is not None:
        updates.append(("prompt", body.prompt))
    if body.cron_expression is not None:
        _validate_cron_expression(body.cron_expression)
        _reject_runaway_frequency(body.cron_expression)
        updates.append(("cron_expression", body.cron_expression))
    if body.timezone is not None:
        _validate_timezone(body.timezone)
        updates.append(("timezone", body.timezone))
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Any content change invalidates whatever Cerveau currently holds, so the
    # row drops back to pending_activation for the sync to pick up again.
    # Toggling `enabled` alone does the same: a paused row must not keep
    # claiming `active` while Cerveau still has the old job.
    set_sql = ", ".join(f"{col} = %s" for col, _ in updates)
    params = [v for _, v in updates]

    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE product.tenant_scheduled_runs
                   SET {set_sql},
                       status = 'pending_activation',
                       status_detail = NULL,
                       updated_at = now()
                 WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                RETURNING {_LIST_COLUMNS}
                """,
                (*params, run_id, user["user_id"]),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Scheduled run not found")
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"scheduled-run update failed for {user['user_id']}/{run_id}: {e}")
        raise HTTPException(status_code=503, detail="Scheduled-run store unavailable")
    finally:
        conn.close()
    return {"scheduled_run": _row_to_dict(row)}


@router.delete("/{run_id}")
def delete_scheduled_run(run_id: str, user: dict = Depends(get_current_user_payload)):
    """Soft-delete. The row stays so the sync can tell "remove this from
    Cerveau" apart from "a row it has never seen"; the unique-name index is
    partial on `deleted_at IS NULL`, so the name frees up immediately."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE product.tenant_scheduled_runs"
                "   SET deleted_at = now(), enabled = false, updated_at = now()"
                " WHERE id = %s AND user_id = %s AND deleted_at IS NULL"
                " RETURNING id",
                (run_id, user["user_id"]),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Scheduled run not found")
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"scheduled-run delete failed for {user['user_id']}/{run_id}: {e}")
        raise HTTPException(status_code=503, detail="Scheduled-run store unavailable")
    finally:
        conn.close()
    return {"status": "deleted", "id": run_id}


# ── Internal (Cerveau-facing) ────────────────────────────────────────────


_INTERNAL_COLUMNS = (
    "id, user_id, agent_type, name, prompt, cron_expression, timezone, enabled, status"
)


def _internal_row_to_dict(row) -> dict:
    row_id, user_id, agent_type, name, prompt, cron_expression, tz, enabled, status = row
    return {
        "id": str(row_id),
        "user_id": user_id,
        "agent_type": agent_type,
        "name": name,
        "prompt": prompt,
        "cron_expression": cron_expression,
        "timezone": tz,
        "enabled": enabled,
        "status": status,
    }


@router.get("/internal/all", dependencies=[Depends(require_internal_token)])
def internal_list_all_schedules():
    """Every live schedule across every tenant.

    A scheduler cannot work per-request: it has to know the whole set ahead
    of any turn. This is the read the (not yet built) Cerveau-side reconcile
    pass needs — deliberately the full set including `enabled = false`, so
    the sync can tell "pause this" apart from "this no longer exists".
    """
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_INTERNAL_COLUMNS} FROM product.tenant_scheduled_runs"
                " WHERE deleted_at IS NULL ORDER BY user_id, agent_type, created_at"
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.error(f"internal scheduled-run list failed: {e}")
        raise HTTPException(status_code=503, detail="Scheduled-run store unavailable")
    finally:
        conn.close()
    return {"scheduled_runs": [_internal_row_to_dict(r) for r in rows]}


@router.get("/internal/{user_id}/{agent_type}", dependencies=[Depends(require_internal_token)])
def internal_list_tenant_schedules(user_id: str, agent_type: str):
    _check_agent_type(agent_type)
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_INTERNAL_COLUMNS} FROM product.tenant_scheduled_runs"
                " WHERE user_id = %s AND agent_type = %s AND deleted_at IS NULL"
                " ORDER BY created_at",
                (user_id, agent_type),
            )
            rows = cur.fetchall()
    except Exception as e:
        logger.error(f"internal scheduled-run lookup failed for {user_id}/{agent_type}: {e}")
        raise HTTPException(status_code=503, detail="Scheduled-run store unavailable")
    finally:
        conn.close()
    return {"scheduled_runs": [_internal_row_to_dict(r) for r in rows]}


class SyncAckRequest(BaseModel):
    """What Cerveau reports back after taking ownership of a row."""

    status: str
    cerveau_job_id: Optional[str] = None
    status_detail: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in ("active", "paused", "failed"):
            raise ValueError("status must be one of: active, paused, failed")
        return v


@router.post("/internal/{run_id}/ack", dependencies=[Depends(require_internal_token)])
def internal_ack_sync(run_id: str, body: SyncAckRequest):
    """Cerveau's side of the handshake: "I now own this row, here is its id".

    Without this the store could only ever say `pending_activation`, and a
    tenant would have no way to tell a schedule that is really running from
    one the scheduler never picked up.
    """
    conn = _connect()
    try:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE product.tenant_scheduled_runs"
                "   SET status = %s, cerveau_job_id = %s, status_detail = %s,"
                "       last_synced_at = now(), updated_at = now()"
                " WHERE id = %s AND deleted_at IS NULL"
                " RETURNING id",
                (body.status, body.cerveau_job_id, body.status_detail, run_id),
            )
            if cur.fetchone() is None:
                raise HTTPException(status_code=404, detail="Scheduled run not found")
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"scheduled-run sync ack failed for {run_id}: {e}")
        raise HTTPException(status_code=503, detail="Scheduled-run store unavailable")
    finally:
        conn.close()
    return {"status": "ok", "id": run_id}
