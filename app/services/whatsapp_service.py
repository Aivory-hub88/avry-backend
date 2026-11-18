"""
WhatsApp deployable-agent service (Kapso transport, one shared Aivory number).

Mirrors TelegramService: a one-time LINK-xxxxxx code binds a WhatsApp number
(wa_id) to a tenant's agent; later messages from that number run the agent
through the same Cerveau gateway (`_route_to_agent(..., channel="whatsapp")`),
approvals and memory included.

Inbound webhooks land on the dashboard (Kapso -> hook.aivory.uk -> dashboard),
which forwards each message to POST /api/v1/whatsapp/inbound. The dashboard
keeps its own CRM flow for numbers that are not bound here.
"""

import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

from app.config import settings
from app.services import tiers
from app.services.telegram_service import (
    AGENT_TYPES,
    TelegramService,
    agent_tier_error,
    is_superadmin,
)

logger = logging.getLogger(__name__)

LINK_TOKENS_COLLECTION = "whatsapp_link_tokens"
BINDINGS_COLLECTION = "whatsapp_bindings"
ADDONS_COLLECTION = "whatsapp_addons"

KAPSO_BASE = "https://api.kapso.ai"
KAPSO_META = f"{KAPSO_BASE}/meta/whatsapp/v24.0"

LINK_TTL_MINUTES = 15
WINDOW_HOURS = 24
MAX_TEXT = 4096
DEDUPE_TTL_SECONDS = 48 * 3600
DEDUPE_MAX = 20000

# Unambiguous alphabet (no 0/O/1/I/L) so a code typed from a QR screen is hard to get wrong.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_TOKEN_LEN = 6
_LINK_RE = re.compile(r"^\s*LINK-([A-Za-z0-9]{6,8})\s*$", re.IGNORECASE)
_WA_ID_RE = re.compile(r"^\d{7,20}$")

WELCOME = (
    "✅ Terhubung! Kamu sekarang bisa ngobrol dengan {agent_name} di sini. "
    "Kirim pesan teks, voice note, gambar, atau dokumen."
)
LINK_INVALID = "⚠️ Kode ini tidak valid atau sudah dipakai. Buat kode baru dari dashboard Aivory."
LINK_EXPIRED = "⚠️ Kode ini sudah kedaluwarsa. Buat kode baru dari dashboard Aivory."
INACTIVE = "⚠️ Agent terputus karena langganan Aivory tidak aktif."
ADDON_OFF = "⚠️ Add-on WhatsApp untuk akun Aivory kamu tidak aktif. Aktifkan lagi dari dashboard."


class AddonRequired(Exception):
    """The tenant has not bought (or has lapsed on) the WhatsApp add-on."""


def normalize_wa_id(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def parse_link_code(text: str) -> Optional[str]:
    """`LINK-ab12cd` -> `AB12CD`, or None when the text is not a link code."""
    m = _LINK_RE.match(text or "")
    return m.group(1).upper() if m else None


def to_whatsapp_format(text: str) -> str:
    """Markdown -> WhatsApp formatting (`*bold*`, `_italic_`); drops heading marks."""
    out = re.sub(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$", r"*\1*", text or "", flags=re.MULTILINE)
    out = re.sub(r"\*\*(.+?)\*\*", r"*\1*", out)
    out = re.sub(r"__(.+?)__", r"*\1*", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", out)
    return out


def split_message(text: str, limit: int = MAX_TEXT) -> list:
    """Split on paragraph/line boundaries so each part fits WhatsApp's text limit."""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts, current = [], ""
    for para in text.split("\n"):
        while len(para) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:limit])
            para = para[limit:]
        if len(current) + len(para) + 1 > limit:
            parts.append(current)
            current = para
        else:
            current = f"{current}\n{para}" if current else para
    if current:
        parts.append(current)
    return parts


class _SeenIds:
    """Message ids already handled. Providers retry unanswered deliveries."""

    def __init__(self, ttl: int = DEDUPE_TTL_SECONDS, cap: int = DEDUPE_MAX):
        self._m: dict = {}
        self._ttl = ttl
        self._cap = cap

    def seen(self, message_id: str) -> bool:
        t = self._m.get(message_id)
        return t is not None and time.time() - t < self._ttl

    def add(self, message_id: str) -> None:
        if len(self._m) >= self._cap:
            for k in sorted(self._m, key=self._m.get)[: self._cap // 10]:
                self._m.pop(k, None)
        self._m[message_id] = time.time()


class WhatsAppService:
    """Link codes, number bindings, add-on entitlement and Kapso sends."""

    def __init__(self, db_service, telegram: Optional[TelegramService] = None):
        self.db = db_service
        # Reuse the Telegram engine for the agent turn, approvals and user lookup.
        self.tg = telegram or TelegramService(db_service)
        self._seen = _SeenIds()

    # ------------------------------------------------------------------ config
    @staticmethod
    def display_phone() -> str:
        phone = normalize_wa_id(os.getenv("KAPSO_DISPLAY_PHONE", ""))
        if not phone:
            raise RuntimeError("KAPSO_DISPLAY_PHONE is not configured")
        return phone

    @staticmethod
    def _kapso_headers() -> dict:
        key = os.getenv("KAPSO_API_KEY")
        if not key:
            raise RuntimeError("KAPSO_API_KEY is not configured")
        return {"x-api-key": key, "content-type": "application/json"}

    @staticmethod
    def _phone_number_id() -> str:
        pid = os.getenv("KAPSO_PHONE_NUMBER_ID")
        if not pid:
            raise RuntimeError("KAPSO_PHONE_NUMBER_ID is not configured")
        return pid

    # ------------------------------------------------------------------ add-on
    def addon_active(self, user_id: str) -> bool:
        """Paid add-on gate. Superadmins bypass it (monitoring/testing)."""
        user = self.tg._load_user(user_id)
        if user and is_superadmin(user):
            return True
        rec = self.db.load_json(ADDONS_COLLECTION, user_id)
        if not rec or not rec.get("active"):
            return False
        expires = rec.get("expires_at")
        return not expires or datetime.utcnow().isoformat() <= expires

    def set_addon(self, user_id: str, active: bool, expires_at: Optional[str] = None) -> dict:
        rec = {
            "user_id": user_id,
            "active": bool(active),
            "expires_at": expires_at,
            "updated_at": datetime.utcnow().isoformat(),
        }
        self.db.save_json(ADDONS_COLLECTION, user_id, rec)
        return rec

    # ------------------------------------------------------------------ link codes
    def create_link_token(self, user_id: str, agent_type: str) -> dict:
        if agent_type not in AGENT_TYPES:
            raise ValueError(f"Unknown agent_type '{agent_type}'")
        tier_err = agent_tier_error(self.tg._load_user(user_id), agent_type)
        if tier_err:
            raise PermissionError(tier_err)
        if not self.addon_active(user_id):
            raise AddonRequired("The WhatsApp add-on is not active for this account.")
        phone = self.display_phone()

        token = "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LEN))
        while self.db.load_json(LINK_TOKENS_COLLECTION, token):  # vanishingly rare collision
            token = "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LEN))
        now = datetime.utcnow()
        record = {
            "token": token,
            "user_id": user_id,
            "agent_type": agent_type,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=LINK_TTL_MINUTES)).isoformat(),
            "used": False,
            "used_at": None,
            "wa_id": None,
        }
        self.db.save_json(LINK_TOKENS_COLLECTION, token, record)
        return {
            "token": token,
            "deep_link": f"https://wa.me/{phone}?text=LINK-{token}",
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES[agent_type],
            "display_phone": phone,
            "expires_at": record["expires_at"],
        }

    def get_link_status(self, token: str, user_id: str) -> dict:
        token = (token or "").upper()
        if not re.match(r"^[A-Z0-9]{6,8}$", token):
            return {"status": "not_found"}
        record = self.db.load_json(LINK_TOKENS_COLLECTION, token)
        if not record or record.get("user_id") != user_id:
            return {"status": "not_found"}
        if record.get("used"):
            return {"status": "connected", "wa_id": record.get("wa_id")}
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            return {"status": "expired"}
        return {"status": "pending"}

    # ------------------------------------------------------------------ bindings
    def get_binding(self, wa_id: str) -> Optional[dict]:
        return self.db.load_json(BINDINGS_COLLECTION, wa_id)

    def list_bindings(self, user_id: str) -> list:
        return [b for b in (self.db.load_all_json(BINDINGS_COLLECTION) or []) if b.get("user_id") == user_id]

    def delete_binding(self, binding_id: str, user_id: str) -> Optional[dict]:
        if not _WA_ID_RE.match(binding_id or ""):
            return None
        binding = self.db.load_json(BINDINGS_COLLECTION, binding_id)
        if not binding or binding.get("user_id") != user_id:
            return None
        self.db.delete_json(BINDINGS_COLLECTION, binding_id)
        return binding

    def _bind(self, wa_id: str, code: str, name: Optional[str]) -> str:
        """Redeem a link code for `wa_id`. Returns the reply to send."""
        record = self.db.load_json(LINK_TOKENS_COLLECTION, code)
        if not record or record.get("used"):
            return LINK_INVALID
        if datetime.utcnow().isoformat() > record.get("expires_at", ""):
            return LINK_EXPIRED
        user = self.tg._load_user(record["user_id"])
        if not self.tg._is_active(user):
            return INACTIVE
        agent_type = record["agent_type"]
        tier_err = agent_tier_error(user, agent_type)
        if tier_err:
            return f"⚠️ {tier_err}"
        if not self.addon_active(record["user_id"]):
            return ADDON_OFF

        now = datetime.utcnow().isoformat()
        binding = {
            "binding_id": wa_id,
            # Telegram-shaped fields so the gateway/approval code works unchanged.
            "chat_id": wa_id,
            "wa_id": wa_id,
            "wa_name": name,
            "user_id": record["user_id"],
            "account_type": user.get("account_type", "free"),
            "tier": "enterprise" if is_superadmin(user) else tiers.account_tier(user.get("tier")),
            "agent_type": agent_type,
            "agent_name": AGENT_TYPES.get(agent_type, agent_type),
            "status": "active",
            "channel": "whatsapp",
            "linked_token": code,
            "last_inbound_at": now,
            "created_at": now,
        }
        self.db.save_json(BINDINGS_COLLECTION, wa_id, binding)
        record["used"] = True
        record["used_at"] = now
        record["wa_id"] = wa_id
        self.db.save_json(LINK_TOKENS_COLLECTION, code, record)
        logger.info("Bound WhatsApp %s to user %s (%s)", wa_id[-4:], record["user_id"], agent_type)
        return WELCOME.format(agent_name=binding["agent_name"])

    # ------------------------------------------------------------------ Kapso I/O
    def send_text(self, wa_id: str, text: str) -> bool:
        """Send inside the 24h window (callers reply to an inbound message). Splits long text."""
        ok = True
        try:
            headers = self._kapso_headers()
            url = f"{KAPSO_META}/{self._phone_number_id()}/messages"
        except RuntimeError as e:
            logger.error("WhatsApp send not configured: %s", e)
            return False
        for part in split_message(to_whatsapp_format(text)):
            try:
                r = requests.post(
                    url,
                    headers=headers,
                    json={"messaging_product": "whatsapp", "to": wa_id, "type": "text", "text": {"body": part}},
                    timeout=15,
                )
                if not r.ok:
                    logger.error("Kapso send failed %s: %s", r.status_code, r.text[:200])
                    ok = False
            except requests.RequestException as e:
                logger.error("Kapso send error: %s", e)
                ok = False
        return ok

    def send_to_binding(self, binding: dict, text: str) -> bool:
        """Proactive send (reminders, scheduled runs): only inside the 24h window.

        Outside it WhatsApp requires an approved template; that path is a later phase,
        so this returns False and the caller keeps the result for the next inbound turn.
        """
        last = binding.get("last_inbound_at") or ""
        if not last or datetime.utcnow() - datetime.fromisoformat(last) > timedelta(hours=WINDOW_HOURS):
            return False
        return self.send_text(binding["wa_id"], text)

    def download_media(self, media_id: str) -> Optional[bytes]:
        """media_id -> URL -> bytes (Graph-style two-step, through Kapso's Meta proxy)."""
        if not re.match(r"^[A-Za-z0-9_-]{1,128}$", media_id or ""):
            return None
        try:
            headers = self._kapso_headers()
            meta = requests.get(f"{KAPSO_META}/{media_id}", headers=headers, timeout=15)
            if not meta.ok:
                logger.error("Kapso media lookup failed %s", meta.status_code)
                return None
            url = (meta.json() or {}).get("url")
            if not url or not str(url).startswith("https://"):
                return None
            blob = requests.get(url, headers=headers, timeout=30)
            return blob.content if blob.ok else None
        except (requests.RequestException, RuntimeError, ValueError) as e:
            logger.error("WhatsApp media download failed: %s", e)
            return None

    # ------------------------------------------------------------------ prompt building
    def build_prompt(self, msg: dict) -> str:
        """Compose the agent prompt from the text and any media (same extractors as Telegram)."""
        from app.services import attachment_extractor as ax

        caption = (msg.get("text") or "").strip()
        kind = msg.get("type") or "text"
        media_id = msg.get("media_id")
        mime = msg.get("mime_type")
        attachments = []

        if media_id and kind in ("audio", "voice"):
            duration = msg.get("duration")
            if duration and duration > ax.MAX_AUDIO_SECONDS:
                transcript = "[This voice note is too long to transcribe.]"
            else:
                data = self.download_media(media_id)
                transcript = (
                    ax.transcribe_audio(data, msg.get("filename") or "voice.ogg", mime, duration) if data else None
                )
            if transcript is None:
                transcript = "[The user sent a voice note but it could not be transcribed.]"
            attachments.append({"filename": "voice", "content": transcript, "kind": "voice"})
            caption = ""  # Kapso fills text with a "(audio)" placeholder
        elif media_id and kind == "image":
            data = self.download_media(media_id)
            if data:
                content = ax.describe_image(data, mime or "image/jpeg", caption)
                attachments.append({"filename": "photo.jpg", "content": content, "kind": "image"})
        elif media_id and kind == "document":
            data = self.download_media(media_id)
            if data:
                name = msg.get("filename") or "document"
                if ax.is_image(name, mime):
                    attachments.append(
                        {"filename": name, "content": ax.describe_image(data, mime or "image/jpeg", caption), "kind": "image"}
                    )
                else:
                    content = ax.extract_document_text(name, data, mime)
                    if content is None:
                        content = f"[Received an unsupported file type: {name}]"
                    attachments.append({"filename": name, "content": content, "kind": "document"})
        elif kind not in ("text", "button", "interactive"):
            caption = ""  # placeholder like "(sticker)" is not user text

        return ax.compose_prompt(caption, attachments)

    # ------------------------------------------------------------------ inbound
    def classify_inbound(self, msg: dict) -> dict:
        """Synchronous half of an inbound message: decide who owns it.

        Returns {"status": "handled" | "duplicate" | "not_handled", "binding": dict | None,
                 "reply": str | None}. `reply` is sent by the caller (link flow); for a bound
        number the caller runs `run_turn` in the background.
        """
        message_id = str(msg.get("message_id") or "")
        wa_id = normalize_wa_id(msg.get("from") or "")
        if not message_id or not _WA_ID_RE.match(wa_id):
            return {"status": "not_handled", "binding": None, "reply": None}
        if self._seen.seen(message_id):
            return {"status": "duplicate", "binding": None, "reply": None}

        text = (msg.get("text") or "").strip()
        code = parse_link_code(text)
        if code:
            self._seen.add(message_id)
            return {"status": "handled", "binding": None, "reply": self._bind(wa_id, code, msg.get("name"))}

        binding = self.get_binding(wa_id)
        if not binding:
            return {"status": "not_handled", "binding": None, "reply": None}  # CRM flow keeps it
        self._seen.add(message_id)

        user = self.tg._load_user(binding["user_id"])
        if not self.tg._is_active(user):
            self.db.delete_json(BINDINGS_COLLECTION, wa_id)
            return {"status": "handled", "binding": None, "reply": INACTIVE}
        if not self.addon_active(binding["user_id"]):
            return {"status": "handled", "binding": None, "reply": ADDON_OFF}

        binding["last_inbound_at"] = datetime.utcnow().isoformat()
        self.db.save_json(BINDINGS_COLLECTION, wa_id, binding)
        return {"status": "handled", "binding": binding, "reply": None}

    def run_turn(self, binding: dict, msg: dict) -> None:
        """Background half: run the agent and reply. Never raises (it runs after the response)."""
        wa_id = binding["wa_id"]
        try:
            raw = (msg.get("text") or "").strip()
            if raw and (msg.get("type") or "text") in ("text", "button", "interactive"):
                handled = self.tg._try_conversational_approval(binding, raw)
                if handled is not None:
                    self.send_text(wa_id, handled["reply"])
                    return
            prompt = self.build_prompt(msg)
            if not prompt:
                return
            result = self.tg._remember_pending_result(
                binding, self.tg._route_to_agent(binding, prompt, channel="whatsapp")
            )
            self.send_text(wa_id, result["reply"])
        except Exception as e:  # noqa: BLE001 - background task, log and move on
            logger.exception("WhatsApp turn failed for %s: %s", wa_id[-4:], e)
