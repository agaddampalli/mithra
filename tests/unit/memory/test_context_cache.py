"""Structural and behavioural tests for mithra/memory/context_cache.py."""

import inspect

import pytest

from mithra.memory.context_cache import ContextCache
from mithra.routing.domain_router import ContextSnapshot, DomainBrief, DomainState


@pytest.fixture
def cache(tmp_path):
    return ContextCache(str(tmp_path / "cache.db"))


def _snapshot(user_id="person_a", device_id="d1", domains=("food",)) -> ContextSnapshot:
    return ContextSnapshot(
        user_id=user_id,
        device_id=device_id,
        priority_domains=[DomainState(domain=d) for d in domains],
        assembled_at="2026-01-01T00:00:00+00:00",
    )


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
        # person_b's home snapshot must survive
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
        # Two devices for same user
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
        """Simulate expiry by manipulating cached_at directly."""
        import sqlite3, time
        snap = _snapshot()
        cache.set(snap)
        # Back-date the cached_at to force expiry
        conn = sqlite3.connect(cache.db_path)
        conn.execute("UPDATE context_cache SET cached_at = ?", (time.time() - 31,))
        conn.commit()
        conn.close()
        assert cache.get("person_a", "d1") is None
