"""Unit tests for StructuredStore.

Uses SQLite in-memory databases (:memory:) for speed and isolation.
All public async methods are covered; health() is synchronous.
"""

from __future__ import annotations

import pytest

from mithra.memory.structured_store import StructuredStore


@pytest.fixture
def store() -> StructuredStore:
    return StructuredStore(db_path=":memory:")


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


class TestFacts:
    async def test_set_and_get_fact(self, store):
        await store.set_fact("food", "pantry_count", 42)
        assert await store.get_fact("food", "pantry_count") == 42

    async def test_get_fact_returns_none_for_missing_key(self, store):
        assert await store.get_fact("food", "nonexistent") is None

    async def test_set_fact_overwrites_existing(self, store):
        await store.set_fact("food", "pantry_count", 10)
        await store.set_fact("food", "pantry_count", 99)
        assert await store.get_fact("food", "pantry_count") == 99

    async def test_set_fact_stores_complex_value(self, store):
        payload = {"items": ["rice", "pasta"], "count": 2, "nested": {"a": 1}}
        await store.set_fact("food", "pantry", payload)
        assert await store.get_fact("food", "pantry") == payload

    async def test_list_facts_returns_all_for_domain(self, store):
        await store.set_fact("food", "a", 1)
        await store.set_fact("food", "b", 2)
        facts = await store.list_facts("food")
        assert facts == {"a": 1, "b": 2}

    async def test_list_facts_empty_domain_returns_empty_dict(self, store):
        assert await store.list_facts("nonexistent") == {}

    async def test_list_facts_isolates_domains(self, store):
        await store.set_fact("food", "x", "apple")
        await store.set_fact("money", "x", "budget")
        food_facts = await store.list_facts("food")
        assert food_facts["x"] == "apple"
        money_facts = await store.list_facts("money")
        assert money_facts["x"] == "budget"

    async def test_delete_fact_removes_entry(self, store):
        await store.set_fact("food", "item", "rice")
        deleted = await store.delete_fact("food", "item")
        assert deleted is True
        assert await store.get_fact("food", "item") is None

    async def test_delete_fact_returns_false_for_nonexistent(self, store):
        assert await store.delete_fact("food", "ghost") is False

    async def test_delete_fact_only_removes_targeted_key(self, store):
        await store.set_fact("food", "keep", "pasta")
        await store.set_fact("food", "remove", "rice")
        await store.delete_fact("food", "remove")
        assert await store.get_fact("food", "keep") == "pasta"
        assert await store.get_fact("food", "remove") is None

    async def test_different_user_ids_store_separately(self, store):
        await store.set_fact("goals", "target", "run 5k", user_id="person_a")
        await store.set_fact("goals", "target", "read 20 books", user_id="person_b")
        assert await store.get_fact("goals", "target", user_id="person_a") == "run 5k"
        assert await store.get_fact("goals", "target", user_id="person_b") == "read 20 books"

    async def test_list_facts_scoped_to_user_id(self, store):
        await store.set_fact("goals", "k", "v_a", user_id="person_a")
        await store.set_fact("goals", "k", "v_b", user_id="person_b")
        a_facts = await store.list_facts("goals", user_id="person_a")
        b_facts = await store.list_facts("goals", user_id="person_b")
        assert a_facts == {"k": "v_a"}
        assert b_facts == {"k": "v_b"}

    async def test_delete_scoped_to_user_id(self, store):
        await store.set_fact("goals", "k", "v_a", user_id="person_a")
        await store.set_fact("goals", "k", "v_b", user_id="person_b")
        await store.delete_fact("goals", "k", user_id="person_a")
        assert await store.get_fact("goals", "k", user_id="person_a") is None
        assert await store.get_fact("goals", "k", user_id="person_b") == "v_b"

    async def test_default_user_id_is_household(self, store):
        await store.set_fact("food", "k", "v")
        # Explicit household read should find it
        assert await store.get_fact("food", "k", user_id="household") == "v"
        # person_a should not see it
        assert await store.get_fact("food", "k", user_id="person_a") is None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class TestState:
    async def test_set_and_get_state(self, store):
        await store.set_state("home", "alarm_active", True)
        assert await store.get_state("home", "alarm_active") is True

    async def test_get_state_returns_none_for_missing(self, store):
        assert await store.get_state("home", "missing") is None

    async def test_set_state_overwrites_existing(self, store):
        await store.set_state("home", "temp", 18.5)
        await store.set_state("home", "temp", 21.0)
        assert await store.get_state("home", "temp") == 21.0

    async def test_list_state_returns_all_for_domain(self, store):
        await store.set_state("home", "k1", "v1")
        await store.set_state("home", "k2", "v2")
        result = await store.list_state("home")
        assert result == {"k1": "v1", "k2": "v2"}

    async def test_list_state_empty_returns_empty_dict(self, store):
        assert await store.list_state("empty_domain") == {}

    async def test_list_state_isolates_domains(self, store):
        await store.set_state("home", "x", "home_val")
        await store.set_state("food", "x", "food_val")
        assert (await store.list_state("home"))["x"] == "home_val"
        assert (await store.list_state("food"))["x"] == "food_val"

    async def test_delete_state_removes_entry(self, store):
        await store.set_state("home", "mode", "away")
        deleted = await store.delete_state("home", "mode")
        assert deleted is True
        assert await store.get_state("home", "mode") is None

    async def test_delete_state_returns_false_for_nonexistent(self, store):
        assert await store.delete_state("home", "ghost") is False

    async def test_delete_state_only_removes_targeted_key(self, store):
        await store.set_state("home", "keep", "on")
        await store.set_state("home", "remove", "off")
        await store.delete_state("home", "remove")
        assert await store.get_state("home", "keep") == "on"
        assert await store.get_state("home", "remove") is None

    async def test_different_user_ids_store_separately(self, store):
        await store.set_state("health", "steps", 8000, user_id="person_a")
        await store.set_state("health", "steps", 12000, user_id="person_b")
        assert await store.get_state("health", "steps", user_id="person_a") == 8000
        assert await store.get_state("health", "steps", user_id="person_b") == 12000

    async def test_list_state_scoped_to_user_id(self, store):
        await store.set_state("health", "k", "a_val", user_id="person_a")
        await store.set_state("health", "k", "b_val", user_id="person_b")
        assert (await store.list_state("health", user_id="person_a")) == {"k": "a_val"}
        assert (await store.list_state("health", user_id="person_b")) == {"k": "b_val"}

    async def test_default_user_id_is_household(self, store):
        await store.set_state("home", "mode", "night")
        assert await store.get_state("home", "mode", user_id="household") == "night"
        assert await store.get_state("home", "mode", user_id="person_a") is None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestHistory:
    async def test_append_and_get_history(self, store):
        entry = {"action": "bought_rice", "qty": 2}
        await store.append_history("food", entry)
        history = await store.get_history("food")
        assert len(history) == 1
        assert history[0] == entry

    async def test_history_returns_empty_for_unknown_domain(self, store):
        assert await store.get_history("nonexistent") == []

    async def test_history_is_newest_first(self, store):
        await store.append_history("food", {"seq": 1})
        await store.append_history("food", {"seq": 2})
        await store.append_history("food", {"seq": 3})
        history = await store.get_history("food")
        seqs = [h["seq"] for h in history]
        assert seqs == [3, 2, 1]

    async def test_history_limit_is_respected(self, store):
        for i in range(10):
            await store.append_history("food", {"i": i})
        history = await store.get_history("food", limit=3)
        assert len(history) == 3

    async def test_history_isolates_domains(self, store):
        await store.append_history("food", {"action": "ate"})
        await store.append_history("money", {"action": "paid"})
        food_history = await store.get_history("food")
        money_history = await store.get_history("money")
        assert all(h["action"] == "ate" for h in food_history)
        assert all(h["action"] == "paid" for h in money_history)

    async def test_multiple_appends_accumulate(self, store):
        for i in range(5):
            await store.append_history("food", {"seq": i})
        assert len(await store.get_history("food")) == 5

    async def test_history_isolates_user_ids(self, store):
        await store.append_history("health", {"event": "a_action"}, user_id="person_a")
        await store.append_history("health", {"event": "b_action"}, user_id="person_b")
        a_hist = await store.get_history("health", user_id="person_a")
        b_hist = await store.get_history("health", user_id="person_b")
        assert len(a_hist) == 1 and a_hist[0]["event"] == "a_action"
        assert len(b_hist) == 1 and b_hist[0]["event"] == "b_action"

    async def test_default_user_id_is_household(self, store):
        await store.append_history("food", {"action": "shared_buy"})
        household = await store.get_history("food", user_id="household")
        person_a = await store.get_history("food", user_id="person_a")
        assert len(household) == 1
        assert person_a == []


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


class TestPrefs:
    async def test_set_and_get_pref(self, store):
        await store.set_pref("person_a", "language", "en")
        assert await store.get_pref("person_a", "language") == "en"

    async def test_get_pref_returns_none_for_missing(self, store):
        assert await store.get_pref("person_a", "nonexistent") is None

    async def test_set_pref_overwrites_existing(self, store):
        await store.set_pref("person_a", "language", "en")
        await store.set_pref("person_a", "language", "es")
        assert await store.get_pref("person_a", "language") == "es"

    async def test_set_pref_stores_complex_value(self, store):
        payload = {"channels": ["telegram", "voice"], "quiet_hours": [22, 7]}
        await store.set_pref("person_a", "notifications", payload)
        assert await store.get_pref("person_a", "notifications") == payload

    async def test_list_prefs_returns_all_for_user(self, store):
        await store.set_pref("person_a", "lang", "en")
        await store.set_pref("person_a", "tz", "UTC")
        prefs = await store.list_prefs("person_a")
        assert prefs == {"lang": "en", "tz": "UTC"}

    async def test_list_prefs_empty_user_returns_empty_dict(self, store):
        assert await store.list_prefs("unknown_user") == {}

    async def test_list_prefs_isolates_user_ids(self, store):
        await store.set_pref("person_a", "lang", "en")
        await store.set_pref("person_b", "lang", "fr")
        assert (await store.list_prefs("person_a"))["lang"] == "en"
        assert (await store.list_prefs("person_b"))["lang"] == "fr"

    async def test_household_user_id_works(self, store):
        await store.set_pref("household", "timezone", "America/New_York")
        assert await store.get_pref("household", "timezone") == "America/New_York"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_ok_on_fresh_store(self, store):
        h = store.health()
        assert h["status"] == "ok"
        assert h["error"] is None
        assert h["fact_count"] == 0
        assert h["state_count"] == 0
        assert h["history_count"] == 0
        assert h["pref_count"] == 0

    async def test_health_counts_reflect_writes(self, store):
        await store.set_fact("food", "k", "v")
        await store.set_state("home", "k", "v")
        await store.append_history("food", {"a": 1})
        await store.append_history("food", {"b": 2})
        await store.set_pref("person_a", "k", "v")
        h = store.health()
        assert h["fact_count"] == 1
        assert h["state_count"] == 1
        assert h["history_count"] == 2
        assert h["pref_count"] == 1

    async def test_health_counts_all_user_ids(self, store):
        await store.set_fact("goals", "k", "v_a", user_id="person_a")
        await store.set_fact("goals", "k", "v_b", user_id="person_b")
        assert store.health()["fact_count"] == 2

    async def test_health_count_decreases_after_delete(self, store):
        await store.set_fact("food", "k", "v")
        assert store.health()["fact_count"] == 1
        await store.delete_fact("food", "k")
        assert store.health()["fact_count"] == 0

    def test_health_includes_db_path(self, store):
        h = store.health()
        assert h["db_path"] == ":memory:"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    async def test_data_persists_across_reconnection(self, tmp_path):
        db = str(tmp_path / "store.db")
        s1 = StructuredStore(db_path=db)
        await s1.set_fact("food", "persistent_key", "persistent_value")
        s1._conn.close()

        s2 = StructuredStore(db_path=db)
        assert await s2.get_fact("food", "persistent_key") == "persistent_value"
