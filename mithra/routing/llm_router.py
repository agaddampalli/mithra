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

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generator

from mithra.storage.sanitiser import SanitisationReport


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
