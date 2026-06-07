"""Tests for mithra/api/gateway.py."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from mithra.agent.interface import AgentIntent, AgentResult, InputChannel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_agent(response_text: str = "Done.") -> MagicMock:
    agent = MagicMock()
    agent.health.return_value = {"status": "ok", "registered_tools": [], "tool_count": 0}
    agent.run.return_value = AgentResult(
        request_id="req-test",
        success=True,
        intent=AgentIntent.SYSTEM_UNKNOWN,
        domain="system",
        confidence=1.0,
        actions=[],
        response_text=response_text,
    )
    return agent


def _mock_engine() -> MagicMock:
    engine = MagicMock()
    receipt = MagicMock()
    receipt.request_id = "req-test"
    receipt.notification_sent = False
    engine.process.return_value = receipt
    return engine


def _mock_executor() -> MagicMock:
    return MagicMock()


def _mock_domain_router() -> MagicMock:
    dr = MagicMock()
    dr.health.return_value = {"status": "ok"}
    ctx = MagicMock()
    dr.assemble_context.return_value = ctx
    return dr


def _mock_llm_router() -> MagicMock:
    lr = MagicMock()
    health = MagicMock()
    health.litellm_reachable = True
    lr.health.return_value = health
    return lr


def _mock_telegram_input() -> MagicMock:
    ti = MagicMock()
    ti.normalise.return_value = None
    return ti


def _make_test_client(extra_state: dict | None = None) -> TestClient:
    """Create a TestClient with a pre-populated state (bypasses lifespan startup)."""
    from mithra.api import gateway as gw

    state: dict[str, Any] = {
        "agent": _mock_agent(),
        "engine": _mock_engine(),
        "executor": _mock_executor(),
        "domain_router": _mock_domain_router(),
        "llm_router": _mock_llm_router(),
        "context_cache": MagicMock(health=MagicMock(return_value={"status": "ok"})),
        "local_adapter": MagicMock(health=MagicMock(return_value={"status": "ok"})),
        "telegram_input": _mock_telegram_input(),
    }
    if extra_state:
        state.update(extra_state)

    gw._state.clear()
    gw._state.update(state)

    # Build app without running lifespan
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as TC

    app = gw.create_app(state_override=state)

    # Use TestClient without triggering lifespan (app_state already set)
    return TC(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /api/v1/health (unauthenticated)
# ---------------------------------------------------------------------------

class TestHealthRoute:
    def test_health_returns_200(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        client = _make_test_client()
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_health_includes_agent_status(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        client = _make_test_client()
        data = client.get("/api/v1/health").json()
        assert "agent" in data
        assert data["agent"]["status"] == "ok"

    def test_health_includes_overall_status(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        client = _make_test_client()
        data = client.get("/api/v1/health").json()
        assert "status" in data
        assert data["status"] in ("ok", "degraded", "error")

    def test_health_no_auth_required(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "super-secret")
        client = _make_test_client()
        # No Authorization header -- should still return 200
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/agent (authenticated)
# ---------------------------------------------------------------------------

class TestAgentRoute:
    def test_agent_accepts_request_returns_result(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        client = _make_test_client()
        resp = client.post(
            "/api/v1/agent",
            json={"user_id": "person_a", "device_id": "phone_a", "text": "update pantry"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert data["result"]["success"] is True

    def test_agent_returns_response_text(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        agent = _mock_agent(response_text="Pantry updated!")
        client = _make_test_client({"agent": agent})
        resp = client.post(
            "/api/v1/agent",
            json={"user_id": "person_a", "device_id": "api", "text": "update pantry"},
        )
        assert resp.json()["result"]["response_text"] == "Pantry updated!"

    def test_agent_missing_auth_returns_401(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "super-secret")
        client = _make_test_client()
        resp = client.post(
            "/api/v1/agent",
            json={"user_id": "person_a", "device_id": "api", "text": "hi"},
        )
        assert resp.status_code == 401

    def test_agent_with_correct_auth_passes(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "super-secret")
        client = _make_test_client()
        resp = client.post(
            "/api/v1/agent",
            json={"user_id": "person_a", "device_id": "api", "text": "hi"},
            headers={"Authorization": "Bearer super-secret"},
        )
        assert resp.status_code == 200

    def test_agent_wrong_token_returns_401(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "super-secret")
        client = _make_test_client()
        resp = client.post(
            "/api/v1/agent",
            json={"user_id": "person_a", "device_id": "api", "text": "hi"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/webhook/telegram
# ---------------------------------------------------------------------------

class TestWebhookTelegramRoute:
    def test_webhook_returns_ok_true(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        client = _make_test_client()
        resp = client.post(
            "/api/v1/webhook/telegram",
            json={
                "update_id": 1,
                "message": {
                    "message_id": 42,
                    "chat": {"id": 111, "type": "private"},
                    "text": "hello",
                },
            },
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_webhook_with_no_message_returns_ok(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        client = _make_test_client()
        resp = client.post(
            "/api/v1/webhook/telegram",
            json={"update_id": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_webhook_calls_agent_when_request_normalised(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        from mithra.agent.interface import AgentRequest as AR

        mock_request = AR(
            user_id="household",
            device_id="telegram:111",
            input_channel=InputChannel.TELEGRAM,
            text="hello",
            request_id="req-webhook",
        )

        telegram_input = MagicMock()
        telegram_input.normalise.return_value = mock_request

        agent = _mock_agent()
        engine = _mock_engine()
        executor = _mock_executor()

        client = _make_test_client(
            {
                "telegram_input": telegram_input,
                "agent": agent,
                "engine": engine,
                "executor": executor,
            }
        )
        client.post(
            "/api/v1/webhook/telegram",
            json={"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111}, "text": "hi"}},
        )
        agent.run.assert_called_once()


# ---------------------------------------------------------------------------
# _is_capture_response — unit tests
# ---------------------------------------------------------------------------

class TestIsCaptureResponse:
    def test_day_name_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("friday") is True

    def test_priority_keyword_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("urgent") is True
        assert _is_capture_response("high") is True
        assert _is_capture_response("normal") is True
        assert _is_capture_response("someday") is True

    def test_skip_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("skip") is True
        assert _is_capture_response("done") is True

    def test_iso_date_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("2026-07-15") is True

    def test_month_day_ordinal_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("July 15th") is True
        assert _is_capture_response("july 15th") is True  # case-insensitive

    def test_month_day_plain_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("July 15") is True
        assert _is_capture_response("Dec 3") is True

    def test_month_day_abbreviated_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("Dec 3rd") is True
        assert _is_capture_response("Jan 1st") is True

    def test_this_next_phrases_are_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("next week") is True
        assert _is_capture_response("this month") is True
        assert _is_capture_response("next friday") is True

    def test_in_n_days_is_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("in 3 days") is True
        assert _is_capture_response("in 2 weeks") is True

    def test_new_command_is_not_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("add task call doctor") is False

    def test_question_with_date_is_not_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("what should I do today") is False

    def test_full_sentence_is_not_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("remind me to fix the fence tomorrow morning") is False

    def test_empty_is_not_capture(self):
        from mithra.api.gateway import _is_capture_response
        assert _is_capture_response("") is False


# ---------------------------------------------------------------------------
# Capture context ordering — agent.run() must not be called when capture active
# ---------------------------------------------------------------------------

class TestCaptureContextOrdering:
    def test_agent_not_called_when_capture_handles_reply(self, tmp_path):
        """With active capture state and a capture response text, agent.run()
        must never be called — the gateway returns early at step 0."""
        import asyncio
        import sqlite3
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel
        from mithra.storage.local import LocalStorageAdapter
        from mithra.domains.tasks.tools import _store_capture_state

        # Real DB with tasks table and a pending capture state
        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        db_path = str(tmp_path / "test.db")
        now = "2026-06-06T00:00:00+00:00"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id, household_id, user_id, title, scope, priority, status, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("tsk_ord1", "hh_test", "usr_test", "Fix fan", "personal", "normal", "inbox", "manual", now, now),
            )
        _store_capture_state(db_path, "usr_test", "tsk_ord1", "due_date", {})

        mock_agent = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter.db_path = db_path

        gw._state.clear()
        gw._state.update({
            "agent": mock_agent,
            "local_adapter": mock_adapter,
        })

        request = AR(
            user_id="usr_test",
            device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="friday",
        )
        asyncio.run(gw._process_request(request))

        mock_agent.run.assert_not_called()

    def test_agent_called_when_text_is_new_command(self, tmp_path):
        """When the user sends a new command (not a capture response),
        the gateway must call agent.run() even if capture state exists."""
        import asyncio
        import sqlite3
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel, AgentIntent, AgentResult
        from mithra.storage.local import LocalStorageAdapter
        from mithra.domains.tasks.tools import _store_capture_state

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        db_path = str(tmp_path / "test.db")
        now = "2026-06-06T00:00:00+00:00"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id, household_id, user_id, title, scope, priority, status, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("tsk_ord2", "hh_test", "usr_test", "Fix fan", "personal", "normal", "inbox", "manual", now, now),
            )
        _store_capture_state(db_path, "usr_test", "tsk_ord2", "due_date", {})

        mock_agent = MagicMock()
        mock_agent.run.return_value = AgentResult(
            request_id="r", success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN, domain="tasks",
            confidence=0.9, actions=[], response_text="Done.",
        )
        mock_adapter = MagicMock()
        mock_adapter.db_path = db_path

        gw._state.clear()
        gw._state.update({
            "agent": mock_agent,
            "local_adapter": mock_adapter,
        })

        request = AR(
            user_id="usr_test",
            device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="add task call doctor",  # new command — not a capture response
        )
        asyncio.run(gw._process_request(request))

        mock_agent.run.assert_called_once()


# ---------------------------------------------------------------------------
# _check_and_apply_capture — notes step interception
# ---------------------------------------------------------------------------

class TestCheckAndApplyCapture:
    def _setup_db(self, tmp_path, task_id: str, step: str):
        """Create a real DB with one task and a capture state."""
        import sqlite3
        from mithra.storage.local import LocalStorageAdapter
        from mithra.domains.tasks.tools import _store_capture_state

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        db_path = str(tmp_path / "test.db")
        now = "2026-06-06T00:00:00+00:00"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id, household_id, user_id, title, scope, priority, status, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, "hh_test", "usr_test", "Fix fan", "personal", "normal", "active", "manual", now, now),
            )
        _store_capture_state(db_path, "usr_test", task_id, step, {})
        return db_path

    def test_notes_step_intercepts_plain_sentence(self, tmp_path):
        """Any non-command sentence during notes step is captured as a note."""
        import asyncio
        from unittest.mock import AsyncMock
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel

        db_path = self._setup_db(tmp_path, "tsk_notes1", "notes")
        handler = MagicMock()
        handler.send = AsyncMock()

        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="Amazon gave debug instructions check email",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, handler))
        assert result is True

    def test_notes_step_new_command_falls_through(self, tmp_path):
        """A new-command prefix during notes step clears state and returns False."""
        import asyncio
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel
        from mithra.domains.tasks.tools import _get_capture_state

        db_path = self._setup_db(tmp_path, "tsk_notes2", "notes")
        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="add task call doctor",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, None))
        assert result is False
        # capture state must be cleared
        assert _get_capture_state(db_path, "usr_test") is None

    def test_due_date_step_rejects_sentence(self, tmp_path):
        """A full sentence during due_date step clears state and returns False."""
        import asyncio
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel
        from mithra.domains.tasks.tools import _get_capture_state

        db_path = self._setup_db(tmp_path, "tsk_dd1", "due_date")
        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="remind me about the fence tomorrow morning",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, None))
        assert result is False
        assert _get_capture_state(db_path, "usr_test") is None

    def test_due_date_step_accepts_friday(self, tmp_path):
        """'friday' during due_date step is intercepted."""
        import asyncio
        from unittest.mock import AsyncMock
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel

        db_path = self._setup_db(tmp_path, "tsk_dd2", "due_date")
        handler = MagicMock()
        handler.send = AsyncMock()

        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="friday",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, handler))
        assert result is True

    def test_no_active_state_returns_false(self, tmp_path):
        """When no capture state exists, always returns False immediately."""
        import asyncio
        from mithra.api import gateway as gw
        from mithra.storage.local import LocalStorageAdapter
        from mithra.agent.interface import AgentRequest as AR, InputChannel

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        db_path = str(tmp_path / "test.db")
        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="friday",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, None))
        assert result is False


# ---------------------------------------------------------------------------
# Milestone capture flow — _apply_milestone_capture + gateway gate
# ---------------------------------------------------------------------------

class TestMilestoneCaptureGateway:
    def _setup_project(self, tmp_path, project_id: str, title: str = "Backyard Deck"):
        """Create a real DB with one project and a milestone capture state."""
        import sqlite3
        from datetime import datetime, timezone
        from mithra.storage.local import LocalStorageAdapter
        from mithra.domains.tasks.tools import _store_capture_state

        LocalStorageAdapter(str(tmp_path), str(tmp_path / "test.db"))
        db_path = str(tmp_path / "test.db")
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO projects (id, household_id, user_id, title, scope, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (project_id, "hh_test", "usr_test", title, "household", "active", now, now),
            )
        _store_capture_state(db_path, "usr_test", project_id, "milestone", {})
        return db_path

    def test_comma_list_creates_milestones(self, tmp_path):
        import asyncio
        import sqlite3
        from unittest.mock import AsyncMock
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel

        db_path = self._setup_project(tmp_path, "prj_mc1")
        handler = MagicMock()
        handler.send = AsyncMock()

        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="buy materials, build frame, lay boards, stain and finish",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, handler))
        assert result is True

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT title FROM milestones WHERE project_id='prj_mc1' ORDER BY order_index"
            ).fetchall()
        assert len(rows) == 4
        assert rows[0][0] == "buy materials"
        assert rows[3][0] == "stain and finish"

    def test_comma_list_clears_capture_state(self, tmp_path):
        import asyncio
        from unittest.mock import AsyncMock
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel
        from mithra.domains.tasks.tools import _get_capture_state

        db_path = self._setup_project(tmp_path, "prj_mc2")
        handler = MagicMock()
        handler.send = AsyncMock()

        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="Phase 1, Phase 2",
        )
        asyncio.run(gw._check_and_apply_capture(request, db_path, handler))
        assert _get_capture_state(db_path, "usr_test") is None

    def test_comma_list_sends_confirmation(self, tmp_path):
        import asyncio
        from unittest.mock import AsyncMock
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel

        db_path = self._setup_project(tmp_path, "prj_mc3", title="Garden Wall")
        handler = MagicMock()
        handler.send = AsyncMock()

        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="Dig foundation, Pour concrete",
        )
        asyncio.run(gw._check_and_apply_capture(request, db_path, handler))
        handler.send.assert_called_once()
        response_text = handler.send.call_args[0][1]
        assert "Garden Wall" in response_text
        assert "2 milestone(s)" in response_text

    def test_skip_clears_state_and_confirms(self, tmp_path):
        import asyncio
        from unittest.mock import AsyncMock
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel
        from mithra.domains.tasks.tools import _get_capture_state

        db_path = self._setup_project(tmp_path, "prj_mc4")
        handler = MagicMock()
        handler.send = AsyncMock()

        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="skip",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, handler))
        assert result is True
        assert _get_capture_state(db_path, "usr_test") is None
        handler.send.assert_called_once()
        assert "saved" in handler.send.call_args[0][1].lower()

    def test_new_command_falls_through(self, tmp_path):
        import asyncio
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel
        from mithra.domains.tasks.tools import _get_capture_state

        db_path = self._setup_project(tmp_path, "prj_mc5")
        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="add task call doctor",
        )
        result = asyncio.run(gw._check_and_apply_capture(request, db_path, None))
        assert result is False
        assert _get_capture_state(db_path, "usr_test") is None

    def test_order_index_sequential(self, tmp_path):
        import asyncio
        import sqlite3
        from unittest.mock import AsyncMock
        from mithra.api import gateway as gw
        from mithra.agent.interface import AgentRequest as AR, InputChannel

        db_path = self._setup_project(tmp_path, "prj_mc6")
        handler = MagicMock()
        handler.send = AsyncMock()

        request = AR(
            user_id="usr_test", device_id="phone",
            input_channel=InputChannel.TELEGRAM,
            text="Step A, Step B, Step C",
        )
        asyncio.run(gw._check_and_apply_capture(request, db_path, handler))
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT order_index FROM milestones WHERE project_id='prj_mc6' ORDER BY order_index"
            ).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3]
