"""
Slack deployable-agent service.

Flow (mirrors the Telegram QR flow, with OAuth instead of a deep link):
  1. Dashboard calls POST /api/v1/slack/deploy-link (JWT auth) -> Slack OAuth
     install URL carrying a one-time `state` token
  2. User approves the install -> Slack redirects to our OAuth callback with
     `code` + `state`
  3. Callback exchanges the code for a workspace bot token and binds
     team_id -> {user_id, agent_type}
  4. DMs to the bot and @mentions in channels are routed to the agent backend

Storage (DatabaseService JSON collections):
    slack_link_tokens/{token}.json      one-time install tokens
    slack_installations/{team_id}.json  workspace installs (bot token + binding)
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from app.config import settings
from app.services.telegram_service import AGENT_TYPES, is_valid_token_format

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"

LINK_TOKENS_COLLECTION = "slack_link_tokens"
INSTALLATIONS_COLLECTION = "slack_installations"

# Bot scopes: receive DMs + mentions, reply in both, download shared files
OAUTH_SCOPES = "app_mentions:read,chat:write,im:history,files:read"

FALLBACK_REPLY = (
    "🤖 Your Aivory agent received the message. "
    "Live responses will appear here once the agent runtime is attached."
)

WELCOME_TEMPLATE = (
    "✅ {agent_name} is connected to this workspace.\n"
    "DM me or @mention me in a channel — no commands, no setup. "
    "I'm ready when you are."
)

# Slack retries events aggressively; remember recently seen event ids
_SEEN_EVENTS: dict = {}
_SEEN_EVENTS_TTL = 600
_SEEN_EVENTS_MAX = 5000


class SlackService:
    """Install tokens, workspace installations, Events API handling."""

    def __init__(self, db_service):
        self.db = db_service

    # ========================================================================
    # SLACK WEB API
    # ========================================================================

    def post_message(self, bot_token: str, channel: str, text: str) -> bool:
        try:
            resp = requests.post(
                f"{SLACK_API_BASE}/chat.postMessage",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={"channel": channel, "text": text},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error(f"chat.postMessage failed: {data.get('error')}")
            return bool(data.get("ok"))
        except (requests.RequestException, ValueError) as e:
            logger.error(f"chat.postMessage error: {e}")
            return False

    # ========================================================================
    # INSTALL LINKS (one-time, expiring — the OAuth `state` param)
    # ========================================================================

    def create_install_link(self, user_id: str, agent_type: str) -> dict:
        if agent_type not in AGENT_TYPES:
            raise ValueError(f"Unknown agent_type '{agent_type}'")
        if not settings.slack_client_id:
            raise RuntimeError("Slack app is not configured")

        token = secrets.token_urlsafe(24)
        now = datetime.utcnow()
        record = {
            "token": token,
            "user_id": user_id,
            "agent_type": agent_type,
            "created_at": now.isoformat(),
            "expires_at": (
                now + timedelta(minutes=settings.telegram_link_token_ttl_minutes)
            ).isoformat(),
            "used": False,
            "used_at": None,
            "team_id": None,
        }
        self.db.save_json(LINK_TOKENS_COLLECTION, token, record)

        install_url = (
            "https://slack.com/oauth/v2/authorize"
            f"?client_id={settings.slack_client_id}"
            f"&scope={OAUTH_SCOPES}"
            f"&state={token}"
        )
        return {
            "token": token,
            "install_url": install_url,
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES[agent_type],
            "expires_at": record["expires_at"],
        }

    def get_link_status(self, token: str, user_id: str) -> dict:
        """pending | connected | expired | not_found — same shape as Telegram."""
        if not is_valid_token_format(token):
            return {"status": "not_found"}
        record = self.db.load_json(LINK_TOKENS_COLLECTION, token)
        if not record or record.get("user_id") != user_id:
            return {"status": "not_found"}
        if record.get("used"):
            return {"status": "connected", "team_id": record.get("team_id")}
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            return {"status": "expired"}
        return {"status": "pending"}

    # ========================================================================
    # OAUTH CALLBACK
    # ========================================================================

    def complete_install(self, code: str, state: str) -> dict:
        """Exchange the OAuth code and bind the workspace to the link token's
        user + agent. Returns the installation record. Raises ValueError with a
        user-facing message on any failure."""
        if not is_valid_token_format(state):
            raise ValueError("Invalid install link")
        record = self.db.load_json(LINK_TOKENS_COLLECTION, state)
        if not record or record.get("used"):
            raise ValueError("This install link is invalid or already used")
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            raise ValueError("This install link has expired")

        try:
            resp = requests.post(
                f"{SLACK_API_BASE}/oauth.v2.access",
                data={
                    "client_id": settings.slack_client_id,
                    "client_secret": settings.slack_client_secret,
                    "code": code,
                },
                timeout=15,
            )
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"oauth.v2.access error: {e}")
            raise ValueError("Could not reach Slack — please try again")

        if not data.get("ok"):
            logger.error(f"oauth.v2.access failed: {data.get('error')}")
            raise ValueError("Slack did not authorize the install")

        team = data.get("team") or {}
        agent_type = record["agent_type"]
        installation = {
            "team_id": team.get("id"),
            "team_name": team.get("name"),
            "bot_token": data.get("access_token"),
            "bot_user_id": data.get("bot_user_id"),
            "user_id": record["user_id"],
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES.get(agent_type, agent_type),
            "status": "active",
            "linked_token": state,
            "created_at": datetime.utcnow().isoformat(),
        }
        if not installation["team_id"] or not installation["bot_token"]:
            raise ValueError("Slack returned an incomplete install")

        self.db.save_json(INSTALLATIONS_COLLECTION, installation["team_id"], installation)

        record["used"] = True
        record["used_at"] = datetime.utcnow().isoformat()
        record["team_id"] = installation["team_id"]
        self.db.save_json(LINK_TOKENS_COLLECTION, state, record)

        logger.info(
            f"Slack workspace {installation['team_id']} ({installation['team_name']}) "
            f"bound to user {record['user_id']} ({agent_type})"
        )
        return installation

    # ========================================================================
    # INSTALLATIONS
    # ========================================================================

    def get_installation(self, team_id: str) -> Optional[dict]:
        if not team_id or not team_id.replace("-", "").isalnum():
            return None
        return self.db.load_json(INSTALLATIONS_COLLECTION, team_id)

    def list_installations(self, user_id: str) -> list:
        items = self.db.load_all_json(INSTALLATIONS_COLLECTION) or []
        out = []
        for i in items:
            if i.get("user_id") != user_id:
                continue
            safe = dict(i)
            safe.pop("bot_token", None)  # never expose workspace tokens
            out.append(safe)
        return out

    def delete_installation(self, team_id: str, user_id: str) -> bool:
        inst = self.get_installation(team_id)
        if not inst or inst.get("user_id") != user_id:
            return False
        return self.db.delete_json(INSTALLATIONS_COLLECTION, team_id)

    # ========================================================================
    # EVENTS API
    # ========================================================================

    @staticmethod
    def verify_signature(timestamp: str, signature: str, raw_body: bytes) -> bool:
        """HMAC check per Slack docs; also rejects replayed timestamps."""
        secret = settings.slack_signing_secret
        if not secret or not timestamp or not signature:
            return False
        try:
            if abs(time.time() - float(timestamp)) > 300:
                return False
        except ValueError:
            return False
        base = f"v0:{timestamp}:".encode() + raw_body
        expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def _already_seen(event_id: Optional[str]) -> bool:
        if not event_id:
            return False
        now = time.time()
        if len(_SEEN_EVENTS) > _SEEN_EVENTS_MAX:
            _SEEN_EVENTS.clear()
        for k in [k for k, v in _SEEN_EVENTS.items() if now - v > _SEEN_EVENTS_TTL]:
            _SEEN_EVENTS.pop(k, None)
        if event_id in _SEEN_EVENTS:
            return True
        _SEEN_EVENTS[event_id] = now
        return False

    def process_event(self, payload: dict) -> None:
        """Handle one Events API callback (already ACKed to Slack)."""
        if self._already_seen(payload.get("event_id")):
            return

        event = payload.get("event") or {}
        etype = event.get("type")
        if etype not in ("message", "app_mention"):
            return
        # Ignore bot messages (including our own replies) and message edits.
        # Allow "file_share" through — that's how a DM'd document/image arrives.
        subtype = event.get("subtype")
        if event.get("bot_id") or (subtype and subtype != "file_share"):
            return
        # Plain channel messages arrive as type "message" too — only respond
        # to DMs there; channels require an @mention
        if etype == "message" and event.get("channel_type") != "im":
            return

        team_id = payload.get("team_id")
        installation = self.get_installation(team_id)
        if not installation:
            return
        if event.get("user") and event["user"] == installation.get("bot_user_id"):
            return

        text = (event.get("text") or "").strip()
        channel = event.get("channel")
        files = event.get("files") or []
        if (not text and not files) or not channel:
            return
        # Strip the leading @mention so the agent sees a clean prompt
        bot_user = installation.get("bot_user_id")
        if bot_user:
            text = text.replace(f"<@{bot_user}>", "").strip()

        # Subscription guard — same semantics as Telegram
        from app.services.telegram_service import TelegramService

        tg = TelegramService(self.db)
        user = tg._load_user(installation["user_id"])
        if not tg._is_active(user):
            self.post_message(
                installation["bot_token"], channel,
                "⚠️ This agent was disconnected because the Aivory subscription is no longer active.",
            )
            self.db.delete_json(INSTALLATIONS_COLLECTION, team_id)
            return

        prompt = self._build_prompt(installation["bot_token"], text, files)
        if not prompt:
            return
        reply = self._route_to_agent(installation, channel, prompt)
        self.post_message(installation["bot_token"], channel, reply)

    def _download_slack_file(self, bot_token: str, url_private: str) -> Optional[bytes]:
        """Download a Slack file via its private URL using the bot token."""
        try:
            resp = requests.get(
                url_private,
                headers={"Authorization": f"Bearer {bot_token}"},
                timeout=30,
            )
            # Slack returns an HTML login page (200) instead of 401 when the
            # token lacks files:read — guard against storing that as content
            ctype = resp.headers.get("content-type", "")
            if resp.ok and "text/html" not in ctype:
                return resp.content
            logger.error(f"Slack file download rejected (content-type={ctype})")
        except requests.RequestException as e:
            logger.error(f"Slack file download failed: {e}")
        return None

    def _build_prompt(self, bot_token: str, text: str, files: list) -> str:
        from app.services import attachment_extractor as ax

        attachments = []
        for f in files[:5]:  # cap per message
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            data = self._download_slack_file(bot_token, url)
            if not data:
                continue
            name = f.get("name") or "attachment"
            mime = f.get("mimetype")
            if ax.is_image(name, mime):
                content = ax.describe_image(data, mime or "image/jpeg", text)
                attachments.append({"filename": name, "content": content, "kind": "image"})
            else:
                content = ax.extract_document_text(name, data, mime)
                if content is None:
                    content = f"[Received an unsupported file type: {name}]"
                attachments.append({"filename": name, "content": content, "kind": "document"})

        return ax.compose_prompt(text, attachments)

    def _route_to_agent(self, installation: dict, channel: str, text: str) -> str:
        if not settings.telegram_agent_gateway_url:
            return FALLBACK_REPLY
        headers = {}
        gateway_token = os.getenv("TELEGRAM_GATEWAY_TOKEN")
        if gateway_token:
            headers["X-Internal-Token"] = gateway_token
        try:
            resp = requests.post(
                f"{settings.telegram_agent_gateway_url.rstrip('/')}/telegram/message",
                headers=headers,
                json={
                    "user_id": installation["user_id"],
                    "agent_type": installation["agent_type"],
                    "chat_id": channel,
                    # unique per (workspace, channel) so histories never merge
                    "session_id": f"slack_{installation['team_id']}_{channel}",
                    "text": text,
                },
                timeout=60,
            )
            if resp.ok:
                return (resp.json().get("reply") or FALLBACK_REPLY)[:39000]
            logger.error(f"Agent gateway returned {resp.status_code}: {resp.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Agent gateway error: {e}")
        return "⚠️ The agent is temporarily unavailable. Please try again in a moment."
