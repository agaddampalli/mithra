from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DashboardAlert:
    """An alert or warning surfaced on the dashboard (ADR 16.2)."""

    type: str       # "overdue" | "inbox_overload" | "routines_pending" | etc.
    message: str
    severity: str = "low"  # "high" | "normal" | "low"


@dataclass
class DashboardItem:
    """A single item rendered in the Home Assistant Lovelace dashboard (ADR 16.2)."""

    id: str
    title: str
    domain: str = ""          # domain that owns this item e.g. "tasks"
    subtitle: str | None = None
    icon: str = ""            # HA MDI icon name e.g. "mdi:calendar"
    priority: str = "normal"  # "high" | "normal" | "low"
    scope: str = "household"  # "household" | "personal"
    action: str | None = None # tool name to call on tap e.g. "task_complete"


@dataclass
class DashboardData:
    """Standardized domain summary for HA Lovelace display (ADR 16.2).

    HA REST sensor polls GET /api/v1/dashboard every 5 minutes.
    Contract: additive only — no field removal without updating all domains.
    """

    domain: str                                             # domain_id
    summary: str                                            # e.g. "2 events today"
    items: list[DashboardItem] = field(default_factory=list)
    alerts: list = field(default_factory=list)              # list[DashboardAlert]; untyped for backward compat
    error: str | None = None                                # set on per-domain failure; caller never 500s
    updated_at: str = ""                                    # ISO8601; kept for backward compat
    quick_actions: list[dict] = field(default_factory=list) # kept for backward compat
