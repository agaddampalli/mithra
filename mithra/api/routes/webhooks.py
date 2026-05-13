"""
/api/v1/webhook/* routes — inbound webhook endpoints.

POST /api/v1/webhook/telegram   — Telegram Bot API webhook receiver
                                  (TELEGRAM_WEBHOOK_SECRET validated in header)

Webhook is the production mode for Telegram; polling is the automatic fallback
when the Cloudflare tunnel is unreachable.

All webhook payloads are forwarded to TelegramInputAdapter.normalise() and
dispatched through the standard gateway lifecycle.
"""

from fastapi import APIRouter

router = APIRouter()


# Route handlers are implemented in Phase 0 step 29 (gateway.py).
