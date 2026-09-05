"""
Discord deployable-agent service.

Flow:
  1. Dashboard calls POST /api/v1/discord/deploy-link (JWT auth) -> one-time
     connect code (e.g. "K7H2-9QXR") + the bot's invite URL.
  2. User invites the shared "Aivory Agent" bot to their own Discord server,
     then types `/connect <code>` in whichever channel they want the agent in.
  3. The Discord Gateway listener (vps-bridge/discord-listener.js — a
     persistent process, since Discord pushes message content over a
     WebSocket, not an inbound webhook the way Telegram does) receives that
     slash-command interaction and calls our internal /api/v1/discord/redeem
     endpoint, which redeems the code here and binds the channel.
  4. Subsequent messages in that channel are POSTed by the listener to our
     internal /api/v1/discord/message endpoint; this service resolves the
     binding, gates on credit/tier/active-subscription exactly like Telegram,
     and forwards to the same channel-agnostic agent gateway
     (vps-bridge's /telegram/message — the name is legacy, the handler is
     generic over `channel`). The listener sends the returned reply back to
     Discord.

One shared bot for every agent type (unlike Telegram's per-agent-type bot
option) — deliberate product choice, see the 2026-08-18 planning
conversation. Bindings are keyed by (guild_id, channel_id) so the same
private/community Discord server can host the agent in one specific
channel without the bot responding everywhere it's been invited.

Storage follows the same MVP file-based convention (DatabaseService JSON
collections) Telegram already uses in production:
    discord_link_tokens/{code}.json
    discord_bindings/{guild_id}_{channel_id}.json

No native Discord message-component (button) support yet: a pending Cerveau
approval is surfaced as plain text asking the user to reply "approve" or
"deny" — see _handle_pending_reply(). Real buttons need a signature-verified
HTTP Interactions endpoint (Ed25519, the app's Public Key) in addition to the
Gateway connection; deferred as a follow-up, not required for v1.
"""

import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

import requests

from app.config import settings
from app.services import tiers
from app.services.telegram_service import (
    AGENT_TYPES,
    agent_tier_error,
    is_superadmin,
    load_user_record,
)

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"

LINK_TOKENS_COLLECTION = "discord_link_tokens"
BINDINGS_COLLECTION = "discord_bindings"

FALLBACK_REPLY = (
    "🤖 Your Aivory agent received the message. "
    "Live responses will appear here once the agent runtime is attached."
)

WELCOME_TEMPLATE = (
    "✅ {agent_name} is connected to this channel.\n\n"
    "Just type what you need — no commands, no menus. I'm ready when you are."
)

PENDING_APPROVAL_REPLY = (
    "⚠️ There's a pending approval on this agent — reply **approve** or **deny** "
    "before sending a new message."
)

# Human-typed via `/connect <code>` — short, unambiguous alphabet (no 0/O, 1/I/L).
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")


def is_valid_code_format(code: str) -> bool:
    return bool(_CODE_RE.match((code or "").upper()))


def get_bot() -> Optional[dict]:
    """Resolve the single shared Discord bot's credentials.

    Unlike Telegram's per-agent-type bot option, every agent type shares one
    bot identity ("Aivory Agent") — simpler product surface, and Discord's
    per-guild install model means the *server* the tenant invites the bot
    into is what actually scopes it to their business, not a distinct bot
    per agent type.
    """
    token = settings.discord_bot_token
    app_id = settings.discord_application_id
    if not token or not app_id:
        return None
    return {"token": token, "application_id": app_id}


def build_invite_url(bot: dict) -> str:
    """OAuth2 install link: `bot` scope + minimal permissions (View Channels,
    Send Messages, Read Message History = permission integer 68608)."""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={bot['application_id']}&scope=bot&permissions=68608"
    )


class DiscordService:
    """Connect codes, channel bindings, and the internal message-routing path."""

    def __init__(self, db_service):
        self.db = db_service

    # ========================================================================
    # CONNECT CODES (one-time, expiring, human-typed)
    # ========================================================================

    def create_link_token(self, user_id: str, agent_type: str) -> dict:
        if agent_type not in AGENT_TYPES:
            raise ValueError(f"Unknown agent_type '{agent_type}'")
        tier_err = agent_tier_error(self._load_user(user_id), agent_type)
        if tier_err:
            raise PermissionError(tier_err)
        bot = get_bot()
        if not bot:
            raise RuntimeError("Discord integration is not configured")

        code = "-".join(
            "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(2)
        )
        now = datetime.utcnow()
        record = {
            "code": code,
            "user_id": user_id,
            "agent_type": agent_type,
            "created_at": now.isoformat(),
            "expires_at": (
                now + timedelta(minutes=settings.discord_link_token_ttl_minutes)
            ).isoformat(),
            "used": False,
            "used_at": None,
            "guild_id": None,
            "channel_id": None,
        }
        self.db.save_json(LINK_TOKENS_COLLECTION, code, record)

        return {
            "code": code,
            "invite_url": build_invite_url(bot),
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES[agent_type],
            "expires_at": record["expires_at"],
        }

    def get_link_status(self, code: str, user_id: str) -> dict:
        if not is_valid_code_format(code):
            return {"status": "not_found"}
        record = self.db.load_json(LINK_TOKENS_COLLECTION, code.upper())
        if not record or record.get("user_id") != user_id:
            return {"status": "not_found"}
        if record.get("used"):
            return {"status": "connected", "channel_id": record.get("channel_id")}
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            return {"status": "expired"}
        return {"status": "pending"}

    # ========================================================================
    # USER LOOKUP
    # ========================================================================

    def _load_user(self, user_id: str) -> Optional[dict]:
        return load_user_record(self.db, user_id)

    @staticmethod
    def _is_active(user: Optional[dict]) -> bool:
        if not user:
            return False
        if user.get("is_active") is False:
            return False
        return user.get("status") != "suspended"

    # ========================================================================
    # BINDINGS — keyed by (guild_id, channel_id)
    # ========================================================================

    @staticmethod
    def _binding_id(guild_id: str, channel_id: str) -> str:
        return f"{guild_id}_{channel_id}"

    def get_binding(self, guild_id: str, channel_id: str) -> Optional[dict]:
        return self.db.load_json(BINDINGS_COLLECTION, self._binding_id(guild_id, channel_id))

    def list_bindings(self, user_id: str) -> list:
        all_bindings = self.db.load_all_json(BINDINGS_COLLECTION) or []
        return [b for b in all_bindings if b.get("user_id") == user_id]

    def delete_binding(self, binding_id: str, user_id: str) -> Optional[dict]:
        if not re.match(r"^\d+_\d+$", binding_id or ""):
            return None
        binding = self.db.load_json(BINDINGS_COLLECTION, binding_id)
        if not binding or binding.get("user_id") != user_id:
            return None
        self.db.delete_json(BINDINGS_COLLECTION, binding_id)
        return binding

    # ========================================================================
    # REDEEM — called by discord-listener.js on a `/connect <code>` interaction
    # ========================================================================

    def redeem_code(
        self, code: str, guild_id: str, channel_id: str, discord_user_id: str,
        channel_name: Optional[str] = None,
    ) -> dict:
        """Returns {"ok": bool, "reply": str} — `reply` is the ephemeral
        message the listener sends back as the interaction response."""
        if not is_valid_code_format(code):
            return {"ok": False, "reply": "⚠️ That code doesn't look right. Double-check it from your dashboard."}

        norm_code = code.upper()
        record = self.db.load_json(LINK_TOKENS_COLLECTION, norm_code)
        if not record or record.get("used"):
            return {"ok": False, "reply": "⚠️ This connect code is invalid or already used. Generate a new one from your dashboard."}
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            return {"ok": False, "reply": "⚠️ This connect code has expired. Generate a new one from your dashboard."}

        user = self._load_user(record["user_id"])
        if not self._is_active(user):
            return {"ok": False, "reply": "⚠️ This Aivory account is not active. Please check your subscription."}

        agent_type = record["agent_type"]
        tier_err = agent_tier_error(user, agent_type)
        if tier_err:
            return {"ok": False, "reply": f"⚠️ {tier_err}"}

        binding = {
            "binding_id": self._binding_id(guild_id, channel_id),
            "guild_id": guild_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "discord_user_id": discord_user_id,
            "user_id": record["user_id"],
            "account_type": user.get("account_type", "free"),
            "tier": "enterprise" if is_superadmin(user) else tiers.account_tier(user.get("tier")),
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES.get(agent_type, agent_type),
            "status": "active",
            "linked_code": norm_code,
            "created_at": datetime.utcnow().isoformat(),
            "pending_approval_id": None,
        }
        self.db.save_json(BINDINGS_COLLECTION, binding["binding_id"], binding)

        record["used"] = True
        record["used_at"] = datetime.utcnow().isoformat()
        record["guild_id"] = guild_id
        record["channel_id"] = channel_id
        self.db.save_json(LINK_TOKENS_COLLECTION, norm_code, record)

        logger.info(
            f"Bound Discord channel {channel_id} (guild {guild_id}) "
            f"to user {record['user_id']} ({agent_type})"
        )
        return {"ok": True, "reply": WELCOME_TEMPLATE.format(agent_name=binding["agent_name"])}

    # ========================================================================
    # MESSAGE HANDLING — called by discord-listener.js on every channel message
    # ========================================================================

    def handle_message(
        self, guild_id: str, channel_id: str, text: str, attachments: Optional[list] = None,
    ) -> Optional[str]:
        """Returns the reply text to send, or None if nothing should be sent
        (unbound channel — keeps the shared bot quiet outside connected
        channels)."""
        binding = self.get_binding(guild_id, channel_id)
        if not binding:
            return None

        user = self._load_user(binding["user_id"])
        if not self._is_active(user):
            self.db.delete_json(BINDINGS_COLLECTION, binding["binding_id"])
            return "⚠️ This agent was disconnected because the Aivory subscription is no longer active."

        if binding.get("pending_approval_id"):
            return self._handle_pending_reply(binding, text)

        prompt = self._build_prompt(text, attachments)
        if not prompt:
            return None
        result = self._route_to_agent(binding, prompt)

        pending = result.get("pending_approval")
        if pending and isinstance(pending, dict) and pending.get("id"):
            binding["pending_approval_id"] = pending["id"]
            self.db.save_json(BINDINGS_COLLECTION, binding["binding_id"], binding)
            return f"{result['reply']}\n\n{PENDING_APPROVAL_REPLY}"

        return result["reply"]

    def _handle_pending_reply(self, binding: dict, text: str) -> str:
        decision = text.strip().lower()
        if decision not in ("approve", "deny"):
            return PENDING_APPROVAL_REPLY
        result = self._resolve_approval(binding, binding["pending_approval_id"], decision)
        binding["pending_approval_id"] = None
        self.db.save_json(BINDINGS_COLLECTION, binding["binding_id"], binding)
        return result["reply"]

    def _resolve_approval(self, binding: dict, pending_id: str, decision: str) -> dict:
        if not settings.telegram_agent_gateway_url:
            return {"reply": FALLBACK_REPLY}
        headers = {}
        gateway_token = os.getenv("TELEGRAM_GATEWAY_TOKEN")
        if gateway_token:
            headers["X-Internal-Token"] = gateway_token
        try:
            resp = requests.post(
                f"{settings.telegram_agent_gateway_url.rstrip('/')}/telegram/approval-decision",
                headers=headers,
                json={
                    "user_id": binding["user_id"],
                    "agent_type": binding["agent_type"],
                    "session_id": binding["binding_id"],
                    "pending_id": pending_id,
                    "decision": decision,
                },
                timeout=195,
            )
            if resp.ok:
                data = resp.json()
                return {"reply": (data.get("reply_text") or "Done.")[:2000]}
            if resp.status_code == 404:
                return {"reply": "⚠️ This approval request could no longer be found — it may have already expired or been resolved."}
            logger.error(f"Approval-decision gateway returned {resp.status_code}: {resp.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Approval-decision gateway error: {e}")
        return {"reply": "⚠️ Couldn't process that right now. Please try again in a moment."}

    def _build_prompt(self, text: str, attachments: Optional[list]) -> str:
        """Discord attachments arrive from the listener as already-fetched
        {filename, content_type, url} dicts (the listener downloads the file
        itself, same shape Telegram's caption+attachment flow ends up
        producing) — reuse the same extractor Telegram uses."""
        from app.services import attachment_extractor as ax

        caption = (text or "").strip()
        parts = []
        for att in attachments or []:
            url = att.get("url")
            if not url:
                continue
            try:
                data = requests.get(url, timeout=15).content
            except requests.RequestException:
                continue
            name = att.get("filename") or "attachment"
            mime = att.get("content_type")
            if ax.is_image(name, mime):
                content = ax.describe_image(data, mime or "image/jpeg", caption)
                parts.append({"filename": name, "content": content, "kind": "image"})
            else:
                content = ax.extract_document_text(name, data, mime)
                if content is None:
                    content = f"[Received an unsupported file type: {name}]"
                parts.append({"filename": name, "content": content, "kind": "document"})
        return ax.compose_prompt(caption, parts)

    def _route_to_agent(self, binding: dict, text: str) -> dict:
        if not settings.telegram_agent_gateway_url:
            return {"reply": FALLBACK_REPLY, "pending_approval": None}
        headers = {}
        gateway_token = os.getenv("TELEGRAM_GATEWAY_TOKEN")
        if gateway_token:
            headers["X-Internal-Token"] = gateway_token
        try:
            resp = requests.post(
                f"{settings.telegram_agent_gateway_url.rstrip('/')}/telegram/message",
                headers=headers,
                json={
                    "user_id": binding["user_id"],
                    "agent_type": binding["agent_type"],
                    "account_type": binding.get("account_type", "free"),
                    "chat_id": binding["binding_id"],
                    "session_id": binding["binding_id"],
                    "text": text,
                    "channel": "discord",
                },
                timeout=195,
            )
            if resp.ok:
                data = resp.json()
                return {
                    "reply": (data.get("reply") or FALLBACK_REPLY)[:2000],
                    "pending_approval": data.get("pending_approval"),
                }
            logger.error(f"Agent gateway returned {resp.status_code}: {resp.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Agent gateway error: {e}")
        return {
            "reply": "⚠️ The agent is temporarily unavailable. Please try again in a moment.",
            "pending_approval": None,
        }
