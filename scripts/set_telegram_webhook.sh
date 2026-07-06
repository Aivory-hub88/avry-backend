#!/usr/bin/env bash
# Register (or update) the Telegram webhook for the Aivory deployable-agent bot.
#
# Usage:
#   TELEGRAM_BOT_TOKEN=123:abc TELEGRAM_WEBHOOK_SECRET=... ./set_telegram_webhook.sh [webhook_url]
#
# Defaults to the production backend route. The secret must match the
# TELEGRAM_WEBHOOK_SECRET the backend container runs with — Telegram echoes it
# back on every update as X-Telegram-Bot-Api-Secret-Token.
set -euo pipefail

WEBHOOK_URL="${1:-https://backend.aivory.id/api/v1/telegram/webhook}"

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}"
: "${TELEGRAM_WEBHOOK_SECRET:?TELEGRAM_WEBHOOK_SECRET is required}"

API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

curl -sS "${API}/setWebhook" \
  -d "url=${WEBHOOK_URL}" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  -d 'allowed_updates=["message"]' \
  -d "drop_pending_updates=true"
echo

# Prompt-only UX: clear the command menu entirely (no visible commands)
# and brand the pre-Start profile screen like an AI product.
curl -sS "${API}/setMyCommands" -H 'Content-Type: application/json' -d '{"commands":[]}'
echo
curl -sS "${API}/setMyShortDescription" \
  --data-urlencode 'short_description=Aivory AI agents, right in your Telegram. Just type.'
echo
curl -sS "${API}/setMyDescription" \
  --data-urlencode 'description=Your Aivory AI agent lives here. Deploy an agent from your Aivory dashboard, scan the QR code, and just start typing — no commands, no menus.'
echo

curl -sS "${API}/getWebhookInfo"
echo
