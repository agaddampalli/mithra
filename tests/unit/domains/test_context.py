"""Tests for DomainContext — frozen dataclass with injected services."""

from __future__ import annotations

import pytest

from mithra.domains.context import DomainContext


def test_domain_context_creation(domain_context):
    assert domain_context.user_id == "usr_ba6d9199"
    assert domain_context.household_id == "hh_b76fef08"
    assert domain_context.household_timezone == "America/Chicago"
    assert domain_context.channel == "telegram"
    assert domain_context.language == "en-US"
    assert domain_context.autonomy_override is None


def test_domain_context_is_frozen(domain_context):
    with pytest.raises((AttributeError, TypeError)):
        domain_context.user_id = "other"  # type: ignore[misc]


def test_domain_context_services_are_none_by_default(domain_context):
    assert domain_context.oauth_manager is None
    assert domain_context.storage_adapter is None
    assert domain_context.file_router is None
    assert domain_context.member_registry is None
    assert domain_context.file_search is None


def test_domain_context_with_autonomy_override():
    ctx = DomainContext(
        user_id="usr_1",
        household_id="hh_1",
        household_timezone="UTC",
        oauth_manager=None,
        storage_adapter=None,
        file_router=None,
        member_registry=None,
        file_search=None,
        request_id="req_1",
        channel="voice",
        language="en-US",
        autonomy_override=3,
    )
    assert ctx.autonomy_override == 3


def test_domain_context_request_id_maps_to_a2a(domain_context):
    # request_id is the A2A task_id — must be present
    assert domain_context.request_id == "req_test_001"
