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
