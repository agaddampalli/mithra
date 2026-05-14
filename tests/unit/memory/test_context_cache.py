"""Structural and behavioural tests for mithra/memory/context_cache.py."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from mithra.memory.context_cache import ContextCache
from mithra.routing.domain_router import ContextSnapshot, DomainBrief, DomainState


@pytest.fixture
def cache(tmp_path):
    return ContextCache(str(tmp_path / "cache.db"))


@pytest.fixture
def mc():
    """In-memory cache — no file I/O, no tmp_path needed."""
    return ContextCache(":memory:")


def _snapshot(user_id="person_a", device_id="d1", domains=("food",)) -> ContextSnapshot:
    return ContextSnapshot(
        user_id=user_id,
        device_id=device_id,
        priority_domains=[DomainState(domain=d) for d in domains],
        assembled_at="2026-01-01T00:00:00+00:00",
    )


def _rich_snapshot() -> ContextSnapshot:
    """Snapshot with all fields populated — used for pickle roundtrip tests."""
    return ContextSnapshot(
        user_id="person_a",
        device_id="phone",
        priority_domains=[
            DomainState(
                domain="food",
                user_id="person_a",
                facts={"pantry_count": 5},
                recent_actions=[{"action": "bought_rice"}],
                priority_score=3.0,
            )
        ],
        domain_briefs=[
            DomainBrief(domain="home", summary="All good", priority_score=1.0)
        ],
        known_pii={"name": "Alice"},
        assembled_at="2026-05-13T12:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Scaffold tests (original 16) — must remain passing
# ---------------------------------------------------------------------------


class TestContextCacheConstants:
    def test_ttl_seconds_is_30(self):
        assert ContextCache.TTL_SECONDS == 30


class TestContextCacheInterface:
    def test_has_get_method(self):
        assert hasattr(ContextCache, "get")

    def test_has_set_method(self):
        assert hasattr(ContextCache, "set")

    def test_has_invalidate_domain_method(self):
        assert hasattr(ContextCache, "invalidate_domain")

    def test_has_invalidate_user_method(self):
        assert hasattr(ContextCache, "invalidate_user")


class TestContextCacheBehaviour:
    def test_get_returns_none_for_missing_entry(self, cache):
        assert cache.get("person_a", "device-1") is None

    def test_set_and_get_round_trip(self, cache):
        snap = _snapshot()
        cache.set(snap)
        retrieved = cache.get("person_a", "d1")
        assert retrieved is not None
        assert retrieved.user_id == "person_a"

    def test_set_overwrites_existing_entry(self, cache):
        cache.set(_snapshot(domains=("food",)))
        cache.set(_snapshot(domains=("home",)))
        retrieved = cache.get("person_a", "d1")
        domain_names = [d.domain for d in retrieved.priority_domains]
        assert "home" in domain_names

    def test_invalidate_domain_returns_int(self, cache):
        cache.set(_snapshot(domains=("food",)))
        count = cache.invalidate_domain("food")
        assert isinstance(count, int)
        assert count == 1

    def test_invalidate_domain_removes_matching_entries(self, cache):
        cache.set(_snapshot(domains=("food",)))
        cache.invalidate_domain("food")
        assert cache.get("person_a", "d1") is None

    def test_invalidate_domain_does_not_remove_non_matching(self, cache):
        cache.set(_snapshot(user_id="person_a", device_id="d1", domains=("food",)))
        cache.set(_snapshot(user_id="person_b", device_id="d2", domains=("home",)))
        count = cache.invalidate_domain("food")
        assert count == 1
        assert cache.get("person_b", "d2") is not None

    def test_invalidate_domain_returns_zero_when_nothing_matches(self, cache):
        cache.set(_snapshot(domains=("food",)))
        count = cache.invalidate_domain("health")
        assert count == 0

    def test_invalidate_user_returns_int(self, cache):
        cache.set(_snapshot())
        count = cache.invalidate_user("person_a")
        assert isinstance(count, int)
        assert count == 1

    def test_invalidate_user_removes_all_user_entries(self, cache):
        cache.set(_snapshot(user_id="person_a", device_id="d1"))
        cache.set(_snapshot(user_id="person_a", device_id="d2"))
        count = cache.invalidate_user("person_a")
        assert count == 2
        assert cache.get("person_a", "d1") is None
        assert cache.get("person_a", "d2") is None

    def test_invalidate_user_does_not_remove_other_users(self, cache):
        cache.set(_snapshot(user_id="person_a", device_id="d1"))
        cache.set(_snapshot(user_id="person_b", device_id="d2"))
        cache.invalidate_user("person_a")
        assert cache.get("person_b", "d2") is not None

    def test_expired_entry_not_returned(self, cache):
        """Simulate expiry by manipulating cached_at directly via a second connection."""
        import sqlite3

        snap = _snapshot()
        cache.set(snap)
        conn = sqlite3.connect(cache.db_path)
        conn.execute("UPDATE context_cache SET cached_at = ?", (time.time() - 31,))
        conn.commit()
        conn.close()
        assert cache.get("person_a", "d1") is None


# ---------------------------------------------------------------------------
# New tests — pickle roundtrip, TTL via patch, isolation, health
# ---------------------------------------------------------------------------


class TestPickleRoundtrip:
    def test_set_get_preserves_all_fields(self, mc):
        snap = _rich_snapshot()
        mc.set(snap)
        retrieved = mc.get("person_a", "phone")

        assert retrieved is not None
        assert retrieved.user_id == "person_a"
        assert retrieved.device_id == "phone"
        assert retrieved.assembled_at == "2026-05-13T12:00:00+00:00"
        assert retrieved.known_pii == {"name": "Alice"}
        pd = retrieved.priority_domains[0]
        assert pd.domain == "food"
        assert pd.facts == {"pantry_count": 5}
        assert pd.recent_actions == [{"action": "bought_rice"}]
        assert pd.priority_score == 3.0
        db = retrieved.domain_briefs[0]
        assert db.domain == "home"
        assert db.summary == "All good"

    def test_set_overwrites_same_key(self, mc):
        mc.set(_snapshot(domains=("food",)))
        mc.set(_snapshot(domains=("money",)))
        retrieved = mc.get("person_a", "d1")
        names = [d.domain for d in retrieved.priority_domains]
        assert names == ["money"]
        assert "food" not in names


class TestTTLBehaviour:
    def test_get_returns_none_for_missing_key(self, mc):
        assert mc.get("nobody", "no-device") is None

    def test_expired_entry_returns_none(self, mc):
        base_time = 1_000_000.0
        with patch("mithra.memory.context_cache.time.time", return_value=base_time):
            mc.set(_snapshot())
        with patch("mithra.memory.context_cache.time.time", return_value=base_time + 31):
            assert mc.get("person_a", "d1") is None

    def test_fresh_entry_not_removed_by_ttl(self, mc):
        base_time = 1_000_000.0
        with patch("mithra.memory.context_cache.time.time", return_value=base_time):
            mc.set(_snapshot())
        with patch("mithra.memory.context_cache.time.time", return_value=base_time + 29):
            assert mc.get("person_a", "d1") is not None

    def test_expired_row_deleted_on_get(self, mc):
        """get() must DELETE the expired row so health() count drops."""
        base_time = 1_000_000.0
        with patch("mithra.memory.context_cache.time.time", return_value=base_time):
            mc.set(_snapshot())
        assert mc.health()["cached_count"] == 1
        with patch("mithra.memory.context_cache.time.time", return_value=base_time + 31):
            mc.get("person_a", "d1")
        assert mc.health()["cached_count"] == 0


class TestInvalidation:
    def test_invalidate_domain_removes_both_users_with_same_domain(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1", domains=("food",)))
        mc.set(_snapshot(user_id="person_b", device_id="d2", domains=("food",)))
        count = mc.invalidate_domain("food")
        assert count == 2
        assert mc.get("person_a", "d1") is None
        assert mc.get("person_b", "d2") is None

    def test_invalidate_domain_spares_other_domains(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1", domains=("food",)))
        mc.set(_snapshot(user_id="person_b", device_id="d2", domains=("home",)))
        mc.invalidate_domain("food")
        assert mc.get("person_b", "d2") is not None

    def test_invalidate_domain_count_is_correct(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1", domains=("food",)))
        mc.set(_snapshot(user_id="person_b", device_id="d2", domains=("food",)))
        mc.set(_snapshot(user_id="person_a", device_id="tablet", domains=("home",)))
        count = mc.invalidate_domain("food")
        assert count == 2

    def test_invalidate_domain_no_match_returns_zero(self, mc):
        mc.set(_snapshot(domains=("food",)))
        assert mc.invalidate_domain("health") == 0

    def test_invalidate_user_removes_all_devices(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1"))
        mc.set(_snapshot(user_id="person_a", device_id="d2"))
        count = mc.invalidate_user("person_a")
        assert count == 2
        assert mc.get("person_a", "d1") is None
        assert mc.get("person_a", "d2") is None

    def test_invalidate_user_spares_other_users(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1"))
        mc.set(_snapshot(user_id="person_b", device_id="d2"))
        mc.invalidate_user("person_a")
        assert mc.get("person_b", "d2") is not None

    def test_invalidate_user_count_is_correct(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1"))
        mc.set(_snapshot(user_id="person_a", device_id="d2"))
        mc.set(_snapshot(user_id="person_b", device_id="d3"))
        count = mc.invalidate_user("person_a")
        assert count == 2

    def test_domain_in_briefs_also_triggers_invalidation(self, mc):
        """domain_briefs domains must be included in invalidation."""
        snap = ContextSnapshot(
            user_id="person_a",
            device_id="d1",
            priority_domains=[DomainState(domain="food")],
            domain_briefs=[DomainBrief(domain="home", summary="ok", priority_score=0.5)],
        )
        mc.set(snap)
        count = mc.invalidate_domain("home")
        assert count == 1
        assert mc.get("person_a", "d1") is None


class TestHealth:
    def test_health_returns_ok(self, mc):
        h = mc.health()
        assert h["status"] == "ok"
        assert h["cached_count"] == 0
        assert "db_path" in h

    def test_health_count_reflects_entries(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1"))
        mc.set(_snapshot(user_id="person_b", device_id="d2"))
        assert mc.health()["cached_count"] == 2

    def test_health_count_decreases_after_invalidation(self, mc):
        mc.set(_snapshot(user_id="person_a", device_id="d1"))
        mc.set(_snapshot(user_id="person_b", device_id="d2"))
        mc.invalidate_domain("food")
        assert mc.health()["cached_count"] == 0
