"""Tests for DefaultDomainMemoryRouter (concrete implementation in domain_router.py).

The first 23 tests are the original scaffold tests that verify dataclass structure
and the abstract interface. All new tests for the concrete implementation are
grouped below in clearly labelled classes.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from mithra.memory.context_cache import ContextCache
from mithra.memory.structured_store import StructuredStore
from mithra.routing.domain_router import (
    AccessResult,
    AccessScope,
    ContextSnapshot,
    DefaultDomainMemoryRouter,
    DOMAIN_ACCESS_MAP,
    DomainBrief,
    DomainMemoryRouter,
    DomainState,
    LearnedPreference,
)
from mithra.storage.adapter import StorageAdapter, StorageRecord


# ---------------------------------------------------------------------------
# Helpers — fake storage
# ---------------------------------------------------------------------------

class FakeStorageAdapter(StorageAdapter):
    """In-memory StorageAdapter for unit tests."""

    def __init__(self) -> None:
        self._records: dict[tuple, StorageRecord] = {}

    def put(self, record: StorageRecord) -> None:
        """Convenience method to pre-populate test data."""
        self._records[(record.domain, record.key, record.layer, record.user_id)] = record

    def read(self, domain, key, layer, user_id):
        return self._records.get((domain, key, layer, user_id))

    def write(self, record):
        self._records[(record.domain, record.key, record.layer, record.user_id)] = record
        return record

    def list(self, domain, layer, user_id):
        return [
            r for r in self._records.values()
            if r.domain == domain and r.layer == layer and r.user_id == user_id
        ]

    def delete(self, domain, key, user_id):
        removed = 0
        for layer in ("facts", "state", "history", "preferences"):
            k = (domain, key, layer, user_id)
            if k in self._records:
                del self._records[k]
                removed += 1
        return removed > 0

    def write_file(self, domain, file_name, content, mime_type, user_id):
        return f"/fake/{domain}/{file_name}"

    def read_file(self, file_path):
        return b""

    def delete_file(self, file_path):
        return False

    def health(self):
        return {"status": "ok"}


class FakeStorageRouter:
    """Router that returns a single FakeStorageAdapter for every domain."""

    def __init__(self, adapter: FakeStorageAdapter | None = None) -> None:
        self._adapter = adapter or FakeStorageAdapter()

    def get_adapter(self, domain: str) -> FakeStorageAdapter:
        return self._adapter


def _make_router(adapter=None) -> DefaultDomainMemoryRouter:
    adapter = adapter or FakeStorageAdapter()
    return DefaultDomainMemoryRouter(
        storage_router=FakeStorageRouter(adapter),
        context_cache=ContextCache(":memory:"),
        structured_store=StructuredStore(":memory:"),
        household_config={"members": ["person_a", "person_b"]},
    )


def _record(domain, key, layer, data, user_id="household") -> StorageRecord:
    return StorageRecord(
        domain=domain, key=key, layer=layer, data=data, user_id=user_id,
        version=1, updated_at="2026-05-13T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Original scaffold tests (23) — must remain passing
# ---------------------------------------------------------------------------


class TestDomainAccessMap:
    _ALL_15 = {
        "food", "home", "environment", "shopping", "travel",
        "money", "documents", "people", "vehicles", "legal",
        "digital", "time", "goals", "family", "health",
    }

    def test_contains_all_15_domains(self):
        assert set(DOMAIN_ACCESS_MAP.keys()) == self._ALL_15

    def test_health_is_restricted(self):
        assert DOMAIN_ACCESS_MAP["health"] is AccessScope.RESTRICTED

    def test_time_goals_family_are_personal(self):
        for domain in ("time", "goals", "family"):
            assert DOMAIN_ACCESS_MAP[domain] is AccessScope.PERSONAL, (
                f"'{domain}' should be PERSONAL"
            )

    def test_all_other_eleven_domains_are_shared(self):
        shared = self._ALL_15 - {"health", "time", "goals", "family"}
        for domain in shared:
            assert DOMAIN_ACCESS_MAP[domain] is AccessScope.SHARED, (
                f"'{domain}' should be SHARED"
            )

    def test_exactly_11_shared_domains(self):
        shared = [d for d, s in DOMAIN_ACCESS_MAP.items() if s is AccessScope.SHARED]
        assert len(shared) == 11

    def test_exactly_3_personal_domains(self):
        personal = [d for d, s in DOMAIN_ACCESS_MAP.items() if s is AccessScope.PERSONAL]
        assert len(personal) == 3

    def test_exactly_1_restricted_domain(self):
        restricted = [d for d, s in DOMAIN_ACCESS_MAP.items() if s is AccessScope.RESTRICTED]
        assert len(restricted) == 1
        assert restricted[0] == "health"


class TestDomainState:
    def test_has_user_id_field(self):
        field_names = {f.name for f in dataclasses.fields(DomainState)}
        assert "user_id" in field_names

    def test_user_id_defaults_to_empty_string(self):
        state = DomainState(domain="food")
        assert state.user_id == ""

    def test_user_id_accepts_person_ids(self):
        for uid in ("person_a", "person_b", "household"):
            state = DomainState(domain="food", user_id=uid)
            assert state.user_id == uid


class TestContextSnapshot:
    def test_has_known_pii_field(self):
        field_names = {f.name for f in dataclasses.fields(ContextSnapshot)}
        assert "known_pii" in field_names

    def test_known_pii_defaults_to_empty_dict(self):
        snap = ContextSnapshot(user_id="person_a", device_id="d1")
        assert snap.known_pii == {}
        assert isinstance(snap.known_pii, dict)

    def test_has_priority_domains_field(self):
        field_names = {f.name for f in dataclasses.fields(ContextSnapshot)}
        assert "priority_domains" in field_names

    def test_has_domain_briefs_field(self):
        field_names = {f.name for f in dataclasses.fields(ContextSnapshot)}
        assert "domain_briefs" in field_names

    def test_priority_domains_defaults_to_empty_list(self):
        snap = ContextSnapshot(user_id="person_a", device_id="d1")
        assert snap.priority_domains == []

    def test_known_pii_accepts_string_map(self):
        snap = ContextSnapshot(
            user_id="person_a",
            device_id="d1",
            known_pii={"John Smith": "[NAME_1]", "07700900000": "[PHONE_1]"},
        )
        assert snap.known_pii["John Smith"] == "[NAME_1]"


class TestAccessResult:
    def test_has_required_fields(self):
        field_names = {f.name for f in dataclasses.fields(AccessResult)}
        assert {"allowed", "scope", "context_key", "denial_reason"} <= field_names

    def test_denial_reason_defaults_to_none(self):
        result = AccessResult(
            allowed=True,
            scope=AccessScope.SHARED,
            context_key="household",
        )
        assert result.denial_reason is None

    def test_denied_result_carries_reason(self):
        result = AccessResult(
            allowed=False,
            scope=AccessScope.RESTRICTED,
            context_key="",
            denial_reason="Please ask me that on your phone.",
        )
        assert result.allowed is False
        assert result.denial_reason is not None


class TestLearnedPreference:
    def test_has_confidence_field(self):
        field_names = {f.name for f in dataclasses.fields(LearnedPreference)}
        assert "confidence" in field_names

    def test_has_observation_count_field(self):
        field_names = {f.name for f in dataclasses.fields(LearnedPreference)}
        assert "observation_count" in field_names

    def test_can_be_instantiated(self):
        pref = LearnedPreference(
            domain="food",
            key="meal_time",
            value="19:00",
            confidence=0.85,
            observation_count=12,
            trigger="ACTION_CONFIRMED",
        )
        assert pref.confidence == 0.85
        assert pref.observation_count == 12


class TestDomainMemoryRouterIsAbstract:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            DomainMemoryRouter()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# New tests — access control
# ---------------------------------------------------------------------------


class TestResolveAccess:
    def test_household_cannot_access_health_domain(self):
        router = _make_router()
        result = router.resolve_access("household", "health")
        assert result.allowed is False
        assert result.denial_reason is not None
        assert "personal device" in result.denial_reason.lower()

    def test_household_cannot_access_goals_domain(self):
        router = _make_router()
        result = router.resolve_access("household", "goals")
        assert result.allowed is False
        assert result.denial_reason is not None

    def test_household_cannot_access_time_domain(self):
        router = _make_router()
        result = router.resolve_access("household", "time")
        assert result.allowed is False

    def test_person_a_can_access_health_domain(self):
        router = _make_router()
        result = router.resolve_access("person_a", "health")
        assert result.allowed is True

    def test_person_a_can_access_food_domain(self):
        router = _make_router()
        result = router.resolve_access("person_a", "food")
        assert result.allowed is True

    def test_person_a_can_access_goals_domain(self):
        router = _make_router()
        result = router.resolve_access("person_a", "goals")
        assert result.allowed is True

    def test_household_can_access_shared_domains(self):
        router = _make_router()
        for domain in ("food", "home", "money", "people", "shopping"):
            result = router.resolve_access("household", domain)
            assert result.allowed is True, f"household should access shared domain '{domain}'"

    def test_unknown_domain_defaults_to_shared_allowed(self):
        router = _make_router()
        result = router.resolve_access("household", "unknown_future_domain")
        assert result.allowed is True
        assert result.scope == AccessScope.SHARED

    def test_shared_domain_context_key_is_household_scoped(self):
        router = _make_router()
        result = router.resolve_access("person_a", "food")
        assert result.context_key == "food:household"

    def test_personal_domain_context_key_is_user_scoped(self):
        router = _make_router()
        result = router.resolve_access("person_a", "goals")
        assert result.context_key == "goals:person_a"

    def test_restricted_denial_message_mentions_personal_device(self):
        router = _make_router()
        result = router.resolve_access("household", "health")
        assert "personal device" in result.denial_reason

    def test_personal_household_denial_mentions_phone(self):
        router = _make_router()
        result = router.resolve_access("household", "goals")
        assert "phone" in result.denial_reason.lower()


# ---------------------------------------------------------------------------
# New tests — context assembly
# ---------------------------------------------------------------------------


class TestAssembleContext:
    def test_returns_context_snapshot_with_correct_user(self):
        router = _make_router()
        snap = router.assemble_context("person_a", "phone")
        assert isinstance(snap, ContextSnapshot)
        assert snap.user_id == "person_a"
        assert snap.device_id == "phone"

    def test_second_call_returns_cached_snapshot(self):
        router = _make_router()
        snap1 = router.assemble_context("person_a", "d1")
        snap2 = router.assemble_context("person_a", "d1")
        # Same assembled_at proves it came from cache
        assert snap1.assembled_at == snap2.assembled_at

    def test_cache_miss_triggers_fresh_assembly(self):
        router = _make_router()
        snap1 = router.assemble_context("person_a", "d1")
        router._context_cache.invalidate_user("person_a")
        snap2 = router.assemble_context("person_a", "d1")
        assert snap2.user_id == "person_a"
        # Both are valid snapshots; they may have different assembled_at
        assert snap2.assembled_at != "" or snap1.assembled_at != ""

    def test_domain_hint_appears_in_priority_domains(self):
        router = _make_router()
        snap = router.assemble_context("person_a", "d1", domain_hint="food")
        priority_names = [d.domain for d in snap.priority_domains]
        assert "food" in priority_names

    def test_domain_hint_is_first_priority_domain(self):
        router = _make_router()
        snap = router.assemble_context("person_a", "d1", domain_hint="money")
        assert snap.priority_domains[0].domain == "money"

    def test_priority_domains_at_most_three(self):
        router = _make_router()
        snap = router.assemble_context("person_a", "d1")
        assert len(snap.priority_domains) <= 3

    def test_household_only_sees_shared_domains_in_snapshot(self):
        router = _make_router()
        snap = router.assemble_context("household", "tablet")
        all_names = (
            [d.domain for d in snap.priority_domains]
            + [b.domain for b in snap.domain_briefs]
        )
        for name in all_names:
            scope = DOMAIN_ACCESS_MAP.get(name, AccessScope.SHARED)
            assert scope == AccessScope.SHARED, f"household saw non-shared domain '{name}'"

    def test_person_a_sees_all_15_domains_in_snapshot(self):
        router = _make_router()
        snap = router.assemble_context("person_a", "phone")
        all_names = set(
            [d.domain for d in snap.priority_domains]
            + [b.domain for b in snap.domain_briefs]
        )
        assert len(all_names) == 15

    def test_snapshot_cached_after_assembly(self):
        router = _make_router()
        router.assemble_context("person_a", "d1")
        assert router._context_cache.get("person_a", "d1") is not None

    def test_assembled_at_is_set(self):
        router = _make_router()
        snap = router.assemble_context("person_a", "d1")
        assert snap.assembled_at != ""

    def test_known_pii_populated_from_people_domain_facts(self):
        adapter = FakeStorageAdapter()
        adapter.put(_record(
            "people", "contact_1", "facts",
            {"name": "Alice Smith", "phone": "+15550001234"},
            user_id="household",
        ))
        router = _make_router(adapter)
        snap = router.assemble_context("person_a", "d1")
        assert "Alice Smith" in snap.known_pii
        assert snap.known_pii["Alice Smith"].startswith("[PERSON_")
        assert "+15550001234" in snap.known_pii
        assert snap.known_pii["+15550001234"] == "[PHONE]"

    def test_known_pii_empty_when_no_people_facts(self):
        router = _make_router()
        snap = router.assemble_context("person_a", "d1")
        assert snap.known_pii == {}


# ---------------------------------------------------------------------------
# New tests — get_domain_state
# ---------------------------------------------------------------------------


class TestGetDomainState:
    def test_returns_domain_state(self):
        router = _make_router()
        state = router.get_domain_state("food", "household")
        assert isinstance(state, DomainState)
        assert state.domain == "food"

    def test_loads_facts_from_storage(self):
        adapter = FakeStorageAdapter()
        adapter.put(_record("food", "item1", "facts", {"name": "rice"}, user_id="household"))
        router = _make_router(adapter)
        state = router.get_domain_state("food", "person_a")
        assert "item1" in state.facts
        assert state.facts["item1"]["name"] == "rice"

    def test_priority_score_is_zero(self):
        router = _make_router()
        state = router.get_domain_state("food", "household")
        assert state.priority_score == 0.0

    def test_not_cached_always_fresh(self):
        adapter = FakeStorageAdapter()
        router = _make_router(adapter)
        state1 = router.get_domain_state("food", "household")
        adapter.put(_record("food", "item1", "facts", {"name": "pasta"}, user_id="household"))
        state2 = router.get_domain_state("food", "household")
        assert "item1" in state2.facts


# ---------------------------------------------------------------------------
# New tests — update_preferences and cache invalidation
# ---------------------------------------------------------------------------


class TestUpdatePreferences:
    def test_update_preferences_invalidates_user_cache(self):
        router = _make_router()
        router.assemble_context("person_a", "d1")
        assert router._context_cache.get("person_a", "d1") is not None
        router.update_preferences("person_a", "food", {"meal_time": "19:00"})
        assert router._context_cache.get("person_a", "d1") is None

    def test_update_preferences_persists_with_domain_prefix(self):
        router = _make_router()
        router.update_preferences("person_a", "food", {"meal_time": "19:00"})
        val = asyncio.run(router._structured_store.get_pref("person_a", "food.meal_time"))
        assert val == "19:00"

    def test_update_preferences_multiple_keys(self):
        router = _make_router()
        router.update_preferences("person_a", "home", {"alarm": "08:00", "mode": "away"})
        assert asyncio.run(router._structured_store.get_pref("person_a", "home.alarm")) == "08:00"
        assert asyncio.run(router._structured_store.get_pref("person_a", "home.mode")) == "away"

    def test_write_to_storage_invalidates_context_cache(self, tmp_path):
        """LocalStorageAdapter with injected cache must call invalidate_domain on write."""
        from mithra.storage.local import LocalStorageAdapter

        cache = ContextCache(":memory:")
        local = LocalStorageAdapter(
            base_path=str(tmp_path / "data"),
            db_path=str(tmp_path / "store.db"),
            cache=cache,
        )
        # Pre-populate cache with a snapshot that includes food domain
        snap = ContextSnapshot(
            user_id="person_a",
            device_id="d1",
            priority_domains=[DomainState(domain="food")],
        )
        cache.set(snap)
        assert cache.get("person_a", "d1") is not None

        # Write to food domain via storage adapter
        local.write(StorageRecord(
            domain="food", key="rice", layer="facts",
            data={"qty": 2}, user_id="household",
            version=1, updated_at="2026-05-13T00:00:00+00:00",
        ))

        # Cache entry for food domain must be gone
        assert cache.get("person_a", "d1") is None


# ---------------------------------------------------------------------------
# New tests — health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok(self):
        router = _make_router()
        h = router.health()
        assert h["status"] == "ok"
        assert h["error"] is None

    def test_health_includes_cache_info(self):
        router = _make_router()
        h = router.health()
        assert "cache" in h
        assert h["cache"]["status"] == "ok"
