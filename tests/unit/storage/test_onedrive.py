"""Unit tests for OneDriveAdapter.

All Graph API calls are intercepted by MockGraphClient — no real network calls.
Tests cover: cache hit/miss, write queue lifecycle, attempt tracking, dead letter,
startup replay, list, delete, file ops, and health.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from mithra.storage.adapter import StorageRecord
from mithra.storage.onedrive import OneDriveAdapter
from tests.conftest import MockGraphClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_graph() -> MockGraphClient:
    return MockGraphClient()


@pytest.fixture
def adapter(tmp_path, mock_graph) -> OneDriveAdapter:
    return OneDriveAdapter(
        cache_db_path=str(tmp_path / "cache.db"),
        graph_client=mock_graph,
    )


def _rec(
    key: str = "k1",
    domain: str = "food",
    layer: str = "facts",
    user_id: str = "person_a",
    data: dict | None = None,
) -> StorageRecord:
    return StorageRecord(
        domain=domain,
        key=key,
        layer=layer,
        data=data or {"value": 42},
        user_id=user_id,
        version=1,
        updated_at="",
    )


# ---------------------------------------------------------------------------
# read() — cache hit
# ---------------------------------------------------------------------------


class TestReadCacheHit:
    def test_cache_hit_does_not_call_graph_api(self, adapter, mock_graph):
        adapter.write(_rec())
        mock_graph.call_log.clear()

        adapter.read("food", "k1", "facts", "person_a")

        graph_reads = [m for m in mock_graph.graph_call_methods if m == "get_record"]
        assert graph_reads == [], "Cache hit must not call Graph API"

    def test_cache_hit_returns_correct_data(self, adapter):
        adapter.write(_rec(data={"name": "rice"}))
        rec = adapter.read("food", "k1", "facts", "person_a")
        assert rec is not None
        assert rec.data["name"] == "rice"


# ---------------------------------------------------------------------------
# read() — cache miss
# ---------------------------------------------------------------------------


class TestReadCacheMiss:
    def test_cache_miss_calls_graph_api(self, adapter, mock_graph):
        # Put a record directly in the mock graph (bypassing cache)
        path = adapter._record_path("food", "k1", "facts", "person_a")
        mock_graph._records[path] = {
            "domain": "food", "key": "k1", "layer": "facts", "user_id": "person_a",
            "data": {"source": "graph"}, "version": 1, "updated_at": "2026-01-01T00:00:00+00:00",
            "content_type": "record",
        }

        rec = adapter.read("food", "k1", "facts", "person_a")

        assert "get_record" in mock_graph.graph_call_methods
        assert rec is not None
        assert rec.data["source"] == "graph"

    def test_cache_miss_populates_cache(self, adapter, mock_graph):
        path = adapter._record_path("food", "k1", "facts", "person_a")
        mock_graph._records[path] = {
            "domain": "food", "key": "k1", "layer": "facts", "user_id": "person_a",
            "data": {"cached": True}, "version": 1, "updated_at": "2026-01-01T00:00:00+00:00",
            "content_type": "record",
        }
        adapter.read("food", "k1", "facts", "person_a")
        mock_graph.call_log.clear()

        # Second read must come from cache
        rec = adapter.read("food", "k1", "facts", "person_a")
        assert "get_record" not in mock_graph.graph_call_methods
        assert rec.data["cached"] is True

    def test_returns_none_when_not_found_anywhere(self, adapter):
        result = adapter.read("food", "ghost", "facts", "person_a")
        assert result is None


# ---------------------------------------------------------------------------
# write() — cache and queue
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_returns_record_immediately(self, adapter, mock_graph):
        mock_graph.fail_on = "put_record"   # Graph API will fail
        rec = adapter.write(_rec())
        assert rec is not None              # must return without waiting for Graph API
        assert rec.version == 1

    def test_write_increments_version(self, adapter):
        adapter.write(_rec())
        rec = adapter.write(_rec())
        assert rec.version == 2

    def test_write_item_appears_in_queue(self, adapter):
        adapter.write(_rec())
        count = adapter._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0]
        assert count == 1

    def test_write_item_removed_after_successful_sync(self, adapter):
        adapter.write(_rec())
        adapter._process_pending()
        count = adapter._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0]
        assert count == 0

    def test_successful_sync_calls_graph_put(self, adapter, mock_graph):
        adapter.write(_rec())
        adapter._process_pending()
        assert "put_record" in mock_graph.graph_call_methods


# ---------------------------------------------------------------------------
# write queue — failure and retry
# ---------------------------------------------------------------------------


class TestWriteQueueRetry:
    def test_attempts_incremented_on_failure(self, adapter, mock_graph):
        mock_graph.fail_on = "put_record"
        adapter.write(_rec())
        adapter._process_pending()

        attempts = adapter._conn.execute(
            "SELECT attempts FROM write_queue"
        ).fetchone()[0]
        assert attempts == 1

    def test_item_moved_to_dead_letter_after_max_attempts(self, adapter, mock_graph):
        mock_graph.fail_on = "put_record"
        adapter.write(_rec())

        # Process 3 times — each increments attempts, 3rd moves to dead_letter
        adapter._process_pending()
        adapter._process_pending()
        adapter._process_pending()

        queue_count = adapter._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0]
        dead_count = adapter._conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0]
        assert queue_count == 0
        assert dead_count == 1

    def test_dead_letter_contains_error_message(self, adapter, mock_graph):
        mock_graph.fail_on = "put_record"
        adapter.write(_rec())
        for _ in range(3):
            adapter._process_pending()

        row = adapter._conn.execute("SELECT error FROM dead_letter").fetchone()
        assert row is not None
        assert "Mock graph failure" in row["error"]

    def test_item_with_one_failure_retried_on_next_process(self, adapter, mock_graph):
        mock_graph.fail_on = "put_record"
        adapter.write(_rec())
        adapter._process_pending()   # fails, attempts=1

        mock_graph.fail_on = None    # restore success
        adapter._process_pending()   # should succeed now

        count = adapter._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# startup queue replay
# ---------------------------------------------------------------------------


class TestStartupQueueReplay:
    def test_pending_items_processed_after_reinit(self, tmp_path):
        """Items left in write_queue survive process restart and are processed."""
        db_path = str(tmp_path / "cache.db")
        mock1 = MockGraphClient()

        # First adapter: write, do NOT flush
        adapter1 = OneDriveAdapter(cache_db_path=db_path, graph_client=mock1)
        adapter1.write(_rec())
        assert adapter1._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0] == 1

        # Second adapter with same DB: items should still be there
        mock2 = MockGraphClient()
        adapter2 = OneDriveAdapter(cache_db_path=db_path, graph_client=mock2)
        count = adapter2._conn.execute("SELECT COUNT(*) FROM write_queue").fetchone()[0]
        assert count == 1, "Queue items must survive across adapter instances"

        # Processing them should succeed
        adapter2._process_pending()
        assert "put_record" in mock2.graph_call_methods


# ---------------------------------------------------------------------------
# cache TTL expiry
# ---------------------------------------------------------------------------


class TestCacheTTL:
    def test_expired_entry_triggers_graph_refresh(self, adapter, mock_graph):
        adapter.write(_rec(data={"old": True}))

        # Backdate cached_at to force expiry
        adapter._conn.execute("UPDATE records SET cached_at = cached_at - 31")
        adapter._conn.commit()

        # Set fresh data in mock graph
        path = adapter._record_path("food", "k1", "facts", "person_a")
        mock_graph._records[path] = {
            "domain": "food", "key": "k1", "layer": "facts", "user_id": "person_a",
            "data": {"fresh": True}, "version": 2, "updated_at": "2026-01-01T00:00:00+00:00",
            "content_type": "record",
        }

        rec = adapter.read("food", "k1", "facts", "person_a")

        assert "get_record" in mock_graph.graph_call_methods
        assert rec is not None
        assert rec.data.get("fresh") is True

    def test_fresh_entry_does_not_trigger_graph_call(self, adapter, mock_graph):
        adapter.write(_rec())
        mock_graph.call_log.clear()

        adapter.read("food", "k1", "facts", "person_a")

        assert "get_record" not in mock_graph.graph_call_methods


# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------


class TestList:
    def test_list_returns_all_matching_records(self, adapter):
        adapter.write(_rec(key="a", data={"v": 1}))
        adapter.write(_rec(key="b", data={"v": 2}))
        adapter.write(_rec(key="c", domain="home"))  # different domain

        records = adapter.list("food", "facts", "person_a")
        keys = {r.key for r in records}
        assert keys == {"a", "b"}

    def test_list_returns_empty_for_unknown_scope(self, adapter):
        assert adapter.list("money", "facts", "person_b") == []

    def test_list_excludes_expired_entries(self, adapter):
        adapter.write(_rec(key="a"))
        adapter._conn.execute("UPDATE records SET cached_at = cached_at - 31")
        adapter._conn.commit()

        records = adapter.list("food", "facts", "person_a")
        assert records == []


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_from_cache(self, adapter):
        adapter.write(_rec())
        adapter.delete("food", "k1", "person_a")
        assert adapter._cache_get("food", "k1", "facts", "person_a") is None

    def test_delete_returns_true_when_existed(self, adapter):
        adapter.write(_rec())
        assert adapter.delete("food", "k1", "person_a") is True

    def test_delete_returns_false_when_not_found(self, adapter):
        assert adapter.delete("food", "ghost", "person_a") is False

    def test_delete_enqueues_graph_delete(self, adapter):
        adapter.write(_rec())
        adapter.delete("food", "k1", "person_a")
        count = adapter._conn.execute(
            "SELECT COUNT(*) FROM write_queue WHERE op='delete'"
        ).fetchone()[0]
        assert count == 1

    def test_delete_graph_called_after_process(self, adapter, mock_graph):
        adapter.write(_rec())
        adapter.delete("food", "k1", "person_a")
        mock_graph.call_log.clear()
        adapter._process_pending()
        assert "delete_record" in mock_graph.graph_call_methods


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_auth_valid_true_with_mock(self, adapter):
        h = adapter.health()
        assert h["auth_valid"] is True

    def test_health_auth_valid_false_when_connectivity_fails(self, adapter, mock_graph):
        mock_graph.fail_on = "check_connectivity"
        h = adapter.health()
        assert h["auth_valid"] is False
        assert h["status"] == "degraded"

    def test_health_contains_required_fields(self, adapter):
        h = adapter.health()
        for field in ("status", "cache_record_count", "queue_depth", "last_sync_at", "auth_valid"):
            assert field in h, f"Missing field: {field}"

    def test_health_queue_depth_reflects_pending(self, adapter, mock_graph):
        mock_graph.fail_on = "put_record"
        adapter.write(_rec(key="a"))
        adapter.write(_rec(key="b"))
        h = adapter.health()
        assert h["queue_depth"] == 2

    def test_health_cache_count_after_writes(self, adapter):
        adapter.write(_rec(key="a"))
        adapter.write(_rec(key="b"))
        h = adapter.health()
        assert h["cache_record_count"] == 2

    def test_health_connectivity_cached_for_30s(self, adapter, mock_graph):
        """check_connectivity() is called once and result is reused within 30s."""
        adapter.health()
        adapter.health()
        calls = mock_graph.graph_call_methods.count("check_connectivity")
        assert calls == 1

    def test_health_connectivity_rechecked_after_ttl(self, adapter, mock_graph):
        """check_connectivity() is called again once the 30s window expires."""
        adapter.health()
        # Expire the cache by backdating the timestamp
        adapter._last_connectivity_check -= 31
        adapter.health()
        calls = mock_graph.graph_call_methods.count("check_connectivity")
        assert calls == 2


# ---------------------------------------------------------------------------
# list() — cold cache fallback
# ---------------------------------------------------------------------------


class TestListColdCache:
    def test_list_falls_back_to_graph_when_cache_empty(self, adapter, mock_graph):
        """list() calls Graph API when the cache has no matching records."""
        path_a = adapter._record_path("food", "a", "facts", "person_a")
        path_b = adapter._record_path("food", "b", "facts", "person_a")
        mock_graph._records[path_a] = {
            "domain": "food", "key": "a", "layer": "facts", "user_id": "person_a",
            "data": {"v": 1}, "version": 1, "updated_at": "2026-01-01T00:00:00+00:00",
            "content_type": "record",
        }
        mock_graph._records[path_b] = {
            "domain": "food", "key": "b", "layer": "facts", "user_id": "person_a",
            "data": {"v": 2}, "version": 1, "updated_at": "2026-01-01T00:00:00+00:00",
            "content_type": "record",
        }

        records = adapter.list("food", "facts", "person_a")

        assert "list_records" in mock_graph.graph_call_methods
        assert {r.key for r in records} == {"a", "b"}

    def test_list_populates_cache_after_graph_fetch(self, adapter, mock_graph):
        """Records fetched from Graph API are stored in cache for subsequent reads."""
        path = adapter._record_path("food", "k1", "facts", "person_a")
        mock_graph._records[path] = {
            "domain": "food", "key": "k1", "layer": "facts", "user_id": "person_a",
            "data": {"loaded": True}, "version": 1, "updated_at": "2026-01-01T00:00:00+00:00",
            "content_type": "record",
        }
        adapter.list("food", "facts", "person_a")
        mock_graph.call_log.clear()

        # Second list must hit cache, not Graph API
        records = adapter.list("food", "facts", "person_a")
        assert "list_records" not in mock_graph.graph_call_methods
        assert records[0].data["loaded"] is True

    def test_list_after_restart_fetches_from_graph(self, tmp_path):
        """After process restart (cold cache), list() falls back to Graph API."""
        db_path = str(tmp_path / "cache.db")
        mock1 = MockGraphClient()
        adapter1 = OneDriveAdapter(cache_db_path=db_path, graph_client=mock1)
        adapter1.write(_rec(key="k1", data={"persisted": True}))
        # Flush so Graph API has the record
        adapter1._process_pending()

        # New adapter instance — fresh empty cache
        mock2 = MockGraphClient()
        adapter2 = OneDriveAdapter(cache_db_path=db_path, graph_client=mock2)
        # Clear the carried-over cache rows (simulate cold start)
        adapter2._conn.execute("DELETE FROM records")
        adapter2._conn.commit()

        # Seed mock2 graph with the record that was "uploaded"
        path = adapter2._record_path("food", "k1", "facts", "person_a")
        mock2._records[path] = {
            "domain": "food", "key": "k1", "layer": "facts", "user_id": "person_a",
            "data": {"persisted": True}, "version": 1,
            "updated_at": "2026-01-01T00:00:00+00:00", "content_type": "record",
        }

        records = adapter2.list("food", "facts", "person_a")
        assert "list_records" in mock2.graph_call_methods
        assert len(records) == 1
        assert records[0].data["persisted"] is True

    def test_list_returns_cache_when_warm(self, adapter, mock_graph):
        """list() does NOT call Graph API when cache has fresh results."""
        adapter.write(_rec(key="a"))
        mock_graph.call_log.clear()

        adapter.list("food", "facts", "person_a")

        assert "list_records" not in mock_graph.graph_call_methods


# ---------------------------------------------------------------------------
# write() — ContextCache invalidation
# ---------------------------------------------------------------------------


class TestWriteContextCacheInvalidation:
    def test_write_calls_invalidate_domain_when_cache_set(self, tmp_path, mock_graph):
        """write() calls cache.invalidate_domain() when a ContextCache is injected."""
        from unittest.mock import MagicMock
        mock_ctx_cache = MagicMock()

        adapter = OneDriveAdapter(
            cache_db_path=str(tmp_path / "cache.db"),
            graph_client=mock_graph,
            cache=mock_ctx_cache,
        )
        adapter.write(_rec(domain="food"))

        mock_ctx_cache.invalidate_domain.assert_called_once_with("food")

    def test_write_does_not_fail_without_cache(self, adapter):
        """write() works normally when no ContextCache is injected."""
        rec = adapter.write(_rec())
        assert rec.version == 1
