from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DashboardItem:
    """A single item rendered in the Home Assistant Lovelace dashboard."""

    id: str
    title: str
    subtitle: str | None
    icon: str           # HA MDI icon name e.g. "mdi:calendar"
    priority: str       # "high" | "normal" | "low"
    action: str | None  # tool name to call on tap e.g. "calendar_read_events"


@dataclass
class DashboardData:
    """Standardized domain summary for HA Lovelace display.

    HA REST sensor polls GET /api/v1/dashboard/{domain_id} every 5 minutes.
    """

    domain: str                                        # domain_id
    updated_at: str                                    # ISO8601
    summary: str                                       # e.g. "2 events today"
    items: list[DashboardItem] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)    # urgent items
    quick_actions: list[dict] = field(default_factory=list)  # tappable actions
