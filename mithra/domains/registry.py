from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mithra.domains.base import Domain
    from mithra.domains.bundle import ToolBundle, ToolMetadata
    from mithra.domains.context import DomainContext


class DomainRegistry:
    """Central registry for all domain plugins.

    Bootstrap populates it at startup; LangGraph agent queries it per request.
    Indexed by tool name so AutononomyEngine can look up ToolMetadata by name.
    """

    def __init__(self) -> None:
        self._domains: dict[str, "Domain"] = {}
        self._tool_metadata: dict[str, "ToolMetadata"] = {}

    def register(self, domain: "Domain") -> None:
        """Register a domain. Idempotent — re-registration replaces previous."""
        # Remove stale metadata for this domain's tools
        stale = [n for n, m in self._tool_metadata.items() if m.domain_id == domain.domain_id]
        for name in stale:
            del self._tool_metadata[name]
        self._domains[domain.domain_id] = domain

    def get(self, domain_id: str) -> "Domain | None":
        return self._domains.get(domain_id)

    def get_tools(
        self,
        domain_ids: list[str],
        context: "DomainContext",
    ) -> list["ToolBundle"]:
        """Return tools for the requested domains (used by LangGraph per request).

        Also updates the tool metadata index so AutononomyEngine can look up
        PermissionTier by tool name without needing a DomainContext.
        """
        bundles: list["ToolBundle"] = []
        for domain_id in domain_ids:
            domain = self._domains.get(domain_id)
            if domain is None:
                continue
            domain_bundles = domain.get_tools(context)
            for bundle in domain_bundles:
                self._tool_metadata[bundle.metadata.name] = bundle.metadata
            bundles.extend(domain_bundles)
        return bundles

    def get_tool_metadata(self, tool_name: str) -> "ToolMetadata | None":
        """Return ToolMetadata for a tool name (used by AutononomyEngine)."""
        return self._tool_metadata.get(tool_name)

    def get_all_domain_ids(self) -> list[str]:
        return list(self._domains.keys())

    def get_event_subscribers(self, event_type: str) -> list["Domain"]:
        """Return domains subscribed to this event type."""
        return [
            domain
            for domain in self._domains.values()
            if event_type in domain.get_event_subscriptions()
        ]

    def health(self) -> dict:
        """Status of all registered domains."""
        return {
            "status": "ok",
            "registered_domains": list(self._domains.keys()),
            "domain_count": len(self._domains),
            "indexed_tools": list(self._tool_metadata.keys()),
        }
