"""
/api/v1/webhook/* routes — inbound webhook endpoints.

POST /api/v1/webhook/telegram  — Telegram Bot API updates

All routes are unauthenticated — each has its own validation mechanism
(Telegram uses X-Telegram-Bot-Api-Secret-Token).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

router = APIRouter()

_logger = logging.getLogger(__name__)
