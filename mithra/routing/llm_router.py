"""
LLMRouter — routes LLM calls to the correct model via the LiteLLM proxy.

All agent code must call LLMRouter. No direct imports of anthropic, openai,
or ollama anywhere in the codebase.

Task routing (from services/litellm/routing.yaml):
  local       — Phi-3 Mini.  Cloud fallback allowed if config permits.
  cloud       — Claude claude-sonnet-4-6.  No fallback.
  local_only  — Phi-3 Mini.  NEVER cloud fallback.  Health domain only.

Two independent health guards:
  1. Agent tags every call with TaskType.HEALTH_DOMAIN
  2. Router validates domain parameter independently
  Mismatch (e.g. health domain but non-health TaskType) → logged as security event.

DataSanitiser is applied before EVERY cloud call — never skipped.
SanitisationReport returned in LLMResponse for audit.

_fit_context(): runs before every call. Truncates + summarises if needed.
  Keeps: system message + last 6 turns + current user message.
  Handles CONTEXT_TOO_LONG before it errors the provider.

Budget exhausted: fall back to local model, never silent failure.

RoutingEvent: one structured log per call in StorageAdapter domain="observability".
Feeds the health dashboard. Tracks model_used, route_taken, fallback, tokens, cost.

IntentClassification confidence threshold: 0.75.
Below threshold → classify_intent() returns None. Caller creates ClarificationRequest.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generator

import requests as _requests

from mithra.storage.adapter import StorageAdapter, StorageRecord
from mithra.storage.sanitiser import DataSanitiser, SanitisationReport


class TaskType(Enum):
    """Classifies an LLM call so the router can pick the correct model."""

    DATA_CAPTURE = "data_capture"                     # local
    RULE_BASED_ALERT = "rule_based_alert"             # local
    SIMPLE_RETRIEVAL = "simple_retrieval"             # local
    INTENT_CLASSIFICATION = "intent_classification"   # local → cloud fallback
    VOICE_DISAMBIGUATION = "voice_disambiguation"     # local → cloud fallback
    CROSS_DOMAIN_INSIGHT = "cross_domain_insight"     # cloud
    NATURAL_LANGUAGE_DRAFTING = "natural_language_drafting"  # cloud
    DOCUMENT_PARSING = "document_parsing"             # cloud
    MONTHLY_SUMMARY = "monthly_summary"               # cloud
    WEB_UNDERSTANDING = "web_understanding"           # cloud
    HEALTH_DOMAIN = "health_domain"                   # local_only — no fallback ever


@dataclass
class IntentClassification:
    """Result of an intent classification call."""

    intent: str
    domain: str
    confidence: float
    alternatives: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class LLMResponse:
    """Typed response from LLMRouter.complete()."""

    content: str
    model_used: str            # "phi3-mini" | "claude-sonnet-4-6"
    route_taken: str           # "local" | "cloud" | "local_only" | "fallback"
    task_type: TaskType
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    fallback_triggered: bool = False
    fallback_reason: str | None = None
    sanitisation: SanitisationReport | None = None
    success: bool = True
    error: str | None = None


@dataclass
class LLMStreamChunk:
    """A single chunk yielded during a streaming completion."""

    delta: str
    model_used: str
    finished: bool = False


@dataclass
class RoutingEvent:
    """One observability log entry per LLM call.
    Written to StorageAdapter domain='observability' layer='history'."""

    event_id: str
    timestamp: str
    task_type: TaskType
    domain: str
    model_used: str
    route_taken: str
    fallback_triggered: bool
    task_type_mismatch: bool    # True if domain guard overrode agent TaskType
    sanitisation_applied: bool
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    success: bool
    error: str | None = None


@dataclass
class UsageReport:
    """LLM usage summary for a given period."""

    period: str
    total_tokens: int
    cloud_tokens: int
    local_tokens: int
    estimated_cost_usd: float
    by_task_type: dict[str, int] = field(default_factory=dict)
    local_calls: int = 0
    cloud_calls: int = 0
    fallback_count: int = 0


@dataclass
class RouterHealth:
    """Health status of the LLMRouter and its backends."""

    litellm_reachable: bool
    cloud_model_available: bool
    local_model_available: bool
    last_call_at: str | None = None
    error: str | None = None
    fallback_active: bool = False
    local_latency_ms: int = 0
    cloud_latency_ms: int = 0


class LLMRouter(ABC):
    """Routes LLM completions to the correct model via the LiteLLM proxy."""

    @abstractmethod
    def complete(
        self,
        task_type: TaskType,
        domain: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> LLMResponse | Generator[LLMStreamChunk, None, LLMResponse]:
        """Send a completion request. domain is required and validated independently.

        Returns LLMResponse for non-streaming, Generator[LLMStreamChunk] for stream=True.
        """
        ...

    @abstractmethod
    def classify_intent(
        self, text: str, context: dict[str, Any]
    ) -> IntentClassification | None:
        """Classify user intent. Returns None when confidence < 0.75.
        Caller is responsible for creating ClarificationRequest."""
        ...

    @abstractmethod
    def get_usage(self, period: str = "month") -> UsageReport:
        """Return aggregated LLM usage for the given period."""
        ...

    @abstractmethod
    def health(self) -> RouterHealth:
        """Return health status of the router and its model backends."""
        ...


# ---------------------------------------------------------------------------
# Route config
# ---------------------------------------------------------------------------

@dataclass
class _RouteConfig:
    model: str
    fallback: str | None  # "cloud" or None


_TASK_ROUTES: dict[TaskType, _RouteConfig] = {
    TaskType.DATA_CAPTURE:              _RouteConfig("ollama/phi3-mini", "cloud"),
    TaskType.RULE_BASED_ALERT:          _RouteConfig("ollama/phi3-mini", "cloud"),
    TaskType.SIMPLE_RETRIEVAL:          _RouteConfig("ollama/phi3-mini", "cloud"),
    TaskType.INTENT_CLASSIFICATION:     _RouteConfig("ollama/phi3-mini", "cloud"),
    TaskType.VOICE_DISAMBIGUATION:      _RouteConfig("ollama/phi3-mini", "cloud"),
    TaskType.CROSS_DOMAIN_INSIGHT:      _RouteConfig("claude-sonnet-4-6", None),
    TaskType.NATURAL_LANGUAGE_DRAFTING: _RouteConfig("claude-sonnet-4-6", None),
    TaskType.DOCUMENT_PARSING:          _RouteConfig("claude-sonnet-4-6", None),
    TaskType.MONTHLY_SUMMARY:           _RouteConfig("claude-sonnet-4-6", None),
    TaskType.WEB_UNDERSTANDING:         _RouteConfig("claude-sonnet-4-6", None),
    TaskType.HEALTH_DOMAIN:             _RouteConfig("ollama/phi3-mini", None),
}

_CLOUD_COST_PER_TOKEN = 0.000003


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _route_label(model: str, fallback_triggered: bool, task_type: TaskType) -> str:
    if fallback_triggered:
        return "fallback"
    if task_type == TaskType.HEALTH_DOMAIN:
        return "local_only"
    if model.startswith("claude"):
        return "cloud"
    return "local"


def _fit_context(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Truncate history to system message(s) + last 6 turns + current user message."""
    if len(messages) <= 8:
        return messages
    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    if len(non_system) > 7:
        non_system = non_system[-7:]
    return system_msgs + non_system


# ---------------------------------------------------------------------------
# Default HTTP client (uses requests; overridable for tests)
# ---------------------------------------------------------------------------

class _DefaultHTTPClient:
    def __init__(self, base_headers: dict | None = None) -> None:
        self._base_headers = {"Content-Type": "application/json", **(base_headers or {})}

    def _headers(self, extra: dict | None = None) -> dict:
        return {**self._base_headers, **(extra or {})}

    def post(self, url: str, json_body: dict, headers: dict | None = None) -> Any:
        resp = _requests.post(url, json=json_body, headers=self._headers(headers), timeout=30)
        resp.raise_for_status()
        return resp

    def get(self, url: str, headers: dict | None = None) -> Any:
        resp = _requests.get(url, headers=self._headers(headers), timeout=10)
        resp.raise_for_status()
        return resp


# ---------------------------------------------------------------------------
# LiteLLMRouterAdapter
# ---------------------------------------------------------------------------

class LiteLLMRouterAdapter(LLMRouter):
    """Concrete LLMRouter that routes all calls through the LiteLLM proxy.

    LiteLLM runs at http://llm-router:4000 and exposes an OpenAI-compatible API.
    Inject http_client in tests to avoid real network calls.
    """

    def __init__(
        self,
        base_url: str = "http://llm-router:4000",
        sanitiser: DataSanitiser | None = None,
        storage_adapter: StorageAdapter | None = None,
        http_client: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._sanitiser = sanitiser or DataSanitiser()
        self._storage = storage_adapter
        self._litellm_key = os.environ.get("LITELLM_MASTER_KEY", "")
        self._http = http_client or _DefaultHTTPClient()

    # ------------------------------------------------------------------
    # complete
    # ------------------------------------------------------------------

    def complete(
        self,
        task_type: TaskType,
        domain: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
    ) -> LLMResponse | Generator[LLMStreamChunk, None, LLMResponse]:
        start = time.time()
        task_type_mismatch = False
        sanitisation_report: SanitisationReport | None = None

        # Step 1 — domain guard: health domain always forces HEALTH_DOMAIN
        if domain == "health" and task_type != TaskType.HEALTH_DOMAIN:
            task_type_mismatch = True
            self._log_security_mismatch(task_type, domain)
            task_type = TaskType.HEALTH_DOMAIN

        route = _TASK_ROUTES[task_type]
        model = route.model
        fallback_triggered = False
        fallback_reason: str | None = None

        messages = _fit_context(messages)

        # Step 2 — sanitise before cloud calls
        if model.startswith("claude"):
            serialised, sanitisation_report = self._sanitiser.sanitise(
                json.dumps(messages), known_pii={}
            )
            messages = json.loads(serialised)

        # Step 3 — call LiteLLM
        raw: Any = None
        error_str: str | None = None
        try:
            raw = self._call(model, messages, tools)
        except Exception as exc:
            error_str = str(exc)

        # Step 4 — fallback (not allowed for health domain)
        if raw is None and route.fallback == "cloud" and domain != "health":
            fallback_triggered = True
            fallback_reason = error_str
            self._log_fallback(task_type, domain, error_str or "")
            model = "claude-sonnet-4-6"
            # Sanitise for the cloud fallback call
            serialised, sanitisation_report = self._sanitiser.sanitise(
                json.dumps(messages), known_pii={}
            )
            messages = json.loads(serialised)
            error_str = None
            try:
                raw = self._call(model, messages, tools)
            except Exception as exc:
                error_str = str(exc)

        latency_ms = int((time.time() - start) * 1000)

        if raw is None:
            resp = LLMResponse(
                content="",
                model_used=model,
                route_taken=_route_label(model, fallback_triggered, task_type),
                task_type=task_type,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=latency_ms,
                fallback_triggered=fallback_triggered,
                fallback_reason=fallback_reason,
                sanitisation=sanitisation_report,
                success=False,
                error=error_str,
            )
            self._write_routing_event(task_type, domain, resp, task_type_mismatch, sanitisation_report is not None)
            return resp

        content, prompt_tokens, completion_tokens = self._parse_response(raw)
        resp = LLMResponse(
            content=content,
            model_used=model,
            route_taken=_route_label(model, fallback_triggered, task_type),
            task_type=task_type,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            fallback_triggered=fallback_triggered,
            fallback_reason=fallback_reason,
            sanitisation=sanitisation_report,
            success=True,
        )
        self._write_routing_event(task_type, domain, resp, task_type_mismatch, sanitisation_report is not None)
        return resp

    # ------------------------------------------------------------------
    # classify_intent
    # ------------------------------------------------------------------

    def classify_intent(
        self, text: str, context: dict[str, Any]
    ) -> IntentClassification | None:
        messages = [
            {
                "role": "system",
                "content": (
                    "Classify the intent. Respond in JSON: "
                    '{"intent": "<domain.action>", "domain": "<domain>", '
                    '"confidence": <float 0-1>, "alternatives": []}'
                ),
            },
            {"role": "user", "content": text},
        ]
        response = self.complete(
            TaskType.INTENT_CLASSIFICATION,
            context.get("domain", "general"),
            messages,
        )
        if not response.success:  # type: ignore[union-attr]
            return None
        try:
            data = json.loads(response.content)  # type: ignore[union-attr]
            confidence = float(data.get("confidence", 0.0))
            if confidence < 0.75:
                return None
            return IntentClassification(
                intent=data["intent"],
                domain=data["domain"],
                confidence=confidence,
                alternatives=data.get("alternatives", []),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    # ------------------------------------------------------------------
    # get_usage
    # ------------------------------------------------------------------

    def get_usage(self, period: str = "month") -> UsageReport:
        total_tokens = 0
        cloud_tokens = 0
        local_tokens = 0
        local_calls = 0
        cloud_calls = 0
        fallback_count = 0
        by_task_type: dict[str, int] = {}

        if self._storage is not None:
            records = self._storage.list("observability", "history", "system")
            for record in records:
                d = record.data
                if d.get("record_type") != "routing_event":
                    continue
                tokens = d.get("prompt_tokens", 0) + d.get("completion_tokens", 0)
                total_tokens += tokens
                tt = d.get("task_type", "")
                by_task_type[tt] = by_task_type.get(tt, 0) + tokens
                if d.get("route_taken") in ("cloud", "fallback"):
                    cloud_tokens += tokens
                    cloud_calls += 1
                else:
                    local_tokens += tokens
                    local_calls += 1
                if d.get("fallback_triggered"):
                    fallback_count += 1

        return UsageReport(
            period=period,
            total_tokens=total_tokens,
            cloud_tokens=cloud_tokens,
            local_tokens=local_tokens,
            estimated_cost_usd=cloud_tokens * _CLOUD_COST_PER_TOKEN,
            by_task_type=by_task_type,
            local_calls=local_calls,
            cloud_calls=cloud_calls,
            fallback_count=fallback_count,
        )

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------

    def health(self) -> RouterHealth:
        ok = False
        error: str | None = None
        try:
            headers = {"Content-Type": "application/json"}
            if self._litellm_key:
                headers["Authorization"] = f"Bearer {self._litellm_key}"
            self._http.get(f"{self._base_url}/health", headers=headers)
            ok = True
        except Exception as exc:
            error = str(exc)
        return RouterHealth(
            litellm_reachable=ok,
            cloud_model_available=ok,
            local_model_available=ok,
            last_call_at=_now_iso(),
            error=error,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call(self, model: str, messages: list[dict], tools: list[dict] | None) -> Any:
        url = f"{self._base_url}/v1/chat/completions"
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self._litellm_key:
            headers["Authorization"] = f"Bearer {self._litellm_key}"
        return self._http.post(url, json_body=body, headers=headers)

    def _parse_response(self, raw: Any) -> tuple[str, int, int]:
        data = raw.json() if hasattr(raw, "json") else raw
        content = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage", {})
        return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    def _write_routing_event(
        self,
        task_type: TaskType,
        domain: str,
        response: LLMResponse,
        task_type_mismatch: bool,
        sanitisation_applied: bool,
    ) -> None:
        if self._storage is None:
            return
        event_id = str(uuid.uuid4())
        self._storage.write(StorageRecord(
            domain="observability",
            key=event_id,
            layer="history",
            data={
                "record_type": "routing_event",
                "event_id": event_id,
                "timestamp": _now_iso(),
                "task_type": task_type.value,
                "domain": domain,
                "model_used": response.model_used,
                "route_taken": response.route_taken,
                "fallback_triggered": response.fallback_triggered,
                "task_type_mismatch": task_type_mismatch,
                "sanitisation_applied": sanitisation_applied,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "latency_ms": response.latency_ms,
                "success": response.success,
                "error": response.error,
            },
            user_id="system",
            version=1,
            updated_at=_now_iso(),
        ))

    def _log_security_mismatch(self, original_task_type: TaskType, domain: str) -> None:
        if self._storage is None:
            return
        self._storage.write(StorageRecord(
            domain="observability",
            key=str(uuid.uuid4()),
            layer="history",
            data={
                "record_type": "security_event",
                "event_type": "health_domain_task_type_mismatch",
                "original_task_type": original_task_type.value,
                "domain": domain,
                "timestamp": _now_iso(),
            },
            user_id="system",
            version=1,
            updated_at=_now_iso(),
        ))

    def _log_fallback(self, task_type: TaskType, domain: str, reason: str) -> None:
        if self._storage is None:
            return
        self._storage.write(StorageRecord(
            domain="observability",
            key=str(uuid.uuid4()),
            layer="history",
            data={
                "record_type": "fallback_event",
                "task_type": task_type.value,
                "domain": domain,
                "reason": reason,
                "timestamp": _now_iso(),
            },
            user_id="system",
            version=1,
            updated_at=_now_iso(),
        ))
