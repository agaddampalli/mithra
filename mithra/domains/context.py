from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DomainContext:
    """Immutable request context injected into all domain tools.

    Frozen prevents tools from mutating shared state — tools must be pure
    functions relative to context. request_id maps to A2A task_id.
    """

    # Identity
    user_id: str
    household_id: str
    household_timezone: str

    # Services (injected, never imported by domains)
    oauth_manager: Any          # OAuthManager
    storage_adapter: Any        # StorageAdapter
    file_router: Any            # FileStorageRouter
    member_registry: Any        # MemberRegistry
    file_search: Any            # FileSearchService

    # Request metadata
    request_id: str             # UUID; maps to A2A task_id
    channel: str                # "telegram" | "voice" | "dashboard"
    language: str               # "en-US"

    # Autonomy override — set when user explicitly said "just do it"
    autonomy_override: int | None
