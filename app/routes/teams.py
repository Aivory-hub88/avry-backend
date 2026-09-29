"""
Teams API (ADR-020 §3.3) — JWT, tenant-scoped: a user only ever sees and edits
their own Teams. See app/services/teams.py for the model.

    GET    /api/v1/teams
    POST   /api/v1/teams                       {name, agent_types, isolated?}
    PUT    /api/v1/teams/{id}                  {name?, agent_types?, isolated?}
    DELETE /api/v1/teams/{id}
    POST   /api/v1/teams/{id}/channels         {kind, ref}
    DELETE /api/v1/teams/{id}/channels/{kind}/{ref}
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.database.db_service import DatabaseService
from app.routes.agent_actions import get_current_user_payload
from app.routes.agent_roster import AGENT_ROSTER
from app.services import teams, workspace_acting
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])

db_service = DatabaseService()
telegram_service = TelegramService(db_service)

AGENT_TYPES = {a["agent_type"] for a in AGENT_ROSTER}


class TeamBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    agent_types: List[str] = Field(min_length=1, max_length=teams.MAX_MEMBERS)
    isolated: bool = True


class TeamPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    agent_types: Optional[List[str]] = Field(default=None, min_length=1, max_length=teams.MAX_MEMBERS)
    isolated: Optional[bool] = None


class ChannelBody(BaseModel):
    kind: str
    ref: str = Field(min_length=1, max_length=128)


def _uid(user: dict) -> str:
    return str(user["user_id"])


def _clean_agents(agent_types: List[str]) -> List[str]:
    out: List[str] = []
    for a in agent_types:
        if a not in AGENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown agent_type '{a}'")
        if a not in out:
            out.append(a)
    return out


def _owns_channel(user_id: str, kind: str, ref: str) -> bool:
    """A user may only bind channels that are theirs."""
    if kind == "workspace":
        try:
            return workspace_acting.fetch_space_facts(ref, user_id)["doc_owner"] == user_id
        except Exception as e:  # fail closed
            logger.warning(f"teams: workspace ownership lookup failed: {e}")
            return False
    if kind == "telegram":
        return any(b.get("binding_id") == ref for b in telegram_service.list_bindings(user_id))
    return False


def _db_error(e: Exception) -> HTTPException:
    logger.error(f"teams store failed: {e}")
    return HTTPException(status_code=503, detail="Teams store unavailable")


@router.get("")
def list_teams(user: dict = Depends(get_current_user_payload)):
    try:
        return {"teams": teams.list_teams(_uid(user))}
    except Exception as e:
        raise _db_error(e)


@router.post("", status_code=201)
def create_team(body: TeamBody, user: dict = Depends(get_current_user_payload)):
    agents = _clean_agents(body.agent_types)
    try:
        return teams.create_team(_uid(user), body.name.strip(), agents, body.isolated)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise _db_error(e)


@router.put("/{team_id}")
def update_team(team_id: str, body: TeamPatch, user: dict = Depends(get_current_user_payload)):
    agents = _clean_agents(body.agent_types) if body.agent_types is not None else None
    try:
        team = teams.update_team(
            _uid(user),
            team_id,
            name=body.name.strip() if body.name is not None else None,
            agent_types=agents,
            isolated=body.isolated,
        )
    except Exception as e:
        raise _db_error(e)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


@router.delete("/{team_id}")
def delete_team(team_id: str, user: dict = Depends(get_current_user_payload)):
    try:
        ok = teams.delete_team(_uid(user), team_id)
    except Exception as e:
        raise _db_error(e)
    if not ok:
        raise HTTPException(status_code=404, detail="Team not found")
    return {"success": True}


@router.post("/{team_id}/channels")
def attach_channel(team_id: str, body: ChannelBody, user: dict = Depends(get_current_user_payload)):
    if body.kind not in teams.CHANNEL_KINDS:
        raise HTTPException(
            status_code=400, detail=f"kind must be one of: {', '.join(teams.CHANNEL_KINDS)}"
        )
    uid = _uid(user)
    if not _owns_channel(uid, body.kind, body.ref):
        raise HTTPException(status_code=403, detail="That channel is not yours to attach")
    try:
        team = teams.attach_channel(uid, team_id, body.kind, body.ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise _db_error(e)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


@router.delete("/{team_id}/channels/{kind}/{ref:path}")
def detach_channel(team_id: str, kind: str, ref: str, user: dict = Depends(get_current_user_payload)):
    try:
        team = teams.detach_channel(_uid(user), team_id, kind, ref)
    except Exception as e:
        raise _db_error(e)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team
