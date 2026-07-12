"""
Slack deployable-agent API endpoints.

Dashboard-facing (JWT auth):
    POST   /api/v1/slack/deploy-link         -> one-time OAuth install URL
    GET    /api/v1/slack/link-status/{tok}   -> pending|connected|expired
    GET    /api/v1/slack/installations       -> list connected workspaces
    DELETE /api/v1/slack/installations/{id}  -> disconnect a workspace

Slack-facing:
    GET    /api/v1/slack/oauth/callback      -> OAuth code exchange (state = link token)
    POST   /api/v1/slack/events              -> Events API (signature-verified)
"""

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Header, Depends, Request
from fastapi.responses import RedirectResponse, PlainTextResponse
from pydantic import BaseModel

from app.config import settings
from app.database.db_service import DatabaseService
from app.services.auth_service import AuthService
from app.services.slack_service import SlackService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/slack", tags=["slack"])

db_service = DatabaseService()
auth_service = AuthService(db_service)
slack_service = SlackService(db_service)


class DeployLinkRequest(BaseModel):
    agent_type: str


def get_current_user_payload(authorization: Optional[str] = Header(None)) -> dict:
    """Require a valid Bearer access token; return its JWT payload."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    payload = auth_service.verify_token(parts[1])
    if not payload or not payload.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


@router.post("/deploy-link")
def create_deploy_link(
    body: DeployLinkRequest, user: dict = Depends(get_current_user_payload)
):
    """Generate a one-time Slack OAuth install URL."""
    try:
        return slack_service.create_install_link(
            user_id=user["user_id"], agent_type=body.agent_type
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except RuntimeError as e:
        logger.error(f"Slack not configured: {e}")
        raise HTTPException(status_code=503, detail="Slack integration is not configured")


@router.get("/link-status/{token}")
def link_status(token: str, user: dict = Depends(get_current_user_payload)):
    """Dashboard polls this after opening the install URL."""
    return slack_service.get_link_status(token, user["user_id"])


@router.get("/installations")
def list_installations(user: dict = Depends(get_current_user_payload)):
    return {"installations": slack_service.list_installations(user["user_id"])}


@router.delete("/installations/{team_id}")
def delete_installation(team_id: str, user: dict = Depends(get_current_user_payload)):
    if not slack_service.delete_installation(team_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="Installation not found")
    return {"success": True}


@router.get("/oauth/callback")
def oauth_callback(code: Optional[str] = None, state: Optional[str] = None,
                   error: Optional[str] = None):
    """Slack redirects here after the user approves (or cancels) the install."""
    if error or not code or not state:
        # User cancelled on Slack's consent screen
        return RedirectResponse(url=f"{settings.slack_post_install_redirect.split('?')[0]}?slack=cancelled")
    try:
        installation = slack_service.complete_install(code, state)
    except ValueError as e:
        logger.warning(f"Slack install failed: {e}")
        return PlainTextResponse(f"Slack install failed: {e}", status_code=400)

    # Greet the workspace so the install feels alive (best-effort: needs a
    # channel — DM the installer is not available with bot-only scopes, so we
    # skip greeting here; the dashboard shows Connected instead.)
    logger.info(f"Slack install completed for team {installation['team_id']}")
    return RedirectResponse(url=settings.slack_post_install_redirect)


@router.post("/events")
async def slack_events(
    request: Request,
    background_tasks: BackgroundTasks,
    x_slack_request_timestamp: Optional[str] = Header(None),
    x_slack_signature: Optional[str] = Header(None),
):
    """Events API endpoint. Must ACK within 3s — processing happens after
    the response via BackgroundTasks."""
    raw = await request.body()
    if not slack_service.verify_signature(
        x_slack_request_timestamp, x_slack_signature, raw
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        payload = await request.json()
    except Exception:
        return {"ok": True}

    # One-time URL verification handshake when saving the events URL in Slack
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}

    background_tasks.add_task(slack_service.process_event, payload)
    return {"ok": True}
