"""Structural and behavioural tests for mithra/routing/llm_router.py."""

import dataclasses
import inspect

import pytest

from mithra.routing.llm_router import (
    IntentClassification,
    LLMResponse,
    LLMRouter,
    LLMStreamChunk,
    RouterHealth,
    RoutingEvent,
    TaskType,
    UsageReport,
)
from mithra.storage.sanitiser import SanitisationReport


class TestTaskType:
    def test_health_domain_value(self):
        assert TaskType.HEALTH_DOMAIN.value == "health_domain"

    def test_local_only_types_exist(self):
        assert TaskType.DATA_CAPTURE.value == "data_capture"
        assert TaskType.RULE_BASED_ALERT.value == "rule_based_alert"
        assert TaskType.SIMPLE_RETRIEVAL.value == "simple_retrieval"

    def test_local_with_fallback_types_exist(self):
        assert TaskType.INTENT_CLASSIFICATION.value == "intent_classification"
        assert TaskType.VOICE_DISAMBIGUATION.value == "voice_disambiguation"

    def test_cloud_types_exist(self):
        cloud = {
            TaskType.CROSS_DOMAIN_INSIGHT,
            TaskType.NATURAL_LANGUAGE_DRAFTING,
            TaskType.DOCUMENT_PARSING,
            TaskType.MONTHLY_SUMMARY,
            TaskType.WEB_UNDERSTANDING,
        }
        assert len(cloud) == 5

    def test_eleven_task_types_total(self):
        assert len(list(TaskType)) == 11


class TestLLMResponse:
    _REQUIRED_FIELDS = {
        "content", "model_used", "route_taken", "task_type",
        "prompt_tokens", "completion_tokens", "latency_ms",
        "fallback_triggered", "sanitisation", "success", "error",
    }

    def test_has_all_required_fields(self):
        field_names = {f.name for f in dataclasses.fields(LLMResponse)}
        missing = self._REQUIRED_FIELDS - field_names
        assert not missing, f"LLMResponse missing: {missing}"

    def test_fallback_triggered_defaults_to_false(self):
        r = LLMResponse(
            content="hi",
            model_used="phi3-mini",
            route_taken="local",
            task_type=TaskType.DATA_CAPTURE,
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=100,
        )
        assert r.fallback_triggered is False

    def test_sanitisation_defaults_to_none(self):
        r = LLMResponse(
            content="hi", model_used="phi3-mini", route_taken="local",
            task_type=TaskType.DATA_CAPTURE, prompt_tokens=0,
            completion_tokens=0, latency_ms=0,
        )
        assert r.sanitisation is None

    def test_sanitisation_accepts_report(self):
        report = SanitisationReport(pass1_hits=1)
        r = LLMResponse(
            content="hi", model_used="phi3-mini", route_taken="local",
            task_type=TaskType.DATA_CAPTURE, prompt_tokens=0,
            completion_tokens=0, latency_ms=0, sanitisation=report,
        )
        assert r.sanitisation is report

    def test_success_defaults_to_true(self):
        r = LLMResponse(
            content="hi", model_used="phi3-mini", route_taken="local",
            task_type=TaskType.DATA_CAPTURE, prompt_tokens=0,
            completion_tokens=0, latency_ms=0,
        )
        assert r.success is True

    def test_error_defaults_to_none(self):
        r = LLMResponse(
            content="hi", model_used="phi3-mini", route_taken="local",
            task_type=TaskType.DATA_CAPTURE, prompt_tokens=0,
            completion_tokens=0, latency_ms=0,
        )
        assert r.error is None


class TestLLMStreamChunk:
    def test_has_delta_model_used_finished(self):
        chunk = LLMStreamChunk(delta="hello", model_used="phi3-mini")
        assert chunk.delta == "hello"
        assert chunk.model_used == "phi3-mini"
        assert chunk.finished is False

    def test_finished_defaults_to_false(self):
        chunk = LLMStreamChunk(delta="x", model_used="phi3-mini")
        assert chunk.finished is False


class TestRoutingEvent:
    def test_has_task_type_mismatch_field(self):
        field_names = {f.name for f in dataclasses.fields(RoutingEvent)}
        assert "task_type_mismatch" in field_names

    def test_has_sanitisation_applied_field(self):
        field_names = {f.name for f in dataclasses.fields(RoutingEvent)}
        assert "sanitisation_applied" in field_names

    def test_task_type_mismatch_is_bool(self):
        event = RoutingEvent(
            event_id="e1", timestamp="2026-01-01T00:00:00+00:00",
            task_type=TaskType.DATA_CAPTURE, domain="food",
            model_used="phi3-mini", route_taken="local",
            fallback_triggered=False, task_type_mismatch=True,
            sanitisation_applied=False,
            prompt_tokens=10, completion_tokens=5, latency_ms=50,
            success=True,
        )
        assert event.task_type_mismatch is True


class TestUsageReport:
    def test_has_local_calls_cloud_calls_fallback_count(self):
        field_names = {f.name for f in dataclasses.fields(UsageReport)}
        assert "local_calls" in field_names
        assert "cloud_calls" in field_names
        assert "fallback_count" in field_names

    def test_all_three_default_to_zero(self):
        r = UsageReport(
            period="month", total_tokens=0, cloud_tokens=0,
            local_tokens=0, estimated_cost_usd=0.0,
        )
        assert r.local_calls == 0
        assert r.cloud_calls == 0
        assert r.fallback_count == 0


class TestRouterHealth:
    def test_has_fallback_active_field(self):
        field_names = {f.name for f in dataclasses.fields(RouterHealth)}
        assert "fallback_active" in field_names

    def test_has_latency_fields(self):
        field_names = {f.name for f in dataclasses.fields(RouterHealth)}
        assert "local_latency_ms" in field_names
        assert "cloud_latency_ms" in field_names

    def test_fallback_active_defaults_to_false(self):
        h = RouterHealth(
            litellm_reachable=True,
            cloud_model_available=True,
            local_model_available=True,
        )
        assert h.fallback_active is False
        assert h.local_latency_ms == 0
        assert h.cloud_latency_ms == 0


class TestIntentClassification:
    def test_has_all_four_fields(self):
        ic = IntentClassification(
            intent="food.update_pantry",
            domain="food",
            confidence=0.9,
        )
        assert ic.intent == "food.update_pantry"
        assert ic.domain == "food"
        assert ic.confidence == 0.9
        assert ic.alternatives == []


class TestLLMRouterInterface:
    def test_complete_has_domain_as_required_parameter(self):
        sig = inspect.signature(LLMRouter.complete)
        params = sig.parameters
        assert "domain" in params
        # domain must not have a default — it is required
        assert params["domain"].default is inspect.Parameter.empty

    def test_classify_intent_return_annotation_allows_none(self):
        sig = inspect.signature(LLMRouter.classify_intent)
        annotation = str(sig.return_annotation)
        assert "None" in annotation or "Optional" in annotation

    def test_llm_router_is_abstract(self):
        with pytest.raises(TypeError):
            LLMRouter()  # type: ignore[abstract]
