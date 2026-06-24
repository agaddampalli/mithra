"""Tests for Domain ABC contract."""

from __future__ import annotations

import pytest

from mithra.domains.base import Domain
from mithra.domains.result import DomainEvent


class TestDomainABC:
    def test_cannot_instantiate_abstract_domain(self):
        with pytest.raises(TypeError):
            Domain()  # type: ignore[abstract]

    def test_concrete_domain_properties(self, test_domain):
        assert test_domain.domain_id == "test"
        assert test_domain.version == "1.0.0"
        assert test_domain.display_name == "Test Domain"

    def test_get_context_keys_default(self, test_domain):
        keys = test_domain.get_context_keys()
        assert keys == ["test"]

    def test_get_event_subscriptions(self, test_domain):
        subs = test_domain.get_event_subscriptions()
        assert "appointment_created" in subs

    def test_get_tools_returns_bundles(self, test_domain, domain_context):
        bundles = test_domain.get_tools(domain_context)
        assert len(bundles) == 2

    def test_get_dashboard_data(self, test_domain, domain_context):
        data = test_domain.get_dashboard_data(
            context=domain_context,
            scope_filter="household",
            viewer_user_id="usr_ba6d9199",
        )
        assert data.domain == "test"
        assert data.summary
        assert len(data.items) > 0

    @pytest.mark.asyncio
    async def test_handle_event_stores_event(self, test_domain, domain_context):
        evt = DomainEvent(
            event_type="appointment_created",
            source_domain="time",
            payload={"date": "2026-06-01"},
            user_id="usr_ba6d9199",
            household_id="hh_b76fef08",
            request_id="req_1",
        )
        await test_domain.handle_event(evt, domain_context)
        assert len(test_domain.handled_events) == 1
        assert test_domain.handled_events[0].event_type == "appointment_created"

    @pytest.mark.asyncio
    async def test_handle_event_base_implementation_is_noop(self, domain_context):
        """Base handle_event() is a no-op — domains override only when needed."""
        from tests.unit.domains.conftest import TestDomain
        domain = TestDomain(domain_id="noop")
        evt = DomainEvent(
            event_type="any_event",
            source_domain="other",
            payload={},
            user_id="u",
            household_id="h",
            request_id="r",
        )
        # Should not raise
        await domain.handle_event(evt, domain_context)
