"""Behavioural tests for LiteLLMRouterAdapter.

Uses a MockHTTPClient so no real network calls are made.
"""

from __future__ import annotations

import json

import pytest

from mithra.routing.llm_router import (
    LiteLLMRouterAdapter,
    TaskType,
    IntentClassification,
)
from mithra.storage.local import LocalStorageAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_response(content: str = "ok", prompt_tokens: int = 10, completion_tokens: int = 5) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class MockHTTPResponse:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class MockHTTPClient:
    """Captures POST calls and returns configurable responses."""

    def __init__(
        self,
        response: dict | None = None,
        fail: bool = False,
        fail_first_call: bool = False,
    ):
        self.calls: list[dict] = []
        self._response = response or _ok_response()
        self._fail = fail
        self._fail_first_call = fail_first_call

    def post(self, url: str, json_body: dict, headers: dict | None = None) -> MockHTTPResponse:
        call_index = len(self.calls)
        self.calls.append({"url": url, "body": json_body, "headers": headers})
        if self._fail or (self._fail_first_call and call_index == 0):
            raise Exception("Connection refused")
        return MockHTTPResponse(self._response)


def _make_router(
    http_client: MockHTTPClient | None = None,
    storage=None,
    fail: bool = False,
    fail_first_call: bool = False,
    response: dict | None = None,
) -> tuple[LiteLLMRouterAdapter, MockHTTPClient]:
    client = http_client or MockHTTPClient(fail=fail, fail_first_call=fail_first_call, response=response)
    router = LiteLLMRouterAdapter(
        base_url="http://test-llm:4000",
        storage_adapter=storage,
        http_client=client,
    )
    return router, client


def _messages(text: str = "hello") -> list[dict]:
    return [{"role": "user", "content": text}]


# ---------------------------------------------------------------------------
# Routing — model selection
# ---------------------------------------------------------------------------


class TestModelRouting:
    def test_local_task_type_routes_to_phi3_mini(self):
        router, client = _make_router()
        router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        assert client.calls[0]["body"]["model"] == "ollama/phi3-mini"

    def test_rule_based_alert_routes_to_phi3_mini(self):
        router, client = _make_router()
        router.complete(TaskType.RULE_BASED_ALERT, "home", _messages())
        assert client.calls[0]["body"]["model"] == "ollama/phi3-mini"

    def test_simple_retrieval_routes_to_phi3_mini(self):
        router, client = _make_router()
        router.complete(TaskType.SIMPLE_RETRIEVAL, "food", _messages())
        assert client.calls[0]["body"]["model"] == "ollama/phi3-mini"

    def test_cloud_task_type_routes_to_claude(self):
        router, client = _make_router()
        router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", _messages())
        assert client.calls[0]["body"]["model"] == "claude-sonnet-4-6"

    def test_document_parsing_routes_to_claude(self):
        router, client = _make_router()
        router.complete(TaskType.DOCUMENT_PARSING, "documents", _messages())
        assert client.calls[0]["body"]["model"] == "claude-sonnet-4-6"

    def test_route_taken_is_local_for_phi3(self):
        router, client = _make_router()
        result = router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        assert result.route_taken == "local"

    def test_route_taken_is_cloud_for_claude(self):
        router, client = _make_router()
        result = router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", _messages())
        assert result.route_taken == "cloud"


# ---------------------------------------------------------------------------
# Health domain guard
# ---------------------------------------------------------------------------


class TestHealthDomainGuard:
    def test_health_domain_forces_health_domain_task_type(self):
        router, client = _make_router()
        result = router.complete(TaskType.DATA_CAPTURE, "health", _messages())
        # Must route to phi3-mini (local_only), never cloud
        assert client.calls[0]["body"]["model"] == "ollama/phi3-mini"
        assert result.route_taken == "local_only"

    def test_health_domain_cloud_task_type_overridden(self):
        router, client = _make_router()
        result = router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "health", _messages())
        assert client.calls[0]["body"]["model"] == "ollama/phi3-mini"
        assert result.task_type == TaskType.HEALTH_DOMAIN

    def test_health_task_type_result_is_local_only(self):
        router, client = _make_router()
        result = router.complete(TaskType.HEALTH_DOMAIN, "health", _messages())
        assert result.route_taken == "local_only"

    def test_health_domain_mismatch_logged_to_observability(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(storage=storage)
        router.complete(TaskType.DATA_CAPTURE, "health", _messages())
        records = storage.list("observability", "history", "system")
        security_events = [r for r in records if r.data.get("record_type") == "security_event"]
        assert len(security_events) == 1
        assert security_events[0].data["event_type"] == "health_domain_task_type_mismatch"

    def test_health_domain_never_falls_back_to_cloud_on_failure(self):
        router, client = _make_router(fail=True)
        result = router.complete(TaskType.HEALTH_DOMAIN, "health", _messages())
        # Only one call attempted — no cloud fallback
        assert len(client.calls) == 1
        assert result.success is False
        assert result.fallback_triggered is False

    def test_health_domain_with_mismatched_task_type_still_no_fallback(self):
        # Even if task_type is one that normally allows fallback, health domain blocks it
        router, client = _make_router(fail=True)
        result = router.complete(TaskType.INTENT_CLASSIFICATION, "health", _messages())
        assert len(client.calls) == 1
        assert result.fallback_triggered is False


# ---------------------------------------------------------------------------
# Sanitisation
# ---------------------------------------------------------------------------


class TestSanitisation:
    def test_cloud_route_sanitises_messages(self):
        router, client = _make_router()
        msgs = [{"role": "user", "content": "email alice@example.com about the meeting"}]
        router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", msgs)
        sent_content = client.calls[0]["body"]["messages"][0]["content"]
        assert "alice@example.com" not in sent_content
        assert "[EMAIL]" in sent_content

    def test_cloud_sanitisation_report_in_response(self):
        router, _ = _make_router()
        msgs = [{"role": "user", "content": "ring 555-123-4567"}]
        result = router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", msgs)
        assert result.sanitisation is not None

    def test_local_route_does_not_sanitise(self):
        router, client = _make_router()
        msgs = [{"role": "user", "content": "email alice@example.com"}]
        router.complete(TaskType.DATA_CAPTURE, "food", msgs)
        # Original content must reach the model unchanged
        assert client.calls[0]["body"]["messages"][0]["content"] == "email alice@example.com"

    def test_local_route_sanitisation_is_none(self):
        router, _ = _make_router()
        result = router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        assert result.sanitisation is None


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


class TestFallback:
    def test_fallback_triggered_when_local_fails(self):
        router, client = _make_router(fail_first_call=True)
        result = router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        assert result.fallback_triggered is True
        assert result.success is True
        # Two calls: first failed, second succeeded with cloud model
        assert len(client.calls) == 2
        assert client.calls[1]["body"]["model"] == "claude-sonnet-4-6"

    def test_fallback_route_taken_label(self):
        router, client = _make_router(fail_first_call=True)
        result = router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        assert result.route_taken == "fallback"

    def test_fallback_not_triggered_for_health_domain(self):
        router, client = _make_router(fail=True)
        result = router.complete(TaskType.DATA_CAPTURE, "health", _messages())
        assert result.fallback_triggered is False
        assert len(client.calls) == 1

    def test_cloud_task_type_does_not_fallback(self):
        router, client = _make_router(fail=True)
        result = router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", _messages())
        assert result.fallback_triggered is False
        assert len(client.calls) == 1
        assert result.success is False

    def test_fallback_sanitises_for_cloud(self):
        router, client = _make_router(fail_first_call=True)
        msgs = [{"role": "user", "content": "ring 555-123-4567"}]
        result = router.complete(TaskType.DATA_CAPTURE, "food", msgs)
        # The fallback call goes to cloud — it must be sanitised
        assert result.sanitisation is not None
        fallback_content = client.calls[1]["body"]["messages"][0]["content"]
        assert "555-123-4567" not in fallback_content

    def test_fallback_logged_to_observability(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(fail_first_call=True, storage=storage)
        router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        records = storage.list("observability", "history", "system")
        fallback_events = [r for r in records if r.data.get("record_type") == "fallback_event"]
        assert len(fallback_events) == 1


# ---------------------------------------------------------------------------
# RoutingEvent persistence
# ---------------------------------------------------------------------------


class TestRoutingEventPersistence:
    def test_routing_event_written_on_successful_call(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(storage=storage)
        router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        records = storage.list("observability", "history", "system")
        events = [r for r in records if r.data.get("record_type") == "routing_event"]
        assert len(events) == 1
        assert events[0].data["domain"] == "food"
        assert events[0].data["model_used"] == "ollama/phi3-mini"

    def test_routing_event_written_on_failed_call(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(fail=True, storage=storage)
        router.complete(TaskType.HEALTH_DOMAIN, "health", _messages())
        records = storage.list("observability", "history", "system")
        events = [r for r in records if r.data.get("record_type") == "routing_event"]
        assert len(events) == 1
        assert events[0].data["success"] is False

    def test_routing_event_has_task_type_mismatch_flag(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(storage=storage)
        router.complete(TaskType.DATA_CAPTURE, "health", _messages())
        records = storage.list("observability", "history", "system")
        events = [r for r in records if r.data.get("record_type") == "routing_event"]
        assert events[0].data["task_type_mismatch"] is True

    def test_routing_event_sanitisation_applied_flag(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(storage=storage)
        router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", _messages())
        records = storage.list("observability", "history", "system")
        events = [r for r in records if r.data.get("record_type") == "routing_event"]
        assert events[0].data["sanitisation_applied"] is True

    def test_no_storage_does_not_raise(self):
        router, _ = _make_router(storage=None)
        result = router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        assert result.success is True


# ---------------------------------------------------------------------------
# classify_intent
# ---------------------------------------------------------------------------


class TestClassifyIntent:
    def test_returns_none_when_confidence_below_threshold(self):
        low_conf = _ok_response(
            content=json.dumps({"intent": "food.query", "domain": "food", "confidence": 0.5, "alternatives": []})
        )
        router, _ = _make_router(response=low_conf)
        result = router.classify_intent("what should i eat?", {})
        assert result is None

    def test_returns_intent_classification_when_confident(self):
        high_conf = _ok_response(
            content=json.dumps({"intent": "food.query", "domain": "food", "confidence": 0.9, "alternatives": []})
        )
        router, _ = _make_router(response=high_conf)
        result = router.classify_intent("what should i eat?", {})
        assert isinstance(result, IntentClassification)
        assert result.intent == "food.query"
        assert result.domain == "food"
        assert result.confidence == 0.9

    def test_returns_none_on_malformed_json(self):
        bad_response = _ok_response(content="not json at all")
        router, _ = _make_router(response=bad_response)
        result = router.classify_intent("anything", {})
        assert result is None

    def test_returns_none_on_llm_failure(self):
        router, _ = _make_router(fail=True)
        result = router.classify_intent("test", {})
        assert result is None

    def test_confidence_exactly_at_threshold_returns_classification(self):
        at_threshold = _ok_response(
            content=json.dumps({"intent": "home.query", "domain": "home", "confidence": 0.75, "alternatives": []})
        )
        router, _ = _make_router(response=at_threshold)
        result = router.classify_intent("turn off lights", {})
        assert result is not None
        assert result.confidence == 0.75


# ---------------------------------------------------------------------------
# get_usage
# ---------------------------------------------------------------------------


class TestGetUsage:
    def test_aggregates_routing_events_correctly(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(storage=storage)
        # Two local calls
        router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        router.complete(TaskType.SIMPLE_RETRIEVAL, "home", _messages())
        # One cloud call
        router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", _messages())
        usage = router.get_usage()
        assert usage.local_calls == 2
        assert usage.cloud_calls == 1
        assert usage.total_tokens == usage.local_tokens + usage.cloud_tokens

    def test_fallback_counted(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(fail_first_call=True, storage=storage)
        router.complete(TaskType.DATA_CAPTURE, "food", _messages())
        usage = router.get_usage()
        assert usage.fallback_count == 1

    def test_empty_storage_returns_zero_usage(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(storage=storage)
        usage = router.get_usage()
        assert usage.total_tokens == 0
        assert usage.local_calls == 0
        assert usage.cloud_calls == 0

    def test_no_storage_returns_empty_usage(self):
        router, _ = _make_router(storage=None)
        usage = router.get_usage()
        assert usage.total_tokens == 0

    def test_estimated_cost_is_cloud_tokens_times_rate(self, tmp_path):
        storage = LocalStorageAdapter(str(tmp_path / "files"), str(tmp_path / "db.sqlite"))
        router, _ = _make_router(storage=storage, response=_ok_response(prompt_tokens=100, completion_tokens=50))
        router.complete(TaskType.CROSS_DOMAIN_INSIGHT, "money", _messages())
        usage = router.get_usage()
        expected_cost = usage.cloud_tokens * 0.000003
        assert abs(usage.estimated_cost_usd - expected_cost) < 1e-9
