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

curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WEBHOOK_URL}" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
  -d 'allowed_updates=["message"]' \
  -d "drop_pending_updates=true"
echo
curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
echo
