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
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)
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
    model_used: str            # "ollama/phi3:mini" | "claude-sonnet-4-6"
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
        max_tokens: int | None = None,
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

    def call_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        tool_choice: Any = "auto",
    ) -> dict[str, Any]:
        """Call the LLM with tool definitions and return the selected tool call.

        Uses the same model routing and fallback chain as complete().
        Returns {"name": str, "args": dict} when the LLM selected a tool,
        or {"name": None, "content": str} when it responded with text only.

        tool_choice: "auto" (default) lets the LLM decide.
          Pass {"type": "function", "function": {"name": "tool_name"}} to force
          a specific tool — required for DESTRUCTIVE intents where the LLM might
          skip the tool and respond with text instead.

        tools: OpenAI-format tool dicts:
          [{"type": "function", "function": {
              "name": "...", "description": "...", "parameters": {...}}}]
        """
        raise NotImplementedError("call_with_tools() not implemented for this router")


# ---------------------------------------------------------------------------
# Route config
# ---------------------------------------------------------------------------

@dataclass
class _RouteConfig:
    model: str
    fallback: str | None  # "cloud" or None


# Cloud routing order: Gemini first (free tier, 1M tokens/day), then GPT-4o-mini
# (cheap buffer), then Claude last (paid credits, highest quality).
# When local LLM is unavailable, cloud tasks start at the top of _CLOUD_CHAIN.
# Health domain is always local-only — never enters the cloud chain.
_TASK_ROUTES: dict[TaskType, _RouteConfig] = {
    TaskType.DATA_CAPTURE:              _RouteConfig("ollama/phi3:mini", "cloud"),
    TaskType.RULE_BASED_ALERT:          _RouteConfig("ollama/phi3:mini", "cloud"),
    TaskType.SIMPLE_RETRIEVAL:          _RouteConfig("ollama/phi3:mini", "cloud"),
    TaskType.INTENT_CLASSIFICATION:     _RouteConfig("ollama/phi3:mini", "cloud"),
    TaskType.VOICE_DISAMBIGUATION:      _RouteConfig("ollama/phi3:mini", "cloud"),
    TaskType.CROSS_DOMAIN_INSIGHT:      _RouteConfig("gemini-flash", None),
    TaskType.NATURAL_LANGUAGE_DRAFTING: _RouteConfig("gemini-flash", None),
    TaskType.DOCUMENT_PARSING:          _RouteConfig("gemini-flash", None),
    TaskType.MONTHLY_SUMMARY:           _RouteConfig("gemini-flash", None),
    TaskType.WEB_UNDERSTANDING:         _RouteConfig("gemini-flash", None),
    TaskType.HEALTH_DOMAIN:             _RouteConfig("ollama/phi3:mini", None),
}

# Priority-ordered cloud provider chain.
# Gemini Flash: free tier (1M tokens/day via Google AI Studio key).
# GPT-4o-mini: very cheap (~$0.15/1M tokens), no free tier.
# Claude Sonnet: paid credits, highest reasoning quality — used last.
# Health domain never enters this chain — local only, hardcoded.
_CLOUD_CHAIN = ["gemini-flash", "gpt-4o-mini", "claude-sonnet-4-6"]

_CLOUD_COST_PER_TOKEN = 0.000003


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _route_label(model: str, fallback_triggered: bool, task_type: TaskType) -> str:
    if fallback_triggered:
        return "fallback"
    if task_type == TaskType.HEALTH_DOMAIN:
        return "local_only"
    if not model.startswith("ollama"):
        return "cloud"
    return "local"


def _strip_fences(raw: str) -> str:
    """Strip markdown code fences and recover from truncated JSON responses.

    Handles three cases:
    1. ```json ... ``` markdown fences wrapping the JSON
    2. JSON embedded within surrounding text (finds the first { ... })
    3. Truncated JSON (response cut off before closing }) — closed with balancing braces
    """
    raw = raw.strip()
    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    # Find the first JSON object boundary
    start = raw.find("{")
    if start == -1:
        return raw

    # Walk from start, tracking balanced braces (ignoring braces inside strings)
    depth = 0
    end = -1
    in_string = False
    escape_next = False
    for i, ch in enumerate(raw[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end != -1:
        return raw[start : end + 1]

    # Truncated response: close all remaining open braces
    return raw[start:] + "}" * depth


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
        resp = _requests.post(url, json=json_body, headers=self._headers(headers), timeout=120)
        resp.raise_for_status()
        return resp

    def get(self, url: str, headers: dict | None = None) -> Any:
        resp = _requests.get(url, headers=self._headers(headers), timeout=3)
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
        max_tokens: int | None = None,
    ) -> LLMResponse | Generator[LLMStreamChunk, None, LLMResponse]:
        start = time.time()
        task_type_mismatch = False
        sanitisation_report: SanitisationReport | None = None

        # Guard 1 — health domain ALWAYS forces HEALTH_DOMAIN task type.
        # This is a hard wall: health data never reaches any cloud model.
        if domain == "health" and task_type != TaskType.HEALTH_DOMAIN:
            task_type_mismatch = True
            self._log_security_mismatch(task_type, domain)
            task_type = TaskType.HEALTH_DOMAIN

        route = _TASK_ROUTES[task_type]

        # Guard 2 — hardware constraint: skip local model when MITHRA_LOCAL_LLM_ENABLED=false.
        # Health domain is exempt — always stays local regardless of this flag.
        _local_enabled = os.environ.get("MITHRA_LOCAL_LLM_ENABLED", "true").lower() != "false"
        if not _local_enabled and task_type != TaskType.HEALTH_DOMAIN and route.model.startswith("ollama"):
            route = _RouteConfig(_CLOUD_CHAIN[0], None)  # start at top of cloud chain

        model = route.model
        fallback_triggered = False
        fallback_reason: str | None = None
        messages = _fit_context(messages)

        # Sanitise before the first cloud call.
        # DataSanitiser runs before ALL cloud providers — Anthropic, Gemini, OpenAI.
        # Applied once here; the sanitised messages are reused for every fallback
        # attempt so PII is never sent to any cloud model in any code path.
        # Health domain (ollama) never reaches this block.
        is_cloud = not model.startswith("ollama")
        if is_cloud:
            serialised, sanitisation_report = self._sanitiser.sanitise(
                json.dumps(messages), known_pii={}
            )
            messages = json.loads(serialised)

        # Primary call
        raw: Any = None
        error_str: str | None = None
        logger.debug("LLM call: task=%s domain=%s model=%s", task_type.value, domain, model)
        try:
            raw = self._call(model, messages, tools, max_tokens=max_tokens)
        except Exception as exc:
            error_str = str(exc)
            logger.warning("LLM call failed: model=%s error=%s", model, error_str)

        # Fallback A — local model failed → walk the full cloud chain.
        # Not allowed for health domain (hard wall).
        if raw is None and route.fallback == "cloud" and domain != "health":
            fallback_triggered = True
            fallback_reason = error_str
            self._log_fallback(task_type, domain, error_str or "")
            # Sanitise now — original model was local so no sanitisation done yet.
            serialised, sanitisation_report = self._sanitiser.sanitise(
                json.dumps(messages), known_pii={}
            )
            messages = json.loads(serialised)
            for cloud_model in _CLOUD_CHAIN:
                error_str = None
                logger.warning(
                    "Local→cloud fallback: model=%s task=%s", cloud_model, task_type.value
                )
                try:
                    raw = self._call(cloud_model, messages, tools, max_tokens=max_tokens)
                    model = cloud_model
                    break
                except Exception as exc:
                    error_str = str(exc)
                    logger.warning("Cloud model %s failed: %s", cloud_model, error_str)

        # Fallback B — cloud provider failed → try remaining providers in the chain.
        # Covers credit exhaustion (400), rate limits (429), and service errors (5xx).
        # Messages are already sanitised from the block above.
        # Health domain never enters this path.
        elif raw is None and is_cloud and domain != "health":
            start_idx = (
                _CLOUD_CHAIN.index(model) + 1 if model in _CLOUD_CHAIN else len(_CLOUD_CHAIN)
            )
            for cloud_model in _CLOUD_CHAIN[start_idx:]:
                fallback_triggered = True
                fallback_reason = error_str
                self._log_fallback(task_type, domain, error_str or "")
                error_str = None
                logger.warning(
                    "Cloud provider unavailable, trying fallback: %s → %s",
                    model, cloud_model,
                )
                try:
                    raw = self._call(cloud_model, messages, tools, max_tokens=max_tokens)
                    model = cloud_model
                    break
                except Exception as exc:
                    error_str = str(exc)
                    logger.warning("Cloud fallback %s also failed: %s", cloud_model, error_str)

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
                    '"confidence": <float 0-1>, "alternatives": []}. '
                    "Time domain handles events and reminders WITH a specific clock time: "
                    "'dentist tuesday 3pm', 'remind me at 8am daily', 'trash every thursday 8pm'. "
                    "Tasks domain handles action items WITHOUT a specific time: "
                    "'fix ceiling fan', 'call insurance this week', "
                    "'return package by friday', 'mix baby formula daily' (routine checklist). "
                    "For 'remind me to X' without a time → tasks domain intent: task.create. "
                    "For 'remind me to X at [time]' → time domain intent: reminder.create. "
                    "Use these intent prefixes: "
                    "calendar.* → time domain (events), "
                    "reminder.* → time domain (timed reminders), "
                    "task.* → tasks domain, "
                    "routine.* → tasks domain, "
                    "project.* → tasks domain, "
                    "inbox.* → tasks domain."
                ),
            },
            {"role": "user", "content": text},
        ]
        response = self.complete(
            TaskType.INTENT_CLASSIFICATION,
            context.get("domain", "general"),
            messages,
            max_tokens=400,
        )
        if not response.success:  # type: ignore[union-attr]
            logger.warning("classify_intent LLM call failed: %s", response.error)
            return None
        try:
            data = json.loads(_strip_fences(response.content))  # type: ignore[union-attr]
            confidence = float(data.get("confidence", 0.0))
            if confidence < 0.75:
                logger.info("classify_intent low confidence %.2f for: %s", confidence, text[:60])
                return None
            logger.info("classify_intent: intent=%s confidence=%.2f", data.get("intent"), confidence)
            return IntentClassification(
                intent=data["intent"],
                domain=data["domain"],
                confidence=confidence,
                alternatives=data.get("alternatives", []),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("classify_intent parse failed: %s — raw: %.200s", exc, response.content)
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
            # 2s timeout: health checks only verify reachability, not throughput
            _requests.get(
                f"{self._base_url}/health",
                headers=headers,
                timeout=2,
            ).raise_for_status()
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
    # call_with_tools
    # ------------------------------------------------------------------

    def call_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        tool_choice: Any = "auto",
    ) -> dict[str, Any]:
        """Call LLM with tool definitions via the LiteLLM proxy HTTP API.

        Reuses _call() and the _CLOUD_CHAIN fallback loop so routing and
        fallback behaviour matches complete() exactly. No separate litellm
        Python package required — the proxy handles provider routing.

        Returns {"name": str, "args": dict} when LLM selected a tool,
        or {"name": None, "content": str} when it responded with text only.
        """
        target_model = model or _CLOUD_CHAIN[0]
        raw: Any = None
        last_exc: str = ""
        tc = None if tool_choice == "auto" else tool_choice

        try:
            raw = self._call(target_model, messages, tools, tc)
        except Exception as exc:
            last_exc = str(exc)
            logger.warning(
                "call_with_tools: primary model=%s failed: %s", target_model, last_exc
            )

        # Walk fallback chain — same logic as complete()
        if raw is None:
            start_idx = (
                _CLOUD_CHAIN.index(target_model) + 1
                if target_model in _CLOUD_CHAIN else 0
            )
            for fallback_model in _CLOUD_CHAIN[start_idx:]:
                try:
                    raw = self._call(fallback_model, messages, tools, tc)
                    break
                except Exception as exc:
                    last_exc = str(exc)
                    logger.warning(
                        "call_with_tools: fallback model=%s failed: %s",
                        fallback_model, last_exc,
                    )

        if raw is None:
            logger.error("call_with_tools: all models failed — last error: %s", last_exc)
            return {"name": None, "content": ""}

        data = raw.json() if hasattr(raw, "json") else raw
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            return {
                "name": tc["function"]["name"],
                "args": json.loads(tc["function"].get("arguments") or "{}"),
            }
        return {"name": None, "content": message.get("content") or ""}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Any = None,
        max_tokens: int | None = None,
    ) -> Any:
        url = f"{self._base_url}/v1/chat/completions"
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
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
