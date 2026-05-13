"""
/api/v1/health/* routes — system health dashboard.

GET /api/v1/health        — returns status and last-run timestamp for every service
GET /api/v1/health/{svc}  — returns status for a single named service

Required by Phase 0 definition of done: all services must report status and
last-run timestamp through this endpoint.
"""

from fastapi import APIRouter

router = APIRouter()


# Route handlers are implemented in Phase 0 step 22 (dashboard route).
