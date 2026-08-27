"""
Telegram deployable-agent service.

Flow:
  1. Dashboard calls POST /api/v1/telegram/deploy-link (JWT auth) -> one-time token
     rendered as a QR of https://t.me/<bot>?start=<token>
  2. User scans, taps Start -> Telegram delivers "/start <token>" to our webhook
  3. Token is redeemed: the chat_id is bound to {user_id, agent_type}
  4. Subsequent messages in that chat are routed to the agent backend

Multi-bot: each agent type can have its own bot (TELEGRAM_BOT_TOKEN_<AGENT>,
TELEGRAM_BOT_USERNAME_<AGENT>); anything unset falls back to the shared default
bot (TELEGRAM_BOT_TOKEN / TELEGRAM_BOT_USERNAME). Bindings are keyed by
(bot_id, chat_id) so the same private chat can host a different agent per bot.

Storage follows the MVP file-based convention (DatabaseService JSON collections):
    telegram_link_tokens/{token}.json
    telegram_bindings/{bot_id}_{chat_id}.json
"""

import logging
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import requests

from app.config import settings
from app.services import tiers

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
    "office_assistant": "Office Assistant Agent",
}

# Minimum subscription tier per agent type (operational < business <
# enterprise; see app/services/tiers.py). Unlisted agents are available on
# every paid tier.
AGENT_MIN_TIER = {
    "office_assistant": "enterprise",
}



def is_superadmin(user) -> bool:
    """Superadmins bypass tier gates (monitoring + feature testing)."""
    u = user or {}
    return bool(u.get("is_superadmin")) or str(u.get("account_type") or "").lower() == "superadmin"


def agent_tier_error(user, agent_type: str):
    """Return an error message if the user's tier can't deploy this agent, else None."""
    required = AGENT_MIN_TIER.get(agent_type)
    if not required:
        return None
    if is_superadmin(user):
        return None
    tier = tiers.account_tier((user or {}).get("tier"))
    if not tiers.meets(tier, required):
        name = AGENT_TYPES.get(agent_type, agent_type)
        return f"{name} is available on the Enterprise plan. Upgrade to deploy it."
    return None

# Prompt-only UX: no commands mentioned anywhere. Disconnecting is done from
# the dashboard; /stop still works but stays undocumented as a fallback.
WELCOME_TEMPLATE = (
    "✅ {agent_name} is connected.\n\n"
    "Just type what you need — no commands, no menus. I'm ready when you are."
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


# Pending-approval ids are Cerveau's own `pa_<uuid4>` (39 chars) — validate
# the shape before ever round-tripping one through a Telegram callback_data
# payload or the approval-decision gateway call, same discipline as
# `is_valid_token_format` above for deploy-link tokens.
_PENDING_APPROVAL_ID_RE = re.compile(r"^pa_[A-Za-z0-9-]{1,64}$")

# appr:{pending_id}:approve|deny — well under Telegram's 64-byte
# callback_data cap even at the id's max validated length (5 + 64 + 1 + 7 = 77
# is the theoretical worst case, but real ids are 39 chars: 5+39+1+7=52).
def _approval_keyboard(pending_approval: Optional[dict]) -> Optional[dict]:
    """Build a Telegram inline keyboard for a pending approval, or None."""
    if not pending_approval or not isinstance(pending_approval, dict):
        return None
    pending_id = pending_approval.get("id")
    if not pending_id or not _PENDING_APPROVAL_ID_RE.match(pending_id):
        logger.error(f"Refusing to render approval keyboard for malformed pending_id: {pending_id!r}")
        return None
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"appr:{pending_id}:approve"},
            {"text": "❌ Deny", "callback_data": f"appr:{pending_id}:deny"},
        ]]
    }


def load_user_record(db, user_id: str) -> Optional[dict]:
    """Load a user (+ current subscription tier) — Postgres on prod, file store in dev.

    The tier lives in user_tiers (written by the payments flow); an expired or
    missing entitlement row counts as `tiers.FREE_TIER`, i.e. no plan.
    """
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        try:
            import psycopg2

            conn = psycopg2.connect(dsn, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT u.id, u.account_type, u.is_active, u.is_superadmin,
                               t.tier, t.expires_at
                        FROM users u
                        LEFT JOIN user_tiers t ON t.user_id = u.id
                        WHERE u.id = %s
                        """,
                        (user_id,),
                    )
                    row = cur.fetchone()
                if row:
                    tier = row[4]
                    expires_at = row[5]
                    if expires_at is not None and expires_at < datetime.utcnow():
                        tier = None  # entitlement lapsed
                    return {
                        "user_id": row[0],
                        "account_type": row[1] or "free",
                        "is_active": row[2],
                        "is_superadmin": bool(row[3]),
                        # `tier` is None here when the account has no
                        # entitlement row or its subscription has lapsed (see
                        # the expiry check above). That used to resolve to the
                        # base PAID tier, so a lapsed subscriber — and anyone
                        # who never paid — kept the paid feature set.
                        "tier": tiers.account_tier(tier),
                    }
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Postgres user lookup failed, using file store: {e}")
    user = db.load_json("users", user_id)
    if user is not None and not user.get("tier"):
        user["tier"] = tiers.FREE_TIER
    return user


def get_bot(agent_type: Optional[str]) -> Optional[dict]:
    """Resolve bot credentials for an agent type.

    Per-agent env vars win; otherwise fall back to the shared default bot.
    Returns {token, username, bot_id, agent_type} or None if unconfigured.
    """
    token = username = None
    if agent_type:
        suffix = agent_type.upper()
        token = os.getenv(f"TELEGRAM_BOT_TOKEN_{suffix}")
        username = os.getenv(f"TELEGRAM_BOT_USERNAME_{suffix}")
    token = token or settings.telegram_bot_token
    username = username or settings.telegram_bot_username
    if not token or not username:
        return None
    return {
        "token": token,
        "username": username,
        "bot_id": token.split(":", 1)[0],
        "agent_type": agent_type,
    }


class TelegramService:
    """Link tokens, chat bindings, and Bot API calls."""

    def __init__(self, db_service):
        self.db = db_service

    # ========================================================================
    # BOT API
    # ========================================================================

    def _api_url(self, method: str, token: str) -> str:
        return f"{TELEGRAM_API_BASE}/bot{token}/{method}"

    def send_message(
        self, bot: dict, chat_id: int, text: str, reply_markup: Optional[dict] = None
    ) -> bool:
        if not bot:
            logger.warning("No bot configured; dropping reply")
            return False
        body = {"chat_id": chat_id, "text": text}
        if reply_markup:
            body["reply_markup"] = reply_markup
        try:
            resp = requests.post(
                self._api_url("sendMessage", bot["token"]),
                json=body,
                timeout=10,
            )
            if not resp.ok:
                logger.error(f"sendMessage failed ({resp.status_code}): {resp.text[:200]}")
            return resp.ok
        except requests.RequestException as e:
            logger.error(f"sendMessage error: {e}")
            return False

    def answer_callback_query(self, bot: dict, callback_query_id: str, text: str = "") -> None:
        """Stop the button's loading spinner. Best-effort — Telegram wants
        this within seconds of the tap, but a failure here shouldn't block
        sending the real reply."""
        if not bot:
            return
        try:
            requests.post(
                self._api_url("answerCallbackQuery", bot["token"]),
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=5,
            )
        except requests.RequestException:
            pass

    def edit_message_reply_markup(
        self, bot: dict, chat_id: int, message_id: int, reply_markup: Optional[dict] = None
    ) -> None:
        """Strip (or replace) a sent message's inline keyboard — used after
        an approval is resolved so the buttons aren't tappable twice.
        Best-effort: Cerveau's resolve is idempotent either way, so a
        failure here is a UX rough edge, not a correctness problem."""
        if not bot:
            return
        try:
            requests.post(
                self._api_url("editMessageReplyMarkup", bot["token"]),
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": reply_markup or {"inline_keyboard": []},
                },
                timeout=5,
            )
        except requests.RequestException:
            pass

    def send_typing(self, bot: dict, chat_id: int) -> None:
        """Show the 'typing…' indicator while the agent thinks (best-effort)."""
        if not bot:
            return
        try:
            requests.post(
                self._api_url("sendChatAction", bot["token"]),
                json={"chat_id": chat_id, "action": "typing"},
                timeout=5,
            )
        except requests.RequestException:
            pass

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
        tier_err = agent_tier_error(self._load_user(user_id), agent_type)
        if tier_err:
            raise PermissionError(tier_err)
        bot = get_bot(agent_type)
        if not bot:
            raise RuntimeError(f"No Telegram bot configured for '{agent_type}'")

        # 24 bytes -> 32 url-safe chars, well under Telegram's 64-char start payload limit
        token = secrets.token_urlsafe(24)
        now = datetime.utcnow()
        record = {
            "token": token,
            "user_id": user_id,
            "agent_type": agent_type,
            "bot_id": bot["bot_id"],
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
        deep_link = f"https://t.me/{bot['username']}?{param}={token}"
        return {
            "token": token,
            "deep_link": deep_link,
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES[agent_type],
            "bot_username": bot["username"],
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
    # USER LOOKUP (Postgres in prod, JSON files in dev)
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
    # BINDINGS — keyed by (bot_id, chat_id)
    # ========================================================================

    @staticmethod
    def _binding_id(bot: dict, chat_id: int) -> str:
        return f"{bot['bot_id']}_{chat_id}"

    def get_binding(self, bot: dict, chat_id: int) -> Optional[dict]:
        return self.db.load_json(BINDINGS_COLLECTION, self._binding_id(bot, chat_id))

    def list_bindings(self, user_id: str) -> list:
        all_bindings = self.db.load_all_json(BINDINGS_COLLECTION) or []
        return [b for b in all_bindings if b.get("user_id") == user_id]

    def delete_binding(self, binding_id: str, user_id: str) -> Optional[dict]:
        """Delete a user's binding by its id; returns the binding if removed."""
        if not re.match(r"^\d+_-?\d+$", binding_id or ""):
            return None
        binding = self.db.load_json(BINDINGS_COLLECTION, binding_id)
        if not binding or binding.get("user_id") != user_id:
            return None
        self.db.delete_json(BINDINGS_COLLECTION, binding_id)
        return binding

    # ========================================================================
    # WEBHOOK UPDATE PROCESSING
    # ========================================================================

    def process_update(self, update: dict, bot: dict) -> None:
        """Handle one Bot API update, in the context of the bot that received it."""
        callback_query = update.get("callback_query")
        if callback_query:
            self._handle_callback(callback_query, bot)
            return

        message = update.get("message") or update.get("channel_post")
        if not message:
            return  # ignore edits, member updates for now

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return

        text = (message.get("text") or "").strip()
        has_attachment = bool(message.get("document") or message.get("photo"))
        if not text and not has_attachment:
            return  # stickers, location, etc. — nothing to act on

        if text.startswith("/start"):
            self._handle_start(chat, text, bot)
        elif text.startswith("/stop"):
            # Undocumented fallback; the official disconnect lives in the dashboard
            self._handle_stop(chat_id, bot)
        else:
            self._handle_message(chat_id, message, bot)

    def _handle_start(self, chat: dict, text: str, bot: dict) -> None:
        chat_id = chat["id"]
        # Accept "/start <token>" and "/start@BotName <token>" (group form)
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else None
        if not token:
            self.send_message(
                bot,
                chat_id,
                "Hi! To connect an Aivory agent, deploy it from your dashboard "
                "and scan the QR code shown there.",
            )
            return

        if not is_valid_token_format(token):
            self.send_message(bot, chat_id, "⚠️ This deploy link is invalid or already used. Generate a new one from your dashboard.")
            return

        record = self.db.load_json(LINK_TOKENS_COLLECTION, token)
        if not record or record.get("used"):
            self.send_message(bot, chat_id, "⚠️ This deploy link is invalid or already used. Generate a new one from your dashboard.")
            return
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            self.send_message(bot, chat_id, "⚠️ This deploy link has expired. Generate a new one from your dashboard.")
            return

        # A deploy link is minted for a specific bot — reject cross-bot redemption
        # (e.g. a Leads QR pasted into the CS bot) so personas stay 1:1 per bot.
        if record.get("bot_id") and record["bot_id"] != bot["bot_id"]:
            self.send_message(bot, chat_id, "⚠️ This deploy link belongs to a different Aivory agent. Please scan the QR code again from your dashboard.")
            return

        # Subscription guard: the linking user must still exist and be active
        user = self._load_user(record["user_id"])
        if not self._is_active(user):
            self.send_message(bot, chat_id, "⚠️ This Aivory account is not active. Please check your subscription.")
            return

        agent_type = record["agent_type"]
        tier_err = agent_tier_error(user, agent_type)
        if tier_err:
            self.send_message(bot, chat_id, f"⚠️ {tier_err}")
            return
        binding = {
            "binding_id": self._binding_id(bot, chat_id),
            "bot_id": bot["bot_id"],
            "bot_username": bot["username"],
            "chat_id": chat_id,
            "chat_type": chat.get("type", "private"),
            "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name"),
            "user_id": record["user_id"],
            "account_type": user.get("account_type", "free"),
            # superadmins get the effective top tier (monitoring/testing access)
            "tier": "enterprise" if is_superadmin(user) else tiers.account_tier(user.get("tier")),
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES.get(agent_type, agent_type),
            "status": "active",
            "linked_token": token,
            "created_at": datetime.utcnow().isoformat(),
        }
        self.db.save_json(BINDINGS_COLLECTION, binding["binding_id"], binding)

        record["used"] = True
        record["used_at"] = datetime.utcnow().isoformat()
        record["chat_id"] = chat_id
        self.db.save_json(LINK_TOKENS_COLLECTION, token, record)

        logger.info(
            f"Bound Telegram chat {chat_id} (bot {bot['username']}) "
            f"to user {record['user_id']} ({agent_type})"
        )
        self.send_message(bot, chat_id, WELCOME_TEMPLATE.format(agent_name=binding["agent_name"]))

    def _handle_stop(self, chat_id: int, bot: dict) -> None:
        binding = self.get_binding(bot, chat_id)
        if binding:
            self.db.delete_json(BINDINGS_COLLECTION, self._binding_id(bot, chat_id))
            self.send_message(bot, chat_id, "👋 Agent disconnected. Scan a new QR code from your dashboard to reconnect.")
        else:
            self.send_message(bot, chat_id, "No agent is connected to this chat.")

    # ========================================================================
    # APPROVE/DENY BUTTON TAPS (Cerveau F-1-for-approvals, Part B)
    # ========================================================================

    def _handle_callback(self, callback_query: dict, bot: dict) -> None:
        callback_id = callback_query.get("id")
        data = callback_query.get("data") or ""
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")

        # Telegram wants this within seconds regardless of how long the real
        # work below takes — stops the button's loading spinner immediately.
        # Real feedback (the continuation reply) arrives as a follow-up
        # sendMessage, same pattern as a normal text turn's send_typing.
        if callback_id:
            self.answer_callback_query(bot, callback_id)

        if chat_id is None:
            return

        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "appr" or parts[2] not in ("approve", "deny"):
            logger.error(f"Malformed approval callback_data: {data!r}")
            return
        _, pending_id, decision = parts
        if not _PENDING_APPROVAL_ID_RE.match(pending_id):
            logger.error(f"Malformed pending_id in callback_data: {pending_id!r}")
            return

        binding = self.get_binding(bot, chat_id)
        if not binding:
            return  # unbound chat somehow tapped a stale button; nothing to resolve as

        user = self._load_user(binding["user_id"])
        if not self._is_active(user):
            self.send_message(bot, chat_id, "⚠️ This agent was disconnected because the Aivory subscription is no longer active.")
            return

        result = self._resolve_approval(binding, pending_id, decision)
        self.send_message(bot, chat_id, result["reply"])

        # Only strip the buttons once the row has actually moved out of
        # "pending" — on a credit-exhaustion reply or a transient gateway
        # error, the row is untouched, so leaving them tappable lets the
        # user retry instead of stranding the approval with no affordance.
        if result.get("outcome") and message_id is not None:
            self.edit_message_reply_markup(bot, chat_id, message_id)

    def _resolve_approval(self, binding: dict, pending_id: str, decision: str) -> dict:
        """Forward an approve/deny tap to the agent gateway.

        Returns {"reply": str, "outcome": str | None} — outcome is None on
        credit exhaustion or a gateway error (nothing was actually resolved).
        """
        if not settings.telegram_agent_gateway_url:
            return {"reply": FALLBACK_REPLY, "outcome": None}
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
                    "session_id": binding.get("binding_id") or str(binding["chat_id"]),
                    "pending_id": pending_id,
                    "decision": decision,
                },
                # Same headroom reasoning as _route_to_agent — a continuation
                # turn is a real LLM call, same latency class as a normal one.
                timeout=195,
            )
            if resp.ok:
                data = resp.json()
                return {
                    "reply": (data.get("reply_text") or "Done.")[:4096],
                    "outcome": data.get("outcome"),
                }
            if resp.status_code == 404:
                return {
                    "reply": "⚠️ This approval request could no longer be found — it may have already expired or been resolved.",
                    "outcome": None,
                }
            logger.error(f"Approval-decision gateway returned {resp.status_code}: {resp.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Approval-decision gateway error: {e}")
        return {
            "reply": "⚠️ Couldn't process that right now. Please try tapping the button again in a moment.",
            "outcome": None,
        }

    def _handle_message(self, chat_id: int, message: dict, bot: dict) -> None:
        binding = self.get_binding(bot, chat_id)
        if not binding:
            return  # unbound chats get no reply; keeps the shared bot quiet

        # Re-check the owning account on every message so cancelled
        # subscriptions stop working without a manual unbind
        user = self._load_user(binding["user_id"])
        if not self._is_active(user):
            self.db.delete_json(BINDINGS_COLLECTION, self._binding_id(bot, chat_id))
            self.send_message(bot, chat_id, "⚠️ This agent was disconnected because the Aivory subscription is no longer active.")
            return

        self.send_typing(bot, chat_id)
        prompt = self._build_prompt(bot, message)
        if not prompt:
            return  # nothing readable (e.g. unsupported file with no caption)
        result = self._route_to_agent(binding, prompt)
        reply_markup = _approval_keyboard(result.get("pending_approval"))
        self.send_message(bot, chat_id, result["reply"], reply_markup=reply_markup)

    # ------------------------------------------------------------------
    # Attachment handling
    # ------------------------------------------------------------------

    def _download_file(self, bot: dict, file_id: str) -> Optional[bytes]:
        """Resolve a Telegram file_id to bytes (getFile + download)."""
        try:
            info = requests.get(
                self._api_url("getFile", bot["token"]),
                params={"file_id": file_id},
                timeout=15,
            ).json()
            if not info.get("ok"):
                return None
            path = info["result"]["file_path"]
            dl = requests.get(
                f"{TELEGRAM_API_BASE}/file/bot{bot['token']}/{path}", timeout=30
            )
            return dl.content if dl.ok else None
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.error(f"Telegram file download failed: {e}")
            return None

    def _build_prompt(self, bot: dict, message: dict) -> str:
        """Compose the agent prompt from the message text + any attachment."""
        from app.services import attachment_extractor as ax

        caption = (message.get("text") or message.get("caption") or "").strip()
        attachments = []

        doc = message.get("document")
        if doc and doc.get("file_id"):
            data = self._download_file(bot, doc["file_id"])
            if data:
                name = doc.get("file_name") or "document"
                mime = doc.get("mime_type")
                if ax.is_image(name, mime):
                    content = ax.describe_image(data, mime or "image/jpeg", caption)
                    attachments.append({"filename": name, "content": content, "kind": "image"})
                else:
                    content = ax.extract_document_text(name, data, mime)
                    if content is None:
                        content = f"[Received an unsupported file type: {name}]"
                    attachments.append({"filename": name, "content": content, "kind": "document"})

        photos = message.get("photo")
        if photos:
            # photo is a list of sizes ascending; grab the largest
            largest = photos[-1]
            data = self._download_file(bot, largest["file_id"])
            if data:
                content = ax.describe_image(data, "image/jpeg", caption)
                attachments.append({"filename": "photo.jpg", "content": content, "kind": "image"})

        return ax.compose_prompt(caption, attachments)

    def _route_to_agent(self, binding: dict, text: str, channel: str = "telegram") -> dict:
        """Forward the composed prompt to the agent gateway, if configured.

        Returns {"reply": str, "pending_approval": dict | None} — the second
        key is additive (Cerveau patch 0035 relayed through the bridge) and
        only ever non-null for engine='cerveau' tenants; callers that don't
        care (e.g. the console) can just read ["reply"].
        """
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
                    "chat_id": binding["chat_id"],
                    # unique per (bot, chat) so agent histories never merge
                    "session_id": binding.get("binding_id") or str(binding["chat_id"]),
                    "text": text,
                    "channel": channel,
                },
                # 195s: headroom over the bridge's own 185s Cerveau call
                # (Phase 6.1, itself 5s over Cerveau's 180s wall-clock cap) —
                # was 90s, too short for a Cerveau-routed tenant's
                # tool-calling loop to ever finish before this gave up.
                timeout=195,
            )
            if resp.ok:
                data = resp.json()
                return {
                    "reply": (data.get("reply") or FALLBACK_REPLY)[:4096],
                    "pending_approval": data.get("pending_approval"),
                }
            logger.error(f"Agent gateway returned {resp.status_code}: {resp.text[:200]}")
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Agent gateway error: {e}")
        return {
            "reply": "⚠️ The agent is temporarily unavailable. Please try again in a moment.",
            "pending_approval": None,
        }

    def route_console_message(
        self, user: dict, agent_type: str, text: str, conversation_id: Optional[str] = None
    ) -> dict:
        """Talk to a deployable agent from the dashboard AI Console.

        No chat binding involved — the console session id keeps agent history
        separate from any Telegram/Slack chats of the same agent. Returns the
        full {"reply", "pending_approval"} dict — the console now renders its
        own Approve/Deny buttons from `pending_approval` and resolves through
        the existing /api/v1/agent-approvals endpoint, the same one the
        dashboard's Approvals page uses.
        """
        pseudo_binding = {
            "user_id": user["user_id"],
            "account_type": user.get("account_type", "free"),
            "agent_type": agent_type,
            "chat_id": 0,
            "binding_id": f"console_{user['user_id']}_{agent_type}_{conversation_id or 'default'}",
        }
        return self._route_to_agent(pseudo_binding, text, channel="console")
