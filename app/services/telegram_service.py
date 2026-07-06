"""
Telegram deployable-agent service.

Flow:
  1. Dashboard calls POST /api/v1/telegram/deploy-link (JWT auth) -> one-time token
     rendered as a QR of https://t.me/<bot>?start=<token>
  2. User scans, taps Start -> Telegram delivers "/start <token>" to our webhook
  3. Token is redeemed: the chat_id is bound to {user_id, agent_type}
  4. Subsequent messages in that chat are routed to the agent backend

Storage follows the MVP file-based convention (DatabaseService JSON collections):
    telegram_link_tokens/{token}.json
    telegram_bindings/{chat_id}.json
"""

import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import requests

from app.config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

LINK_TOKENS_COLLECTION = "telegram_link_tokens"
BINDINGS_COLLECTION = "telegram_bindings"

# Agent catalog shown on the dashboard Agents page
AGENT_TYPES = {
    "autonomous": "Autonomous Agent",
    "customer_service": "Customer Service Agent",
    "leads_qualifier": "Leads Qualifier Agent",
    "finance_invoice_ops": "Finance & Invoice Ops Agent",
}

WELCOME_TEMPLATE = (
    "✅ {agent_name} is now connected to this chat.\n\n"
    "Send a message any time and your Aivory agent will pick it up. "
    "Send /stop to disconnect."
)

FALLBACK_REPLY = (
    "🤖 Your Aivory agent received the message. "
    "Live responses will appear here once the agent runtime is attached."
)

# Tokens are user-controlled input used as JSON filenames — restrict to the
# url-safe alphabet so they can never traverse out of the collection dir
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def is_valid_token_format(token: str) -> bool:
    return bool(_TOKEN_RE.match(token or ""))


class TelegramService:
    """Link tokens, chat bindings, and Bot API calls."""

    def __init__(self, db_service):
        self.db = db_service

    # ========================================================================
    # BOT API
    # ========================================================================

    def _api_url(self, method: str) -> str:
        return f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/{method}"

    def send_message(self, chat_id: int, text: str) -> bool:
        if not settings.telegram_bot_token:
            logger.warning("telegram_bot_token not configured; dropping reply")
            return False
        try:
            resp = requests.post(
                self._api_url("sendMessage"),
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            if not resp.ok:
                logger.error(f"sendMessage failed ({resp.status_code}): {resp.text[:200]}")
            return resp.ok
        except requests.RequestException as e:
            logger.error(f"sendMessage error: {e}")
            return False

    # ========================================================================
    # LINK TOKENS (one-time, expiring)
    # ========================================================================

    def create_link_token(
        self, user_id: str, agent_type: str, chat_target: str = "private"
    ) -> dict:
        """Create a one-time deep-link token for deploying an agent.

        chat_target: "private" (?start=) or "group" (?startgroup=)
        """
        if agent_type not in AGENT_TYPES:
            raise ValueError(f"Unknown agent_type '{agent_type}'")
        if chat_target not in ("private", "group"):
            raise ValueError("chat_target must be 'private' or 'group'")
        if not settings.telegram_bot_username:
            raise RuntimeError("telegram_bot_username is not configured")

        # 24 bytes -> 32 url-safe chars, well under Telegram's 64-char start payload limit
        token = secrets.token_urlsafe(24)
        now = datetime.utcnow()
        record = {
            "token": token,
            "user_id": user_id,
            "agent_type": agent_type,
            "chat_target": chat_target,
            "created_at": now.isoformat(),
            "expires_at": (
                now + timedelta(minutes=settings.telegram_link_token_ttl_minutes)
            ).isoformat(),
            "used": False,
            "used_at": None,
            "chat_id": None,
        }
        self.db.save_json(LINK_TOKENS_COLLECTION, token, record)

        param = "startgroup" if chat_target == "group" else "start"
        deep_link = f"https://t.me/{settings.telegram_bot_username}?{param}={token}"
        return {
            "token": token,
            "deep_link": deep_link,
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES[agent_type],
            "expires_at": record["expires_at"],
        }

    def get_link_status(self, token: str, user_id: str) -> dict:
        """Status for dashboard polling: pending | connected | expired | not_found."""
        if not is_valid_token_format(token):
            return {"status": "not_found"}
        record = self.db.load_json(LINK_TOKENS_COLLECTION, token)
        if not record or record.get("user_id") != user_id:
            return {"status": "not_found"}
        if record.get("used"):
            return {"status": "connected", "chat_id": record.get("chat_id")}
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            return {"status": "expired"}
        return {"status": "pending"}

    # ========================================================================
    # BINDINGS
    # ========================================================================

    def get_binding(self, chat_id: int) -> Optional[dict]:
        return self.db.load_json(BINDINGS_COLLECTION, str(chat_id))

    def list_bindings(self, user_id: str) -> list:
        all_bindings = self.db.load_all_json(BINDINGS_COLLECTION) or []
        return [b for b in all_bindings if b.get("user_id") == user_id]

    def delete_binding(self, chat_id: int, user_id: str) -> bool:
        binding = self.get_binding(chat_id)
        if not binding or binding.get("user_id") != user_id:
            return False
        return self.db.delete_json(BINDINGS_COLLECTION, str(chat_id))

    # ========================================================================
    # WEBHOOK UPDATE PROCESSING
    # ========================================================================

    def process_update(self, update: dict) -> None:
        message = update.get("message") or update.get("channel_post")
        if not message:
            return  # ignore edits, callbacks, member updates for now

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            return

        if text.startswith("/start"):
            self._handle_start(chat, text)
        elif text.startswith("/stop"):
            self._handle_stop(chat_id)
        else:
            self._handle_message(chat_id, message)

    def _handle_start(self, chat: dict, text: str) -> None:
        chat_id = chat["id"]
        # Accept "/start <token>" and "/start@BotName <token>" (group form)
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else None
        if not token:
            self.send_message(
                chat_id,
                "Hi! To connect an Aivory agent, deploy it from your dashboard "
                "and scan the QR code shown there.",
            )
            return

        if not is_valid_token_format(token):
            self.send_message(chat_id, "⚠️ This deploy link is invalid or already used. Generate a new one from your dashboard.")
            return

        record = self.db.load_json(LINK_TOKENS_COLLECTION, token)
        if not record or record.get("used"):
            self.send_message(chat_id, "⚠️ This deploy link is invalid or already used. Generate a new one from your dashboard.")
            return
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            self.send_message(chat_id, "⚠️ This deploy link has expired. Generate a new one from your dashboard.")
            return

        # Subscription guard: the linking user must still exist and be active
        user = self.db.load_json("users", record["user_id"])
        if not user or user.get("status") == "suspended":
            self.send_message(chat_id, "⚠️ This Aivory account is not active. Please check your subscription.")
            return

        agent_type = record["agent_type"]
        binding = {
            "chat_id": chat_id,
            "chat_type": chat.get("type", "private"),
            "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name"),
            "user_id": record["user_id"],
            "account_type": user.get("account_type", "free"),
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES.get(agent_type, agent_type),
            "status": "active",
            "linked_token": token,
            "created_at": datetime.utcnow().isoformat(),
        }
        self.db.save_json(BINDINGS_COLLECTION, str(chat_id), binding)

        record["used"] = True
        record["used_at"] = datetime.utcnow().isoformat()
        record["chat_id"] = chat_id
        self.db.save_json(LINK_TOKENS_COLLECTION, token, record)

        logger.info(f"Bound Telegram chat {chat_id} to user {record['user_id']} ({agent_type})")
        self.send_message(chat_id, WELCOME_TEMPLATE.format(agent_name=binding["agent_name"]))

    def _handle_stop(self, chat_id: int) -> None:
        binding = self.get_binding(chat_id)
        if binding:
            self.db.delete_json(BINDINGS_COLLECTION, str(chat_id))
            self.send_message(chat_id, "👋 Agent disconnected. Scan a new QR code from your dashboard to reconnect.")
        else:
            self.send_message(chat_id, "No agent is connected to this chat.")

    def _handle_message(self, chat_id: int, message: dict) -> None:
        binding = self.get_binding(chat_id)
        if not binding:
            return  # unbound chats get no reply; keeps the shared bot quiet

        # Re-check the owning account on every message so cancelled
        # subscriptions stop working without a manual unbind
        user = self.db.load_json("users", binding["user_id"])
        if not user or user.get("status") == "suspended":
            self.db.delete_json(BINDINGS_COLLECTION, str(chat_id))
            self.send_message(chat_id, "⚠️ This agent was disconnected because the Aivory subscription is no longer active.")
            return

        reply = self._route_to_agent(binding, message)
        self.send_message(chat_id, reply)

    def _route_to_agent(self, binding: dict, message: dict) -> str:
        """Forward a bound chat message to the agent gateway, if configured."""
        if not settings.telegram_agent_gateway_url:
            return FALLBACK_REPLY
        try:
            resp = requests.post(
                f"{settings.telegram_agent_gateway_url.rstrip('/')}/telegram/message",
                json={
                    "user_id": binding["user_id"],
                    "agent_type": binding["agent_type"],
                    "account_type": binding.get("account_type", "free"),
                    "chat_id": binding["chat_id"],
                    "text": message.get("text", ""),
                },
                timeout=60,
            )
            if resp.ok:
                return (resp.json().get("reply") or FALLBACK_REPLY)[:4096]
            logger.error(f"Agent gateway returned {resp.status_code}: {resp.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Agent gateway error: {e}")
        return "⚠️ The agent is temporarily unavailable. Please try again in a moment."
