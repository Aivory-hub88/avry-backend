#!/usr/bin/env bash
# Register webhooks + profiles for the Aivory deployable-agent bots.
#
# Multi-bot mode: one bot per agent type via TELEGRAM_BOT_TOKEN_<AGENT> env
# vars; any agent without its own token falls back to the shared default bot
# (TELEGRAM_BOT_TOKEN), which keeps single-bot setups working.
#
# Usage:
#   source .env && ./set_telegram_webhook.sh [webhook_base]
#
# The secret must match TELEGRAM_WEBHOOK_SECRET on the backend — Telegram
# echoes it back on every update as X-Telegram-Bot-Api-Secret-Token.
set -euo pipefail

WEBHOOK_BASE="${1:-https://backend.aivory.id/api/v1/telegram/webhook}"
: "${TELEGRAM_WEBHOOK_SECRET:?TELEGRAM_WEBHOOK_SECRET is required}"

# agent_key | short description | long description
AGENTS=(
  "autonomous|Aivory Autonomous Agent — triages, responds, and acts. Just type.|Your Aivory Autonomous Agent lives here. Deploy it from your Aivory dashboard, scan the QR code, and just start typing — no commands, no menus."
  "customer_service|Aivory Customer Service Agent — 24/7 support. Just type.|Your Aivory Customer Service Agent lives here. Deploy it from your Aivory dashboard, scan the QR code, and just start typing — no commands, no menus."
  "leads_qualifier|Aivory Leads Qualifier — BANT-qualifies your leads. Just type.|Your Aivory Leads Qualifier Agent lives here. Deploy it from your Aivory dashboard, scan the QR code, and just start typing — no commands, no menus."
  "finance_invoice_ops|Aivory Finance & Invoice Ops — precise invoice automation. Just type.|Your Aivory Finance & Invoice Ops Agent lives here. Deploy it from your Aivory dashboard, scan the QR code, and just start typing — no commands, no menus."
)

setup_bot() {
  local token="$1" url="$2" short_desc="$3" long_desc="$4"
  local api="https://api.telegram.org/bot${token}"

  curl -sS "${api}/setWebhook" \
    -d "url=${url}" \
    -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
    -d 'allowed_updates=["message"]' \
    -d "drop_pending_updates=true"
  echo
  # Prompt-only UX: no visible command menu, branded profile
  curl -sS "${api}/setMyCommands" -H 'Content-Type: application/json' -d '{"commands":[]}' > /dev/null
  curl -sS "${api}/setMyShortDescription" --data-urlencode "short_description=${short_desc}" > /dev/null
  curl -sS "${api}/setMyDescription" --data-urlencode "description=${long_desc}" > /dev/null
  curl -sS "${api}/getWebhookInfo" | head -c 300
  echo; echo
}

DEFAULT_DONE=""
for entry in "${AGENTS[@]}"; do
  IFS='|' read -r key short_desc long_desc <<< "$entry"
  var="TELEGRAM_BOT_TOKEN_$(echo "$key" | tr '[:lower:]' '[:upper:]')"
  token="${!var:-}"

  if [[ -n "$token" ]]; then
    echo "── ${key} (dedicated bot) ──"
    setup_bot "$token" "${WEBHOOK_BASE}/${key}" "$short_desc" "$long_desc"
  elif [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -z "$DEFAULT_DONE" ]]; then
    # Shared default bot serves every agent without a dedicated token.
    # Registered once, on the first fallback key's path — the backend resolves
    # that path back to the default bot, and bindings carry their own agent_type.
    echo "── default bot (shared fallback) ──"
    setup_bot "${TELEGRAM_BOT_TOKEN}" "${WEBHOOK_BASE}/${key}" \
      "Aivory AI agents, right in your Telegram. Just type." \
      "Your Aivory AI agent lives here. Deploy an agent from your Aivory dashboard, scan the QR code, and just start typing — no commands, no menus."
    DEFAULT_DONE=1
  fi
done
