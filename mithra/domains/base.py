from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mithra.domains.bundle import ToolBundle
    from mithra.domains.context import DomainContext
    from mithra.domains.dashboard import DashboardData
    from mithra.domains.result import DomainEvent


class Domain(ABC):
    """Abstract base class for all Mithra domain plugins.

    One-way door: the interface contract here is fixed.
    Changing it requires updating all domains simultaneously.
    Domain code must never import other domain code — use events and context.
    """

    @property
    @abstractmethod
    def domain_id(self) -> str:
        """Unique identifier e.g. 'time', 'people'.
        Immutable. Used as the storage domain key and event source.
        Must match StorageAdapter domain conventions."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Semantic version e.g. '1.0.0'.
        Major bump signals a breaking tool interface change."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name e.g. 'Time & Calendar'."""

    @abstractmethod
    def get_tools(self, context: "DomainContext") -> list["ToolBundle"]:
        """Return tools for this domain bound to the given context.
        Max 8 tools per domain. Tools must be context-bound, not stateful."""

    @abstractmethod
    async def get_dashboard_data(
        self,
        user_id: str,
        household_id: str,
    ) -> "DashboardData":
        """Return standardized summary for HA Lovelace dashboard.
        Called every 5 minutes by HA REST sensor."""

    def get_context_keys(self) -> list[str]:
        """Memory domains to load for context assembly.
        Override if the domain needs cross-domain memory."""
        return [self.domain_id]

    def get_event_subscriptions(self) -> list[str]:
        """Domain event types this domain reacts to.
        Override to subscribe e.g. MoneyDomain returns ['appointment_created']."""
        return []

    async def handle_event(
        self,
        event: "DomainEvent",
        context: "DomainContext",
    ) -> None:
        """React to a domain event from another domain.
        Called AFTER autonomy confirmation — never if user rejects."""
