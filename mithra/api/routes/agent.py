"""
/api/v1/agent/* routes.

Handles inbound agent requests from Home Assistant (conversation agent),
direct API callers, and internal services.

POST /api/v1/agent/chat   — submit a text or voice-transcript request
GET  /api/v1/agent/status — return last-action status for the caller's user_id
"""

from fastapi import APIRouter

router = APIRouter()


# Route handlers are implemented in Phase 0 step 20 (api/gateway.py).
