"""Shared fixtures for domain unit tests."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from mithra.domains.base import Domain
from mithra.domains.bundle import PermissionTier, ToolBundle, ToolMetadata
from mithra.domains.context import DomainContext
from mithra.domains.dashboard import DashboardData, DashboardItem
from mithra.domains.result import DomainEvent, ToolResult


# ---------------------------------------------------------------------------
# Minimal LangChain-compatible tool stub (no network required)
# ---------------------------------------------------------------------------

class _FakeToolSchema(BaseModel):
    query: str = ""


class _FakeTool:
    """Minimal BaseTool stand-in for testing — avoids LLM calls."""

    def __init__(self, name: str, result: ToolResult | None = None) -> None:
        self.name = name
        self._result = result or ToolResult.ok(f"Result from {name}")

    def invoke(self, tool_input: dict) -> ToolResult:
        return self._result

    args_schema = _FakeToolSchema


# ---------------------------------------------------------------------------
# TestDomain — concrete Domain for testing registry and agent wiring
# ---------------------------------------------------------------------------

class TestDomain(Domain):
    """Concrete Domain implementation used in tests."""

    def __init__(
        self,
        domain_id: str = "test",
        version: str = "1.0.0",
        display_name: str = "Test Domain",
        tools: list[ToolBundle] | None = None,
        event_subscriptions: list[str] | None = None,
    ) -> None:
        self._domain_id = domain_id
        self._version = version
        self._display_name = display_name
        self._tools = tools or []
        self._subscriptions = event_subscriptions or []
        self.handled_events: list[DomainEvent] = []

    @property
    def domain_id(self) -> str:
        return self._domain_id

    @property
    def version(self) -> str:
        return self._version

    @property
    def display_name(self) -> str:
        return self._display_name

    def get_tools(self, context: DomainContext) -> list[ToolBundle]:
        return self._tools

    async def get_dashboard_data(self, user_id: str, household_id: str) -> DashboardData:
        from datetime import datetime, timezone
        return DashboardData(
            domain=self._domain_id,
            updated_at=datetime.now(timezone.utc).isoformat(),
            summary=f"{self._display_name} is running",
            items=[
                DashboardItem(
                    id="item1",
                    title="Test item",
                    subtitle="subtitle",
                    icon="mdi:test",
                    priority="normal",
                    action=None,
                )
            ],
        )

    def get_event_subscriptions(self) -> list[str]:
        return self._subscriptions

    async def handle_event(self, event: DomainEvent, context: DomainContext) -> None:
        self.handled_events.append(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def domain_context() -> DomainContext:
    return DomainContext(
        user_id="usr_ba6d9199",
        household_id="hh_b76fef08",
        household_timezone="America/Chicago",
        oauth_manager=None,
        storage_adapter=None,
        file_router=None,
        member_registry=None,
        file_search=None,
        request_id="req_test_001",
        channel="telegram",
        language="en-US",
        autonomy_override=None,
    )


@pytest.fixture()
def read_tool_bundle() -> ToolBundle:
    tool = _FakeTool("test_read_data", ToolResult.ok("Read 5 records", data={"count": 5}))
    meta = ToolMetadata(
        name="test_read_data",
        domain_id="test",
        permission=PermissionTier.READ,
        is_reversible=False,
        autonomy_level=3,
    )
    return ToolBundle(tool=tool, metadata=meta)


@pytest.fixture()
def write_tool_bundle() -> ToolBundle:
    tool = _FakeTool("test_write_data", ToolResult.ok("Wrote record", undo_token="undo_abc"))
    meta = ToolMetadata(
        name="test_write_data",
        domain_id="test",
        permission=PermissionTier.WRITE,
        is_reversible=True,
        autonomy_level=1,
        event_emitted="data_written",
    )
    return ToolBundle(tool=tool, metadata=meta)


@pytest.fixture()
def destructive_tool_bundle() -> ToolBundle:
    tool = _FakeTool("test_delete_data", ToolResult.ok("Deleted record"))
    meta = ToolMetadata(
        name="test_delete_data",
        domain_id="test",
        permission=PermissionTier.DESTRUCTIVE,
        is_reversible=False,
        autonomy_level=1,
    )
    return ToolBundle(tool=tool, metadata=meta)


@pytest.fixture()
def test_domain(read_tool_bundle, write_tool_bundle) -> TestDomain:
    return TestDomain(
        domain_id="test",
        tools=[read_tool_bundle, write_tool_bundle],
        event_subscriptions=["appointment_created"],
    )
