"""Tests for DomainRegistry — register, get, get_tools, events, health."""

from __future__ import annotations

import pytest

from mithra.domains.bundle import PermissionTier
from mithra.domains.registry import DomainRegistry
from mithra.domains.result import DomainEvent
from tests.unit.domains.conftest import TestDomain, _FakeTool
from mithra.domains.bundle import ToolBundle, ToolMetadata
from mithra.domains.result import ToolResult


class TestDomainRegistryRegister:
    def test_register_and_get(self, test_domain):
        registry = DomainRegistry()
        registry.register(test_domain)
        assert registry.get("test") is test_domain

    def test_get_nonexistent_returns_none(self):
        registry = DomainRegistry()
        assert registry.get("nonexistent") is None

    def test_register_replaces_previous(self, test_domain, domain_context):
        registry = DomainRegistry()
        registry.register(test_domain)
        new_domain = TestDomain(domain_id="test", display_name="New Test")
        registry.register(new_domain)
        assert registry.get("test") is new_domain

    def test_get_all_domain_ids(self):
        registry = DomainRegistry()
        registry.register(TestDomain(domain_id="time"))
        registry.register(TestDomain(domain_id="money"))
        ids = registry.get_all_domain_ids()
        assert "time" in ids
        assert "money" in ids
        assert len(ids) == 2


class TestDomainRegistryGetTools:
    def test_get_tools_returns_bundles(self, test_domain, domain_context):
        registry = DomainRegistry()
        registry.register(test_domain)
        bundles = registry.get_tools(["test"], domain_context)
        assert len(bundles) == 2

    def test_get_tools_unknown_domain_skipped(self, domain_context):
        registry = DomainRegistry()
        bundles = registry.get_tools(["nonexistent"], domain_context)
        assert bundles == []

    def test_get_tools_multiple_domains(self, domain_context):
        registry = DomainRegistry()
        time_domain = TestDomain(domain_id="time")
        money_domain = TestDomain(domain_id="money")
        registry.register(time_domain)
        registry.register(money_domain)
        bundles = registry.get_tools(["time", "money"], domain_context)
        # Both empty (no tools added to these test domains) — no error
        assert isinstance(bundles, list)

    def test_get_tools_indexes_metadata(self, test_domain, domain_context):
        registry = DomainRegistry()
        registry.register(test_domain)
        registry.get_tools(["test"], domain_context)
        meta = registry.get_tool_metadata("test_read_data")
        assert meta is not None
        assert meta.domain_id == "test"
        assert meta.permission == PermissionTier.READ

    def test_get_tool_metadata_unknown_returns_none(self):
        registry = DomainRegistry()
        assert registry.get_tool_metadata("unknown_tool") is None

    def test_reregistration_clears_stale_metadata(self, test_domain, domain_context):
        registry = DomainRegistry()
        registry.register(test_domain)
        registry.get_tools(["test"], domain_context)
        assert registry.get_tool_metadata("test_read_data") is not None

        # Re-register with different tools
        new_tool = _FakeTool("test_new_action")
        new_meta = ToolMetadata(
            name="test_new_action",
            domain_id="test",
            permission=PermissionTier.READ,
            is_reversible=False,
            autonomy_level=3,
        )
        new_domain = TestDomain(
            domain_id="test",
            tools=[ToolBundle(tool=new_tool, metadata=new_meta)],
        )
        registry.register(new_domain)
        # Old metadata should be gone
        assert registry.get_tool_metadata("test_read_data") is None


class TestDomainRegistryEvents:
    def test_get_event_subscribers(self, test_domain):
        registry = DomainRegistry()
        registry.register(test_domain)
        subscribers = registry.get_event_subscribers("appointment_created")
        assert test_domain in subscribers

    def test_no_subscribers_for_unknown_event(self, test_domain):
        registry = DomainRegistry()
        registry.register(test_domain)
        subscribers = registry.get_event_subscribers("nonexistent_event")
        assert subscribers == []

    def test_multiple_subscribers(self):
        registry = DomainRegistry()
        d1 = TestDomain(domain_id="money", event_subscriptions=["appointment_created"])
        d2 = TestDomain(domain_id="time", event_subscriptions=["appointment_created", "expense_recorded"])
        registry.register(d1)
        registry.register(d2)
        subs = registry.get_event_subscribers("appointment_created")
        assert d1 in subs
        assert d2 in subs


class TestDomainRegistryHealth:
    def test_health_empty_registry(self):
        registry = DomainRegistry()
        h = registry.health()
        assert h["status"] == "ok"
        assert h["domain_count"] == 0
        assert h["registered_domains"] == []

    def test_health_with_domains(self, test_domain, domain_context):
        registry = DomainRegistry()
        registry.register(test_domain)
        registry.get_tools(["test"], domain_context)
        h = registry.health()
        assert h["domain_count"] == 1
        assert "test" in h["registered_domains"]
        assert "test_read_data" in h["indexed_tools"]
