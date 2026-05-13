"""Structural and behavioural tests for mithra/routing/domain_router.py."""

import dataclasses

import pytest

from mithra.routing.domain_router import (
    AccessResult,
    AccessScope,
    ContextSnapshot,
    DOMAIN_ACCESS_MAP,
    DomainBrief,
    DomainMemoryRouter,
    DomainState,
    LearnedPreference,
)


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
