"""Tests for ToolBundle, ToolMetadata, and PermissionTier."""

from __future__ import annotations

import pytest

from mithra.domains.bundle import PermissionTier, ToolBundle, ToolMetadata


class TestPermissionTier:
    def test_all_tiers_exist(self):
        assert PermissionTier.READ.value == "read"
        assert PermissionTier.WRITE.value == "write"
        assert PermissionTier.DESTRUCTIVE.value == "destructive"

    def test_destructive_is_distinct(self):
        assert PermissionTier.DESTRUCTIVE != PermissionTier.WRITE
        assert PermissionTier.DESTRUCTIVE != PermissionTier.READ


class TestToolMetadata:
    def test_read_metadata(self, read_tool_bundle):
        meta = read_tool_bundle.metadata
        assert meta.name == "test_read_data"
        assert meta.domain_id == "test"
        assert meta.permission == PermissionTier.READ
        assert meta.is_reversible is False
        assert meta.autonomy_level == 3
        assert meta.event_emitted is None

    def test_write_metadata_with_event(self, write_tool_bundle):
        meta = write_tool_bundle.metadata
        assert meta.permission == PermissionTier.WRITE
        assert meta.is_reversible is True
        assert meta.event_emitted == "data_written"

    def test_destructive_metadata(self, destructive_tool_bundle):
        meta = destructive_tool_bundle.metadata
        assert meta.permission == PermissionTier.DESTRUCTIVE
        assert meta.is_reversible is False


class TestToolBundle:
    def test_bundle_has_tool_and_metadata(self, read_tool_bundle):
        assert read_tool_bundle.tool is not None
        assert read_tool_bundle.metadata is not None

    def test_bundle_metadata_name_matches_tool_name(self, read_tool_bundle):
        assert read_tool_bundle.metadata.name == read_tool_bundle.tool.name

    def test_tool_invoke(self, read_tool_bundle):
        result = read_tool_bundle.tool.invoke({})
        assert result.success is True
        assert "5" in result.summary or "record" in result.summary.lower()
