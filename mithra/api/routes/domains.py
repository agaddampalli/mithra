"""
/api/v1/domains/* routes.

Exposes domain state and configuration to authorised clients.

GET  /api/v1/domains/           — list all registered domain plugins
GET  /api/v1/domains/{domain}   — return current state for a domain
POST /api/v1/domains/{domain}/trigger — manually trigger a domain refresh
"""

from fastapi import APIRouter

router = APIRouter()


# Route handlers are implemented once Phase 1 domain plugins are registered.
