"""Tests for TelegramInputAdapter and TelegramOutputAdapter."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest
import respx

from mithra.adapters.messaging import ActionButton, DeliveryReceipt, OutboundMessage
from mithra.adapters.messaging.telegram_input import TelegramInputAdapter
from mithra.adapters.messaging.telegram_output import TelegramOutputAdapter
from mithra.agent.interface import AgentRequest, InputChannel
from mithra.identity.member_registry import MemberRegistry


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


def _make_input_adapter(**kwargs) -> TelegramInputAdapter:
    return TelegramInputAdapter(bot_token="test-token", **kwargs)


def _make_output_adapter(http_client=None) -> TelegramOutputAdapter:
    adapter = TelegramOutputAdapter(bot_token="test-token", http_client=http_client)
    adapter.register_chat("person_a", 111)
    return adapter


def _make_registry(
    tmp_path,
    members=None,
    channels=None,
    blocked=None,
    household_id: str = "hh_test",
) -> MemberRegistry:
    """Create a MemberRegistry backed by a temp SQLite DB with seeded test data."""
    from mithra.storage.local import LocalStorageAdapter

    db_path = str(tmp_path / "db.sqlite")
    # LocalStorageAdapter runs all DDL including identity tables
    LocalStorageAdapter(str(tmp_path / "files"), db_path)
    registry = MemberRegistry(db_path, household_id)

    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO households (id, display_name, timezone, locale, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (household_id, "Test Household", "UTC", "en-US", now, now),
    )
    for m in (members or []):
        conn.execute(
            "INSERT OR IGNORE INTO members (id, household_id, display_name, role, status, invited_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (m["id"], household_id, m.get("display_name", m["id"]),
             m.get("role", "member"), m.get("status", "active"), None, now, now),
        )
    for c in (channels or []):
        conn.execute(
            "INSERT OR IGNORE INTO member_channels (user_id, household_id, channel, channel_id, verified, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (c["user_id"], household_id, c["channel"], str(c["channel_id"]), 1, now),
        )
    for b in (blocked or []):
        block_uid = f"__blocked__{b['channel_id']}"
        conn.execute(
            "INSERT OR IGNORE INTO member_channels (user_id, household_id, channel, channel_id, verified, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (block_uid, household_id, b["channel"], str(b["channel_id"]), 0, now),
        )
    conn.commit()
    conn.close()
    return registry


# ---------------------------------------------------------------------------
# MemberRegistry — channel resolution
# ---------------------------------------------------------------------------

class TestMemberRegistry:
    def test_known_telegram_channel_returns_correct_member(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[
                {"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"},
                {"id": "person_b", "display_name": "Bob", "role": "member", "status": "active"},
            ],
            channels=[
                {"user_id": "person_a", "channel": "telegram", "channel_id": "111"},
                {"user_id": "person_b", "channel": "telegram", "channel_id": "222"},
            ],
        )
        assert registry.resolve_channel("telegram", "111").id == "person_a"
        assert registry.resolve_channel("telegram", "222").id == "person_b"

    def test_unknown_channel_returns_none(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        assert registry.resolve_channel("telegram", "999") is None

    def test_empty_registry_returns_none(self, tmp_path):
        registry = _make_registry(tmp_path)
        assert registry.resolve_channel("telegram", "42") is None

    def test_get_channel_id_returns_string(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        assert registry.get_channel_id("person_a", "telegram") == "111"

    def test_get_channel_id_unknown_returns_none(self, tmp_path):
        registry = _make_registry(tmp_path)
        assert registry.get_channel_id("person_a", "telegram") is None


# ---------------------------------------------------------------------------
# MemberRegistry — registration
# ---------------------------------------------------------------------------

class TestMemberRegistryRegistration:
    def test_register_channel_authorizes_member(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
        )
        assert registry.is_authorized("telegram", "555") is False
        registry.register_channel("person_a", "telegram", "555")
        assert registry.is_authorized("telegram", "555") is True

    def test_register_channel_overwrites_existing(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[
                {"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"},
                {"id": "person_b", "display_name": "Bob", "role": "member", "status": "active"},
            ],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        registry.register_channel("person_b", "telegram", "111")
        assert registry.resolve_channel("telegram", "111").id == "person_b"

    def test_register_channel_persists_across_instances(self, tmp_path):
        registry1 = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
        )
        registry1.register_channel("person_a", "telegram", "789")
        registry2 = MemberRegistry(str(tmp_path / "db.sqlite"), "hh_test")
        assert registry2.resolve_channel("telegram", "789").id == "person_a"

    def test_get_members_returns_active_members(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[
                {"id": "person_a", "display_name": "Alice", "role": "owner", "status": "active"},
                {"id": "person_b", "display_name": "Bob", "role": "member", "status": "active"},
            ],
        )
        members = registry.get_members()
        assert len(members) == 2
        ids = {m.id for m in members}
        assert ids == {"person_a", "person_b"}

    def test_get_members_excludes_invited(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[
                {"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"},
                {"id": "person_b", "display_name": "Bob", "role": "member", "status": "invited"},
            ],
        )
        members = registry.get_members()
        assert len(members) == 1
        assert members[0].id == "person_a"


# ---------------------------------------------------------------------------
# MemberRegistry — authorization
# ---------------------------------------------------------------------------

class TestMemberRegistryAuthorized:
    def test_registered_channel_is_authorized(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        assert registry.is_authorized("telegram", "111") is True

    def test_unknown_channel_is_not_authorized(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        assert registry.is_authorized("telegram", "999") is False

    def test_empty_registry_is_not_authorized(self, tmp_path):
        registry = _make_registry(tmp_path)
        assert registry.is_authorized("telegram", "111") is False

    def test_registered_id_becomes_authorized(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_b", "display_name": "Bob", "role": "member", "status": "active"}],
        )
        assert registry.is_authorized("telegram", "555") is False
        registry.register_channel("person_b", "telegram", "555")
        assert registry.is_authorized("telegram", "555") is True

    def test_admin_seed_channel_becomes_authorized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "12345")
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Admin", "role": "owner", "status": "active"}],
        )
        adapter = _make_input_adapter(member_registry=registry)
        adapter._seed_admin_channel()
        assert registry.is_authorized("telegram", "12345") is True

    def test_admin_seed_maps_to_owner_member(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "99999")
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Admin", "role": "owner", "status": "active"}],
        )
        adapter = _make_input_adapter(member_registry=registry)
        adapter._seed_admin_channel()
        member = registry.resolve_channel("telegram", "99999")
        assert member is not None
        assert member.id == "person_a"

    def test_invalid_admin_env_does_not_crash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "not-a-number")
        registry = _make_registry(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        adapter._seed_admin_channel()  # must not raise
        assert registry.is_authorized("telegram", "0") is False

    def test_missing_admin_env_has_no_effect(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MITHRA_ADMIN_TELEGRAM_ID", raising=False)
        registry = _make_registry(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        adapter._seed_admin_channel()
        assert registry.is_authorized("telegram", "12345") is False


# ---------------------------------------------------------------------------
# MemberRegistry — blocking
# ---------------------------------------------------------------------------

class TestBlockedChannels:
    def test_block_prevents_authorization(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        assert registry.is_authorized("telegram", "111") is True
        registry.block_channel("telegram", "111")
        assert registry.is_authorized("telegram", "111") is False

    def test_block_unknown_id_also_rejected(self, tmp_path):
        registry = _make_registry(tmp_path)
        registry.block_channel("telegram", "999")
        assert registry.is_authorized("telegram", "999") is False

    def test_pre_seeded_blocked_channel_is_blocked(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            blocked=[{"channel": "telegram", "channel_id": "777"}],
        )
        assert registry.is_blocked("telegram", "777") is True
        assert registry.is_authorized("telegram", "777") is False

    def test_block_persists_across_instances(self, tmp_path):
        registry1 = _make_registry(tmp_path)
        registry1.block_channel("telegram", "888")
        registry2 = MemberRegistry(str(tmp_path / "db.sqlite"), "hh_test")
        assert registry2.is_authorized("telegram", "888") is False

    def test_blocked_user_rejected_in_normalise(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        registry.block_channel("telegram", "111")
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_text_update(chat_id=111), "telegram")
        assert result is None

    def test_non_blocked_member_still_passes(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[
                {"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"},
                {"id": "person_b", "display_name": "Bob", "role": "member", "status": "active"},
            ],
            channels=[
                {"user_id": "person_a", "channel": "telegram", "channel_id": "111"},
                {"user_id": "person_b", "channel": "telegram", "channel_id": "222"},
            ],
        )
        registry.block_channel("telegram", "111")
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_text_update(chat_id=222), "telegram")
        assert result is not None


# ---------------------------------------------------------------------------
# TelegramInputAdapter.normalise() — text messages
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

    def test_text_message_resolves_user_id(self, tmp_path):
        registry = _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "111"}],
        )
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_text_update(chat_id=111), "telegram")
        assert result.user_id == "person_a"

    def test_unknown_chat_id_defaults_to_household(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(chat_id=9999), "telegram")
        assert result.user_id == "household"


# ---------------------------------------------------------------------------
# TelegramInputAdapter.normalise() — callbacks
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# TelegramInputAdapter.normalise() — documents and voice
# ---------------------------------------------------------------------------

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
# normalise() — /start handling
# ---------------------------------------------------------------------------

class TestNormaliseStart:
    """Option B: /start returns None for unknown users, AgentRequest("hi") for known users."""

    def _known_registry(self, tmp_path, chat_id: int = 111) -> MemberRegistry:
        return _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": str(chat_id)}],
        )

    def _unknown_registry(self, tmp_path) -> MemberRegistry:
        return _make_registry(tmp_path)

    def test_start_by_unknown_user_returns_none(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._unknown_registry(tmp_path))
        result = adapter.normalise(_start_update(chat_id=777), "telegram")
        assert result is None

    def test_start_by_unknown_user_records_attempt(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._unknown_registry(tmp_path))
        adapter.normalise(_start_update(chat_id=777), "telegram")
        assert len(adapter._start_attempts.get(777, [])) == 1

    def test_start_by_authorized_user_returns_agent_request(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._known_registry(tmp_path, 111))
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert isinstance(result, AgentRequest)
        assert result.text == "hi"

    def test_start_by_authorized_user_sets_user_id(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._known_registry(tmp_path, 111))
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result.user_id == "person_a"

    def test_start_by_authorized_user_sets_device_id(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._known_registry(tmp_path, 111))
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result.device_id == "telegram:111"

    def test_start_by_authorized_user_not_rate_limited(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._known_registry(tmp_path, 111))
        for _ in range(5):
            result = adapter.normalise(_start_update(chat_id=111), "telegram")
            assert result is not None and result.text == "hi"
        assert len(adapter._start_attempts.get(111, [])) == 0

    def test_start_with_no_registry_returns_none(self):
        # No registry: /start for unknown → treated as unknown → None
        adapter = _make_input_adapter()
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result is None

    def test_non_register_callback_still_works(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_callback_update(action="confirm"), "telegram")
        assert result.signal["confirmation"] == "confirm"


# ---------------------------------------------------------------------------
# Authorization gate in normalise()
# ---------------------------------------------------------------------------

class TestAuthorization:
    def _registry_with_member(self, tmp_path, chat_id: int = 111) -> MemberRegistry:
        return _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": str(chat_id)}],
        )

    def test_unknown_user_text_message_rejected(self, tmp_path):
        registry = self._registry_with_member(tmp_path, chat_id=111)
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_text_update(chat_id=999), "telegram")
        assert result is None

    def test_unknown_user_callback_rejected(self, tmp_path):
        registry = self._registry_with_member(tmp_path, chat_id=111)
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_callback_update(chat_id=999), "telegram")
        assert result is None

    def test_unknown_user_voice_rejected(self, tmp_path):
        registry = self._registry_with_member(tmp_path, chat_id=111)
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_voice_update(chat_id=999), "telegram")
        assert result is None

    def test_known_user_text_message_passes(self, tmp_path):
        registry = self._registry_with_member(tmp_path, chat_id=111)
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_text_update(chat_id=111), "telegram")
        assert result is not None
        assert result.text == "Hello Mithra"

    def test_start_bypasses_auth_gate_for_unknown_user(self, tmp_path):
        registry = self._registry_with_member(tmp_path, chat_id=111)
        adapter = _make_input_adapter(member_registry=registry)
        # chat_id=999 is unknown but /start must bypass the auth gate
        adapter.normalise(_start_update(chat_id=999), "telegram")
        assert len(adapter._start_attempts.get(999, [])) == 1

    def test_no_registry_allows_all_users(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_text_update(chat_id=9999), "telegram")
        assert result is not None


# ---------------------------------------------------------------------------
# /start rate limiting
# ---------------------------------------------------------------------------

class TestStartRateLimit:
    def _unknown_registry(self, tmp_path) -> MemberRegistry:
        return _make_registry(tmp_path)

    def _known_registry(self, tmp_path, chat_id: int = 111) -> MemberRegistry:
        return _make_registry(
            tmp_path,
            members=[{"id": "person_a", "display_name": "Alice", "role": "member", "status": "active"}],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": str(chat_id)}],
        )

    def test_three_start_attempts_recorded_for_unknown_user(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._unknown_registry(tmp_path))
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        assert len(adapter._start_attempts.get(111, [])) == 3

    def test_fourth_start_silently_dropped(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._unknown_registry(tmp_path))
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        result = adapter.normalise(_start_update(chat_id=111), "telegram")
        assert result is None
        assert len(adapter._start_attempts.get(111, [])) == 3

    def test_rate_limit_is_per_chat_id(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._unknown_registry(tmp_path))
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        adapter.normalise(_start_update(chat_id=222), "telegram")
        assert len(adapter._start_attempts.get(222, [])) == 1

    def test_rate_limit_resets_after_window(self, tmp_path):
        import time as _time
        adapter = _make_input_adapter(member_registry=self._unknown_registry(tmp_path))
        for _ in range(3):
            adapter.normalise(_start_update(chat_id=111), "telegram")
        adapter._start_attempts[111] = [_time.time() - 3601] * 3
        adapter.normalise(_start_update(chat_id=111), "telegram")
        assert len(adapter._start_attempts.get(111, [])) == 1

    def test_authorized_user_start_never_rate_limited(self, tmp_path):
        adapter = _make_input_adapter(member_registry=self._known_registry(tmp_path, 111))
        for _ in range(5):
            result = adapter.normalise(_start_update(chat_id=111), "telegram")
            assert result is not None and result.text == "hi"
        assert len(adapter._start_attempts.get(111, [])) == 0


# ---------------------------------------------------------------------------
# Admin approve / block callbacks
# ---------------------------------------------------------------------------

class TestAdminApprove:
    def _registry_with_admin(self, tmp_path) -> MemberRegistry:
        return _make_registry(
            tmp_path,
            members=[
                {"id": "person_a", "display_name": "Abhiram", "role": "owner", "status": "active"},
                {"id": "person_b", "display_name": "Wife", "role": "member", "status": "active"},
            ],
            channels=[{"user_id": "person_a", "channel": "telegram", "channel_id": "999"}],
        )

    def test_admin_approve_registers_as_specified_user_id(self, tmp_path):
        registry = self._registry_with_admin(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        adapter.normalise(_admin_approve_callback(admin_chat_id=999, target_id=555, uid="person_b"), "telegram")
        assert registry.is_authorized("telegram", "555") is True
        member = registry.resolve_channel("telegram", "555")
        assert member is not None and member.id == "person_b"

    def test_admin_approve_returns_none(self, tmp_path):
        registry = self._registry_with_admin(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_admin_approve_callback(admin_chat_id=999), "telegram")
        assert result is None

    def test_admin_approve_no_registry_does_not_crash(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_admin_approve_callback(), "telegram")
        assert result is None

    def test_admin_deny_blocks_user(self, tmp_path):
        registry = self._registry_with_admin(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        adapter.normalise(_admin_deny_callback(admin_chat_id=999, target_id=666), "telegram")
        assert registry.is_authorized("telegram", "666") is False
        # Confirm it's blocked, not just unknown — register shouldn't override a block
        registry.register_channel("person_a", "telegram", "666")
        assert registry.is_authorized("telegram", "666") is False

    def test_admin_deny_returns_none(self, tmp_path):
        registry = self._registry_with_admin(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        result = adapter.normalise(_admin_deny_callback(admin_chat_id=999), "telegram")
        assert result is None

    def test_admin_deny_no_registry_does_not_crash(self):
        adapter = _make_input_adapter()
        result = adapter.normalise(_admin_deny_callback(), "telegram")
        assert result is None

    def test_approved_user_can_send_messages(self, tmp_path):
        registry = self._registry_with_admin(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        adapter.normalise(_admin_approve_callback(admin_chat_id=999, target_id=555, uid="person_b"), "telegram")
        result = adapter.normalise(_text_update(chat_id=555), "telegram")
        assert result is not None

    def test_denied_user_blocked_even_after_explicit_register(self, tmp_path):
        registry = self._registry_with_admin(tmp_path)
        adapter = _make_input_adapter(member_registry=registry)
        adapter.normalise(_admin_deny_callback(admin_chat_id=999, target_id=666), "telegram")
        registry.register_channel("person_a", "telegram", "666")
        result = adapter.normalise(_text_update(chat_id=666), "telegram")
        assert result is None


# ---------------------------------------------------------------------------
# _request_admin_approval — async (respx)
# ---------------------------------------------------------------------------

class TestAdminNotification:
    @pytest.mark.asyncio
    async def test_request_admin_approval_sends_member_buttons(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MITHRA_ADMIN_TELEGRAM_ID", "999")
        captured: list[dict] = []

        def capture(request, route):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        registry = _make_registry(
            tmp_path,
            members=[
                {"id": "person_a", "display_name": "Abhiram", "role": "owner", "status": "active"},
                {"id": "person_b", "display_name": "Wife", "role": "member", "status": "active"},
            ],
        )

        with respx.mock:
            respx.post("https://api.telegram.org/bottest-token/sendMessage").mock(
                side_effect=capture
            )
            adapter = _make_input_adapter(member_registry=registry)
            await adapter._request_admin_approval(
                chat_id=555, first_name="Alice", username="alice99"
            )

        assert len(captured) == 1
        body = captured[0]
        assert body["chat_id"] == 999
        assert "555" in body["text"]
        assert "Alice" in body["text"]

        all_buttons = [btn for row in body["reply_markup"]["inline_keyboard"] for btn in row]

        for btn in all_buttons:
            assert len(btn["callback_data"].encode()) <= 64, (
                f"callback_data too long: {btn['callback_data']!r}"
            )

        actions = [json.loads(b["callback_data"])["action"] for b in all_buttons]
        assert "admin_approve" in actions
        assert "admin_deny" in actions

        # 2 approve buttons (one per active member) + 1 deny
        assert len(all_buttons) == 3

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
