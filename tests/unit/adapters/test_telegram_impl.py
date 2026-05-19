"""Tests for TelegramInputAdapter and TelegramOutputAdapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx

from mithra.adapters.messaging import ActionButton, DeliveryReceipt, MessageFormat, OutboundMessage
from mithra.adapters.messaging.telegram_input import TelegramInputAdapter, UserIdentityResolver
from mithra.adapters.messaging.telegram_output import TelegramOutputAdapter
from mithra.agent.interface import AgentRequest, InputChannel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_update(chat_id: int = 111, text: str = "Hello Mithra") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 42,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id},
            "text": text,
        },
    }


def _callback_update(
    chat_id: int = 111,
    action: str = "confirm",
    request_id: str = "req-1",
    action_index: int = 0,
) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cq-1",
            "message": {"chat": {"id": chat_id}, "message_id": 99},
            "data": json.dumps(
                {"action": action, "request_id": request_id, "action_index": action_index}
            ),
        },
    }


def _document_update(chat_id: int = 111) -> dict:
    return {
        "update_id": 3,
        "message": {
            "message_id": 43,
            "chat": {"id": chat_id, "type": "private"},
            "document": {"file_id": "file-abc", "file_name": "receipt.pdf"},
        },
    }


def _voice_update(chat_id: int = 111) -> dict:
    return {
        "update_id": 4,
        "message": {
            "message_id": 44,
            "chat": {"id": chat_id, "type": "private"},
            "voice": {"file_id": "voice-xyz", "duration": 3},
        },
    }


def _make_input_adapter(**kwargs) -> TelegramInputAdapter:
    return TelegramInputAdapter(bot_token="test-token", **kwargs)


def _make_output_adapter(http_client=None) -> TelegramOutputAdapter:
    adapter = TelegramOutputAdapter(bot_token="test-token", http_client=http_client)
    adapter.register_chat("person_a", 111)
    return adapter


# ---------------------------------------------------------------------------
# UserIdentityResolver
# ---------------------------------------------------------------------------

class TestUserIdentityResolver:
    def test_known_telegram_id_returns_correct_user_id(self):
        config = {
            "members": [
                {"user_id": "person_a", "telegram_ids": [111]},
                {"user_id": "person_b", "telegram_ids": [222]},
            ]
        }
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(111) == "person_a"
        assert resolver.resolve_telegram(222) == "person_b"

    def test_unknown_telegram_id_returns_household(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(999) == "household"

    def test_empty_config_returns_household(self):
        resolver = UserIdentityResolver({})
        assert resolver.resolve_telegram(42) == "household"

    def test_telegram_ids_as_list(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [123456789]}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "person_a"

    def test_telegram_ids_as_bare_int(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": 123456789}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "person_a"

    def test_telegram_ids_as_empty_list(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": []}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "household"

    def test_telegram_ids_as_none(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": None}]}
        resolver = UserIdentityResolver(config)
        assert resolver.resolve_telegram(123456789) == "household"


# ---------------------------------------------------------------------------
# TelegramInputAdapter.normalise()
# ---------------------------------------------------------------------------

class TestNormaliseText:
    def test_text_message_returns_agent_request(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(text="Update pantry"), "telegram")
        assert isinstance(result, AgentRequest)
        assert result.text == "Update pantry"

    def test_text_message_sets_input_channel(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(), "telegram")
        assert result.input_channel == InputChannel.TELEGRAM

    def test_text_message_sets_session_id(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(chat_id=555), "telegram")
        assert result.session_id == "tg:555"

    def test_text_message_resolves_user_id(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_text_update(chat_id=111), "telegram")
        assert result.user_id == "person_a"

    def test_unknown_chat_id_defaults_to_household(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(chat_id=9999), "telegram")
        assert result.user_id == "household"


class TestNormaliseCallback:
    def test_callback_query_returns_agent_request(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_callback_update(), "telegram")
        assert isinstance(result, AgentRequest)

    def test_callback_query_sets_confirmation_signal(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(
            _callback_update(action="confirm", request_id="r1", action_index=0),
            "telegram",
        )
        assert result.signal["confirmation"] == "confirm"
        assert result.signal["request_id"] == "r1"
        assert result.signal["action_index"] == 0

    def test_callback_query_reject_action(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(
            _callback_update(action="reject", request_id="r2", action_index=1),
            "telegram",
        )
        assert result.signal["confirmation"] == "reject"

    def test_callback_query_text_is_none(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_callback_update(), "telegram")
        assert result.text is None


class TestNormaliseDocument:
    def test_document_returns_none(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_document_update(), "telegram")
        assert result is None

    def test_voice_message_returns_agent_request(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_voice_update(), "telegram")
        assert isinstance(result, AgentRequest)
        assert result.signal.get("voice_bytes_pending") is True

    def test_unknown_update_returns_none(self):
        adapter = _make_input_adapter()
        result = adapter.normalise({"update_id": 99}, "telegram")
        assert result is None


# ---------------------------------------------------------------------------
# TelegramOutputAdapter
# ---------------------------------------------------------------------------

class TestSend:
    @pytest.mark.asyncio
    async def test_send_returns_delivery_receipt(self):
        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                return_value=httpx.Response(
                    200, json={"ok": True, "result": {"message_id": 77}}
                )
            )
            adapter = TelegramOutputAdapter(bot_token="test-token")
            adapter.register_chat("person_a", 111)
            receipt = await adapter.send("person_a", OutboundMessage(text="Hello!"))
        assert isinstance(receipt, DeliveryReceipt)
        assert receipt.success is True
        assert receipt.message_id == "77"
        assert receipt.channel == "telegram"

    @pytest.mark.asyncio
    async def test_send_with_actions_builds_inline_keyboard(self):
        captured: list[dict] = []

        def capture(request, route):
            body = json.loads(request.content)
            captured.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 88}})

        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                side_effect=capture
            )
            adapter = TelegramOutputAdapter(bot_token="test-token")
            adapter.register_chat("person_a", 111)
            await adapter.send_with_actions(
                "person_a",
                OutboundMessage(text="Confirm?"),
                [ActionButton(label="Yes", action="confirm", payload={"x": 1})],
            )

        assert len(captured) == 1
        keyboard = captured[0]["reply_markup"]["inline_keyboard"]
        assert len(keyboard[0]) == 1
        btn = keyboard[0][0]
        assert btn["text"] == "Yes"
        data = json.loads(btn["callback_data"])
        assert data["action"] == "confirm"

    @pytest.mark.asyncio
    async def test_undo_with_delete_message_strategy(self):
        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/deleteMessage").mock(
                return_value=httpx.Response(200, json={"ok": True, "result": True})
            )
            adapter = TelegramOutputAdapter(bot_token="test-token")
            receipt = DeliveryReceipt(
                channel="telegram",
                message_id="111:42",
                delivered_at="2026-01-01T00:00:00+00:00",
                success=True,
                undoable=True,
                undo_strategy="delete_message",
            )
            result = await adapter.undo(receipt)
        assert result is True

    @pytest.mark.asyncio
    async def test_undo_returns_false_when_not_undoable(self):
        adapter = TelegramOutputAdapter(bot_token="test-token")
        receipt = DeliveryReceipt(
            channel="telegram",
            message_id="99",
            delivered_at="2026-01-01T00:00:00+00:00",
            success=True,
            undoable=False,
        )
        result = await adapter.undo(receipt)
        assert result is False

    def test_register_chat_stores_mapping(self):
        adapter = TelegramOutputAdapter(bot_token="test-token")
        adapter.register_chat("person_a", 123456)
        assert adapter._get_chat_id("person_a") == 123456

    def test_health_returns_ok(self):
        adapter = TelegramOutputAdapter(bot_token="test-token")
        h = adapter.health()
        assert h["status"] == "ok"


# ---------------------------------------------------------------------------
# UserIdentityResolver — registration
# ---------------------------------------------------------------------------

class TestUserIdentityResolverRegistration:
    def test_register_updates_in_memory_map(self):
        resolver = UserIdentityResolver({})
        resolver.register(555, "person_a")
        assert resolver.resolve_telegram(555) == "person_a"

    def test_register_overwrites_existing_mapping(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        resolver.register(111, "person_b")
        assert resolver.resolve_telegram(111) == "person_b"

    def test_register_persists_to_storage(self, tmp_path):
        from mithra.storage.local import LocalStorageAdapter
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        resolver = UserIdentityResolver({}, storage_adapter=storage)
        resolver.register(789, "person_a")
        records = storage.list("identity", "state", "household")
        mapping = next((r for r in records if r.data.get("telegram_id") == 789), None)
        assert mapping is not None
        assert mapping.data["user_id"] == "person_a"

    def test_load_from_storage_on_init(self, tmp_path):
        from mithra.storage.local import LocalStorageAdapter
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        # Persist a mapping via one resolver instance
        r1 = UserIdentityResolver({}, storage_adapter=storage)
        r1.register(321, "person_b")
        # New instance should load it automatically
        r2 = UserIdentityResolver({}, storage_adapter=storage)
        assert r2.resolve_telegram(321) == "person_b"

    def test_get_members_returns_member_list(self):
        config = {"members": [
            {"user_id": "person_a", "display_name": "Alice"},
            {"user_id": "person_b", "display_name": "Bob"},
        ]}
        resolver = UserIdentityResolver(config)
        assert resolver.get_members() == config["members"]

    def test_get_members_empty_when_no_config(self):
        resolver = UserIdentityResolver({})
        assert resolver.get_members() == []

    def test_storage_failure_does_not_crash_init(self):
        class BrokenStorage:
            def list(self, *a, **kw):
                raise RuntimeError("db exploded")
        resolver = UserIdentityResolver({}, storage_adapter=BrokenStorage())
        assert resolver.resolve_telegram(1) == "household"


# ---------------------------------------------------------------------------
# normalise() — /start and registration callback
# ---------------------------------------------------------------------------

def _start_update(chat_id: int = 111, first_name: str = "Alice", username: str = "") -> dict:
    from_data: dict = {"id": chat_id, "first_name": first_name}
    if username:
        from_data["username"] = username
    return {
        "update_id": 10,
        "message": {
            "message_id": 50,
            "chat": {"id": chat_id, "type": "private"},
            "from": from_data,
            "text": "/start",
        },
    }


class TestNormaliseStart:
    """Option B: /start returns None for unknown users, AgentRequest("hi") for known users."""

    def _known_resolver(self, chat_id: int = 111) -> UserIdentityResolver:
        return UserIdentityResolver({"members": [
            {"user_id": "person_a", "telegram_ids": [chat_id], "display_name": "Alice"},
        ]})

    def _unknown_resolver(self) -> UserIdentityResolver:
        return UserIdentityResolver({})

    def test_start_by_unknown_user_returns_none(self):
        adapter = _make_input_adapter(identity_resolver=self._unknown_resolver())
        result = adapter.normalise(_start_update(chat_id=777), "telegram")
        assert result is None

    def test_start_by_unknown_user_records_attempt(self):
        adapter = _make_input_adapter(identity_resolver=self._unknown_resolver())
        adapter.normalise(_start_update(chat_id=777), "telegram")
        assert len(adapter._start_attempts.get(777, [])) == 1

    def test_start_by_authorized_user_returns_agent_request(self):
        adapter = _make_input_adapter(identity_resolver=self._known_resolver(111))
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert isinstance(result, AgentRequest)
        assert result.text == "hi"

    def test_start_by_authorized_user_sets_user_id(self):
        adapter = _make_input_adapter(identity_resolver=self._known_resolver(111))
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result.user_id == "person_a"

    def test_start_by_authorized_user_sets_device_id(self):
        adapter = _make_input_adapter(identity_resolver=self._known_resolver(111))
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result.device_id == "telegram:111"

    def test_start_by_authorized_user_not_rate_limited(self):
        adapter = _make_input_adapter(identity_resolver=self._known_resolver(111))
        for _ in range(5):
            result = adapter.normalise(_start_update(chat_id=111), "telegram")
            assert result is not None and result.text == "hi"
        # No rate-limit tracking for authorized users
        assert len(adapter._start_attempts.get(111, [])) == 0

    def test_start_with_no_resolver_returns_none(self):
        # No resolver: cannot confirm authorized → treated as unknown → None
        adapter = _make_input_adapter()
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result is None

    def test_non_register_callback_still_works(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_callback_update(action="confirm"), "telegram")
        assert result.signal["confirmation"] == "confirm"


# ---------------------------------------------------------------------------
# UserIdentityResolver — is_authorized
# ---------------------------------------------------------------------------

class TestIsAuthorized:
    def test_known_id_is_authorized(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        assert resolver.is_authorized(111) is True

    def test_unknown_id_is_not_authorized(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        assert resolver.is_authorized(999) is False

    def test_empty_map_is_not_authorized(self):
        resolver = UserIdentityResolver({})
        assert resolver.is_authorized(111) is False

    def test_registered_id_becomes_authorized(self):
        resolver = UserIdentityResolver({})
        assert resolver.is_authorized(555) is False
        resolver.register(555, "person_b")
        assert resolver.is_authorized(555) is True

    def test_admin_id_from_env_is_authorized(self, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "12345")
        resolver = UserIdentityResolver({})
        assert resolver.is_authorized(12345) is True

    def test_admin_id_from_env_maps_to_person_a(self, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "99999")
        resolver = UserIdentityResolver({})
        assert resolver.resolve_telegram(99999) == "person_a"

    def test_invalid_admin_id_does_not_crash(self, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "not-a-number")
        resolver = UserIdentityResolver({})  # must not raise
        assert resolver.is_authorized(0) is False

    def test_missing_admin_env_var_has_no_effect(self, monkeypatch):
        monkeypatch.delenv("MITHRA_ADMIN_TELEGRAM_ID", raising=False)
        resolver = UserIdentityResolver({})
        assert resolver.is_authorized(12345) is False


# ---------------------------------------------------------------------------
# Authorization gate in normalise()
# ---------------------------------------------------------------------------

class TestAuthorization:
    def _resolver_with_member(self, chat_id: int = 111) -> UserIdentityResolver:
        config = {"members": [{"user_id": "person_a", "telegram_ids": [chat_id]}]}
        return UserIdentityResolver(config)

    def test_unknown_user_text_message_rejected(self):
        resolver = self._resolver_with_member(chat_id=111)
        adapter = _make_input_adapter(identity_resolver=resolver)
        # chat_id=999 is not in the resolver
        result = adapter.normalise(_text_update(chat_id=999), "telegram")
        assert result is None

    def test_unknown_user_callback_rejected(self):
        resolver = self._resolver_with_member(chat_id=111)
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_callback_update(chat_id=999), "telegram")
        assert result is None

    def test_unknown_user_voice_rejected(self):
        resolver = self._resolver_with_member(chat_id=111)
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_voice_update(chat_id=999), "telegram")
        assert result is None

    def test_known_user_text_message_passes(self):
        resolver = self._resolver_with_member(chat_id=111)
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_text_update(chat_id=111), "telegram")
        assert result is not None
        assert result.text == "Hello Mithra"

    def test_start_bypasses_auth_gate_for_unknown_user(self):
        resolver = self._resolver_with_member(chat_id=111)
        adapter = _make_input_adapter(identity_resolver=resolver)
        # chat_id=999 is unknown but /start must bypass the auth gate (Option B)
        # It returns None (holding message sent async) — NOT rejected by auth gate
        adapter.normalise(_start_update(chat_id=999), "telegram")
        # Rate-limit tracking proves the holding-message path was reached, not auth rejection
        assert len(adapter._start_attempts.get(999, [])) == 1

    def test_no_resolver_allows_all_users(self):
        # No resolver attached — auth check is skipped entirely
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(chat_id=9999), "telegram")
        assert result is not None


# ---------------------------------------------------------------------------
# /start rate limiting
# ---------------------------------------------------------------------------

class TestStartRateLimit:
    """Rate limit only applies to unknown users (to prevent admin notification spam)."""

    def _resolver_unknown(self) -> UserIdentityResolver:
        return UserIdentityResolver({})

    def _resolver_known(self, chat_id: int = 111) -> UserIdentityResolver:
        return UserIdentityResolver({"members": [
            {"user_id": "person_a", "telegram_ids": [chat_id], "display_name": "Alice"},
        ]})

    def test_three_start_attempts_recorded_for_unknown_user(self):
        adapter = _make_input_adapter(identity_resolver=self._resolver_unknown())
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        assert len(adapter._start_attempts.get(111, [])) == 3

    def test_fourth_start_silently_dropped(self):
        adapter = _make_input_adapter(identity_resolver=self._resolver_unknown())
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        # 4th: returns None without adding to attempts (silent drop)
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result is None
        assert len(adapter._start_attempts.get(111, [])) == 3

    def test_rate_limit_is_per_chat_id(self):
        adapter = _make_input_adapter(identity_resolver=self._resolver_unknown())
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        # chat 222 gets its own attempt counter
        adapter.normalise(_start_update(chat_id=222), "telegram")
        assert len(adapter._start_attempts.get(222, [])) == 1

    def test_rate_limit_resets_after_window(self):
        import time as _time
        adapter = _make_input_adapter(identity_resolver=self._resolver_unknown())
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        # Age the timestamps past the window
        adapter._start_attempts[111] = [_time.time() - 3601] * 3
        # Next attempt clears stale entries and records fresh one
        adapter.normalise(_start_update(chat_id=111), "telegram")
        assert len(adapter._start_attempts.get(111, [])) == 1

    def test_authorized_user_start_never_rate_limited(self):
        adapter = _make_input_adapter(identity_resolver=self._resolver_known(111))
        for _ in range(5):
            result = adapter.normalise(_start_update(chat_id=111), "telegram")
            assert result is not None and result.text == "hi"
        assert len(adapter._start_attempts.get(111, [])) == 0


# ---------------------------------------------------------------------------
# UserIdentityResolver — block()
# ---------------------------------------------------------------------------

class TestBlockedUsers:
    def test_block_prevents_authorization(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        assert resolver.is_authorized(111) is True
        resolver.block(111)
        assert resolver.is_authorized(111) is False

    def test_block_unknown_id_also_rejected(self):
        resolver = UserIdentityResolver({})
        resolver.block(999)
        assert resolver.is_authorized(999) is False

    def test_block_persists_to_storage(self, tmp_path):
        from mithra.storage.local import LocalStorageAdapter
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        resolver = UserIdentityResolver({}, storage_adapter=storage)
        resolver.block(777)
        records = storage.list("identity", "state", "household")
        blocked = next((r for r in records if r.data.get("telegram_id") == 777), None)
        assert blocked is not None
        assert blocked.data["type"] == "blocked"

    def test_blocked_ids_loaded_from_storage_on_init(self, tmp_path):
        from mithra.storage.local import LocalStorageAdapter
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        r1 = UserIdentityResolver({}, storage_adapter=storage)
        r1.block(888)
        r2 = UserIdentityResolver({}, storage_adapter=storage)
        assert r2.is_authorized(888) is False

    def test_blocked_user_rejected_in_normalise(self):
        config = {"members": [{"user_id": "person_a", "telegram_ids": [111]}]}
        resolver = UserIdentityResolver(config)
        resolver.block(111)
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_text_update(chat_id=111), "telegram")
        assert result is None

    def test_non_blocked_member_still_passes(self):
        config = {"members": [
            {"user_id": "person_a", "telegram_ids": [111]},
            {"user_id": "person_b", "telegram_ids": [222]},
        ]}
        resolver = UserIdentityResolver(config)
        resolver.block(111)
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_text_update(chat_id=222), "telegram")
        assert result is not None


# ---------------------------------------------------------------------------
# Admin approve / block callbacks
# ---------------------------------------------------------------------------

def _admin_approve_callback(
    admin_chat_id: int = 999, target_id: int = 555, uid: str = "person_a"
) -> dict:
    return {
        "update_id": 20,
        "callback_query": {
            "id": "cq-admin",
            "message": {"chat": {"id": admin_chat_id}, "message_id": 100},
            "data": json.dumps(
                {"action": "admin_approve", "id": target_id, "uid": uid},
                separators=(',', ':'),
            ),
        },
    }


def _admin_deny_callback(admin_chat_id: int = 999, target_id: int = 555) -> dict:
    return {
        "update_id": 21,
        "callback_query": {
            "id": "cq-admin-deny",
            "message": {"chat": {"id": admin_chat_id}, "message_id": 101},
            "data": json.dumps(
                {"action": "admin_deny", "id": target_id},
                separators=(',', ':'),
            ),
        },
    }


class TestAdminApprove:
    def _resolver_with_admin(self) -> UserIdentityResolver:
        # Admin (999) is pre-authorized — mirrors MITHRA_ADMIN_TELEGRAM_ID in prod
        config = {"members": [
            {"user_id": "person_a", "telegram_ids": [999], "display_name": "Abhiram"},
            {"user_id": "person_b", "telegram_ids": [], "display_name": "Wife"},
        ]}
        return UserIdentityResolver(config)

    def test_admin_approve_registers_as_specified_user_id(self):
        resolver = self._resolver_with_admin()
        adapter = _make_input_adapter(identity_resolver=resolver)
        adapter.normalise(_admin_approve_callback(admin_chat_id=999, target_id=555, uid="person_b"), "telegram")
        assert resolver.is_authorized(555) is True
        assert resolver.resolve_telegram(555) == "person_b"

    def test_admin_approve_returns_none(self):
        resolver = self._resolver_with_admin()
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_admin_approve_callback(admin_chat_id=999), "telegram")
        assert result is None

    def test_admin_approve_no_resolver_does_not_crash(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_admin_approve_callback(), "telegram")
        assert result is None

    def test_admin_deny_blocks_user(self):
        resolver = self._resolver_with_admin()
        adapter = _make_input_adapter(identity_resolver=resolver)
        adapter.normalise(_admin_deny_callback(admin_chat_id=999, target_id=666), "telegram")
        assert resolver.is_authorized(666) is False
        # Confirm it's actually blocked, not just unknown
        resolver.register(666, "person_a")
        assert resolver.is_authorized(666) is False

    def test_admin_deny_returns_none(self):
        resolver = self._resolver_with_admin()
        adapter = _make_input_adapter(identity_resolver=resolver)
        result = adapter.normalise(_admin_deny_callback(admin_chat_id=999), "telegram")
        assert result is None

    def test_admin_deny_no_resolver_does_not_crash(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_admin_deny_callback(), "telegram")
        assert result is None

    def test_approved_user_can_send_messages(self):
        resolver = self._resolver_with_admin()
        adapter = _make_input_adapter(identity_resolver=resolver)
        adapter.normalise(_admin_approve_callback(admin_chat_id=999, target_id=555), "telegram")
        result = adapter.normalise(_text_update(chat_id=555), "telegram")
        assert result is not None

    def test_denied_user_blocked_even_after_explicit_register(self):
        resolver = self._resolver_with_admin()
        adapter = _make_input_adapter(identity_resolver=resolver)
        adapter.normalise(_admin_deny_callback(admin_chat_id=999, target_id=666), "telegram")
        resolver.register(666, "person_a")
        result = adapter.normalise(_text_update(chat_id=666), "telegram")
        assert result is None


# ---------------------------------------------------------------------------
# _notify_admin_of_unknown_user — async (respx)
# ---------------------------------------------------------------------------

class TestAdminNotification:
    @pytest.mark.asyncio
    async def test_request_admin_approval_sends_member_buttons(self, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "999")
        captured: list[dict] = []

        def capture(request, route):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        config = {"members": [
            {"user_id": "person_a", "telegram_ids": [], "display_name": "Abhiram"},
            {"user_id": "person_b", "telegram_ids": [], "display_name": "Wife"},
        ]}
        resolver = UserIdentityResolver(config)

        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                side_effect=capture
            )
            adapter = _make_input_adapter(identity_resolver=resolver)
            await adapter._request_admin_approval(
                chat_id=555, first_name="Alice", username="alice99"
            )

        assert len(captured) == 1
        body = captured[0]
        assert body["chat_id"] == 999
        assert "555" in body["text"]
        assert "Alice" in body["text"]

        # Flatten rows to get all buttons
        all_buttons = [btn for row in body["reply_markup"]["inline_keyboard"] for btn in row]

        # All callback_data within Telegram's 64-byte limit
        for btn in all_buttons:
            assert len(btn["callback_data"].encode()) <= 64, (
                f"callback_data too long: {btn['callback_data']!r}"
            )

        actions = [json.loads(b["callback_data"])["action"] for b in all_buttons]
        assert "admin_approve" in actions
        assert "admin_deny" in actions

        # 2 approve buttons (one per member) + 1 deny
        assert len(all_buttons) == 3

        # Each approve button carries uid and id
        approve_btns = [b for b in all_buttons if json.loads(b["callback_data"])["action"] == "admin_approve"]
        uids = {json.loads(b["callback_data"])["uid"] for b in approve_btns}
        assert uids == {"person_a", "person_b"}
        for b in approve_btns:
            assert json.loads(b["callback_data"])["id"] == 555

    @pytest.mark.asyncio
    async def test_request_admin_approval_skipped_when_no_admin_id(self, monkeypatch):
        monkeypatch.delenv("MITHRA_ADMIN_TELEGRAM_ID", raising=False)
        captured: list[dict] = []

        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                side_effect=lambda req, route: captured.append(req) or httpx.Response(200, json={"ok": True})
            )
            adapter = _make_input_adapter()
            await adapter._request_admin_approval(555, "", "")

        assert len(captured) == 0

    @pytest.mark.asyncio
    async def test_send_holding_message_notifies_user(self):
        captured: list[dict] = []

        def capture(request, route):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})

        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                side_effect=capture
            )
            adapter = _make_input_adapter()
            await adapter._send_holding_message(555, "Alice")

        assert len(captured) == 1
        assert captured[0]["chat_id"] == 555
        assert "Alice" in captured[0]["text"]
        assert "access request" in captured[0]["text"].lower()
