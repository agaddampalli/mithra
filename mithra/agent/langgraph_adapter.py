"""
LangGraph adapter — implements AgentInterface using LangGraph as the underlying
orchestration engine.

LangGraph imports are confined to this module. No other file in the codebase
may import from langgraph directly.

Graph: classify_intent → load_domain_tools → llm_tool_selection → execute_tool → generate_response

Conditional routing after classify_intent:
  CLARIFICATION_NEEDED or UNKNOWN, or confidence < 0.75 → generate_response
  otherwise → load_domain_tools

Conditional routing after llm_tool_selection:
  tool_calls present → execute_tool
  no tool_calls but MCPTool matches intent → execute_tool
  otherwise → generate_response
"""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid

_logger = logging.getLogger(__name__)
from datetime import datetime, timedelta, timezone
from typing import Any, Generator, TypedDict

import pytz

from langgraph.graph import END, StateGraph

from mithra.agent.interface import (
    AgentAction,
    AgentError,
    AgentErrorCode,
    AgentInterface,
    AgentIntent,
    AgentRequest,
    AgentResult,
    AgentStreamEvent,
    ClarificationRequest,
    InputChannel,
    MCPTool,
    StreamEventType,
)
from mithra.routing.llm_router import LLMRouter, TaskType
from mithra.storage.router import StorageRouter


# ---------------------------------------------------------------------------
# Intent → domain_id mapping
# ---------------------------------------------------------------------------

INTENT_DOMAIN_MAP: dict[str, str] = {
    # Time domain — requires a specific clock time to exist
    "calendar":         "time",
    "time":             "time",
    "scheduling":       "time",
    "schedule":         "time",
    "reminder":         "time",   # default; tasks domain handles if no time
    "reminders":        "time",
    "event":            "time",
    "cancel":           "time",

    # Tasks domain — action items without a specific clock time
    "task":             "tasks",
    "tasks":            "tasks",
    "todo":             "tasks",
    "routine":          "tasks",
    "routines":         "tasks",
    "project":          "tasks",
    "projects":         "tasks",
    "checklist":        "tasks",
    "habit":            "tasks",

    # Other domains
    "people":           "people",
    "contacts":         "people",
    "money":            "money",
    "finance":          "money",
    "documents":        "documents",
    "files":            "documents",
    "home":             "home",
    "health":           "health",
    "food":             "food",
    "shopping":         "shopping",
    "travel":           "travel",

    # No domain tools
    "general":          "",
    "system":           "",
    "unknown":          "",
    "integration":      "",
    "auth":             "",
    "conversation":     "",
    "dialog":           "",
}


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class _GraphState(TypedDict):
    request: AgentRequest
    intent: str
    confidence: float
    domain_bundles: list[Any]
    tool_calls: list[Any]            # tool_calls from LangChain bind_tools response
    llm_response: Any | None         # raw LangChain AIMessage from llm_tool_selection
    tool_result: dict[str, Any] | None
    pending_actions: list
    response_text: str
    error: str | None
    stream_events: list[AgentStreamEvent]


class LangGraphAdapter(AgentInterface):
    """LangGraph implementation of AgentInterface."""

    def __init__(
        self,
        llm_router: LLMRouter,
        storage_router: StorageRouter | None = None,
        context_cache: Any | None = None,
        household_timezone: str = "UTC",
        domain_registry: Any | None = None,
        household_id: str = "",
        oauth_manager: Any | None = None,
        file_router: Any | None = None,
        member_registry: Any | None = None,
        file_search: Any | None = None,
    ) -> None:
        self._llm_router = llm_router
        self._storage_router = storage_router
        self._context_cache = context_cache
        self._household_tz = household_timezone
        self._domain_registry = domain_registry
        self._household_id = household_id
        self._oauth_manager = oauth_manager
        self._file_router = file_router
        self._member_registry = member_registry
        self._file_search = file_search
        self._local_db_path = ""
        self._tools: dict[str, MCPTool] = {}
        self._graph = self._build_graph()

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self) -> Any:
        graph = StateGraph(_GraphState)

        graph.add_node("classify_intent", self._node_classify_intent)
        graph.add_node("load_domain_tools", self._node_load_domain_tools)
        graph.add_node("llm_tool_selection", self._node_llm_tool_selection)
        graph.add_node("execute_tool", self._node_execute_tool)
        graph.add_node("generate_response", self._node_generate_response)

        graph.set_entry_point("classify_intent")
        graph.add_conditional_edges(
            "classify_intent",
            self._route_after_classify,
            {
                "load_domain_tools": "load_domain_tools",
                "generate_response": "generate_response",
            },
        )
        graph.add_edge("load_domain_tools", "llm_tool_selection")
        graph.add_conditional_edges(
            "llm_tool_selection",
            self._route_after_tool_selection,
            {
                "execute_tool": "execute_tool",
                "generate_response": "generate_response",
            },
        )
        graph.add_edge("execute_tool", "generate_response")
        graph.add_edge("generate_response", END)

        return graph.compile()

    def _route_after_classify(self, state: _GraphState) -> str:
        intent = state.get("intent", "")
        confidence = state.get("confidence", 0.0)
        if intent in (
            AgentIntent.SYSTEM_CLARIFICATION_NEEDED,
            AgentIntent.SYSTEM_UNKNOWN,
        ) or confidence < 0.75:
            return "generate_response"
        return "load_domain_tools"

    def _route_after_tool_selection(self, state: _GraphState) -> str:
        if state.get("tool_calls"):
            return "execute_tool"
        # MCPTool fallback: intent maps directly to a registered legacy tool
        intent = state.get("intent", "")
        if str(intent) in self._tools:
            return "execute_tool"
        return "generate_response"

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def _node_classify_intent(self, state: _GraphState) -> dict[str, Any]:
        request: AgentRequest = state["request"]
        text = request.text or ""
        context: dict[str, Any] = {
            "domain_hint": request.domain_hint,
            "user_id": request.user_id,
        }

        classification = self._llm_router.classify_intent(text, context)
        if classification is None:
            return {
                "intent": AgentIntent.SYSTEM_CLARIFICATION_NEEDED,
                "confidence": 0.0,
            }
        return {
            "intent": classification.intent,
            "confidence": classification.confidence,
        }

    def _node_load_domain_tools(self, state: _GraphState) -> dict[str, Any]:
        """Stage 2a — load this intent's domain tools from the registry."""
        intent = state.get("intent", "")
        domain_ids = self._extract_domain_ids(str(intent))
        request: AgentRequest = state["request"]
        domain_bundles: list[Any] = []
        if self._domain_registry and domain_ids:
            try:
                ctx = self._build_domain_context(request)
                domain_bundles = self._domain_registry.get_tools(domain_ids, ctx)
            except Exception as exc:
                _logger.error(
                    "_node_load_domain_tools failed: %s\n%s", exc, traceback.format_exc()
                )
        _logger.info(
            "_node_load_domain_tools: intent=%r domain_ids=%s bundles=%d",
            intent, domain_ids, len(domain_bundles),
        )
        return {"domain_bundles": domain_bundles}

    def _node_llm_tool_selection(self, state: _GraphState) -> dict[str, Any]:
        """Stage 2b — LLM selects the specific tool via call_with_tools (ADR 9.4).

        Converts ToolBundles to OpenAI tool schema, calls call_with_tools()
        which routes through the LiteLLM proxy with the full fallback chain.
        """
        domain_bundles: list[Any] = state.get("domain_bundles", [])
        request: AgentRequest = state["request"]

        if not domain_bundles:
            return {"tool_calls": [], "llm_response": None}

        # Convert ToolBundles to OpenAI-format tool schema
        tools_schema: list[dict[str, Any]] = []
        for bundle in domain_bundles:
            lc_tool = bundle.tool
            schema: dict[str, Any] = (
                lc_tool.args_schema.model_json_schema()
                if lc_tool.args_schema else {}
            )
            schema.pop("title", None)
            tools_schema.append({
                "type": "function",
                "function": {
                    "name": lc_tool.name,
                    "description": lc_tool.description,
                    "parameters": schema,
                },
            })

        tz = pytz.timezone(self._household_tz)
        now = datetime.now(tz)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are Mithra, a household AI assistant. "
                    f"Today is {now.strftime('%A, %B %-d, %Y')}. "
                    f"Current time: {now.strftime('%-I:%M %p')} {self._household_tz}. "
                    "Use the available tools to help the user. "
                    "Always use a tool when one is relevant."
                ),
            },
            {"role": "user", "content": request.text or ""},
        ]

        # Force the delete tool when user clearly intends to cancel/delete a calendar event.
        # Without this, the LLM may skip the tool and respond with text instead.
        _CANCEL_KEYWORDS = {"cancel", "delete", "remove", "drop", "scratch", "abort"}
        user_text_lower = (request.text or "").lower()
        force_tool: str | None = None
        has_delete_tool = any(b.tool.name == "calendar_delete_event" for b in domain_bundles)
        if has_delete_tool and any(k in user_text_lower for k in _CANCEL_KEYWORDS):
            force_tool = "calendar_delete_event"
            _logger.info(
                "_node_llm_tool_selection: forcing tool=%r for cancel/delete intent", force_tool
            )

        tool_choice: Any
        if force_tool:
            tool_choice = {"type": "function", "function": {"name": force_tool}}
        else:
            tool_choice = "auto"

        try:
            result = self._llm_router.call_with_tools(
                messages=messages,
                tools=tools_schema,
                tool_choice=tool_choice,
            )
            if result.get("name"):
                _logger.info(
                    "_node_llm_tool_selection: selected tool=%r", result["name"]
                )
                return {
                    "tool_calls": [{"name": result["name"], "args": result.get("args", {})}],
                    "llm_response": None,
                }
            _logger.info("_node_llm_tool_selection: LLM chose text response (no tool)")
            return {"tool_calls": [], "llm_response": result.get("content", "")}
        except Exception as exc:
            _logger.error(
                "_node_llm_tool_selection failed: %s\n%s", exc, traceback.format_exc()
            )
            return {"tool_calls": [], "llm_response": None}

    def _node_execute_tool(self, state: _GraphState) -> dict[str, Any]:
        tool_calls: list[Any] = state.get("tool_calls", [])
        domain_bundles: list[Any] = state.get("domain_bundles", [])
        request: AgentRequest = state["request"]

        # --- Path A: LLM-selected domain bundle tool (bind_tools) ---
        if tool_calls:
            first_call = tool_calls[0]
            tool_name = (
                first_call["name"] if isinstance(first_call, dict)
                else getattr(first_call, "name", "")
            )
            tool_args: dict[str, Any] = (
                first_call.get("args", {}) if isinstance(first_call, dict)
                else getattr(first_call, "args", {})
            ) or {}

            _logger.info(
                "_node_execute_tool (bind_tools): tool_name=%r args=%s",
                tool_name, tool_args,
            )

            bundle = next(
                (b for b in domain_bundles if b.metadata.name == tool_name), None
            )
            if bundle is not None:
                try:
                    ctx = self._build_domain_context(request)
                    result = self._execute_with_autonomy(bundle, tool_args, ctx)
                    if result.get("requires_confirmation"):
                        pending_payload: dict[str, Any] = {
                            **tool_args,
                            "user_id": request.user_id,
                        }
                        # Pre-format start_time so the confirmation message can
                        # show "3:00 PM CST" without importing domain code in the engine.
                        start_raw = tool_args.get("start_time", "")
                        if start_raw and self._household_tz:
                            display = self._format_start_for_confirmation(
                                start_raw, self._household_tz
                            )
                            if display:
                                pending_payload["start_time_display"] = display
                        return {
                            "tool_result": None,
                            "pending_actions": [{
                                "action_type": bundle.metadata.name,
                                "domain": bundle.metadata.domain_id,
                                "payload": pending_payload,
                                "output_channel": "telegram",
                                "autonomy_level": bundle.metadata.autonomy_level,
                                "reversibility": "full" if bundle.metadata.is_reversible else "none",
                            }],
                        }
                    return {"tool_result": result}
                except Exception as exc:  # noqa: BLE001
                    _logger.error(
                        "_node_execute_tool: bundle %r failed: %s\n%s",
                        tool_name, exc, traceback.format_exc(),
                    )
                    return {"tool_result": None, "error": str(exc)}

        # --- Path B: MCPTool fallback (legacy, no domain registry) ---
        # Domain bundles were loaded but LLM chose not to call a tool (text response).
        # Never fall through to legacy MCP lookup when domain tools were available.
        if domain_bundles:
            return {"tool_result": None, "error": None}

        intent = state.get("intent", "")
        mcp_tool_name = str(intent)
        if mcp_tool_name not in self._tools:
            return {"tool_result": None, "error": None}

        tool = self._tools[mcp_tool_name]
        _logger.info("_node_execute_tool (MCP fallback): tool=%r", mcp_tool_name)
        try:
            kwargs: dict[str, Any] = {}
            if tool.requires_user_id:
                kwargs["user_id"] = request.user_id
            result = tool.handler(**kwargs)
            if isinstance(result, dict):
                return {"tool_result": result}
            return {"tool_result": {"result": result}}
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "_node_execute_tool: MCP tool %r failed: %s\n%s",
                mcp_tool_name, exc, traceback.format_exc(),
            )
            return {"tool_result": None, "error": str(exc)}

    def _node_generate_response(self, state: _GraphState) -> dict[str, Any]:
        request: AgentRequest = state["request"]
        tool_result = state.get("tool_result")
        error = state.get("error")
        intent_str = str(state.get("intent", "")).lower()
        domain_bundles: list[Any] = state.get("domain_bundles", [])
        llm_response: str = state.get("llm_response") or ""

        # Pending confirmation — build human-readable message without calling LLM.
        # The AutononomyEngine sends Yes/No buttons separately via the receipt;
        # this response text describes WHAT is being confirmed, not WHETHER to confirm.
        pending_actions = state.get("pending_actions", [])
        if pending_actions:
            action = pending_actions[0]
            summary = action.get("summary", "")
            if not summary:
                tool_name = action.get("action_type", "action")
                payload = action.get("payload", {})
                if "title" in payload and "start_time" in payload:
                    summary = f"Create '{payload['title']}' at {payload['start_time']}"
                elif "event_query" in payload:
                    summary = f"Delete '{payload['event_query']}' from your calendar"
                elif "title" in payload:
                    summary = f"Confirm: {payload['title']}"
                else:
                    summary = f"Confirm: {tool_name.replace('_', ' ')}"
            return {"response_text": summary}

        # Tool executed successfully — prefer summary (domain tools) over response (MCP tools)
        if tool_result:
            return {"response_text": tool_result.get("summary") or tool_result.get("response", "Done.")}

        # Real tool execution error (not "no tool found")
        if error:
            return {"response_text": f"I encountered an error: {error}"}

        # Domain tools were loaded — LLM chose a text response (no tool selected).
        # The old calendar_write adapter fallback must NOT run here: it bypasses the
        # domain tool path and creates actions through a different adapter, producing
        # the wrong behavior (e.g. "cancel" triggers a create instead of delete).
        if domain_bundles:
            if llm_response:
                return {"response_text": llm_response}
            return {"response_text": "I'm not sure how to help with that. Try rephrasing."}

        # Detect calendar/scheduling intents — build a structured pending action
        _CALENDAR_PREFIXES = ("time.", "calendar.")
        _CALENDAR_KEYWORDS = (
            "add_event", "create_event", "schedule_event",
            "schedule", "appointment", "add_appointment",
        )
        is_calendar_intent = any(intent_str.startswith(p) for p in _CALENDAR_PREFIXES) or any(
            k in intent_str for k in _CALENDAR_KEYWORDS
        )

        if is_calendar_intent:
            user_tz_str = self._household_tz
            tz = pytz.timezone(user_tz_str)
            now_local = datetime.now(tz)
            today = now_local.strftime("%Y-%m-%d")
            tomorrow = (now_local + timedelta(days=1)).strftime("%Y-%m-%d")
            extract_messages = [
                {
                    "role": "system",
                    "content": (
                        f"Timezone: {user_tz_str}. "
                        f"All times are in {user_tz_str}. "
                        f"Today is {today}. Tomorrow is {tomorrow}. "
                        f"Current local time is {now_local.strftime('%H:%M')}. "
                        "Extract calendar event details from the user's message. "
                        "Return ONLY a JSON object with these fields: "
                        "summary (string, required), "
                        "start_datetime (ISO 8601 string with timezone offset, required, "
                        f"e.g. {today}T15:00:00{now_local.strftime('%z')[:3]}:{now_local.strftime('%z')[3:]}), "
                        "end_datetime (ISO 8601 string with timezone offset, required), "
                        "description (string, optional), "
                        "location (string, optional). "
                        "If duration is unspecified, default to 1 hour. "
                        "If no date is given, use today. "
                        "Do not include any explanation or markdown fences."
                    ),
                },
                {"role": "user", "content": request.text or ""},
            ]
            event_data: dict[str, Any] = {}
            try:
                extract_resp = self._llm_router.complete(
                    TaskType.DATA_CAPTURE, "time", extract_messages
                )
                if extract_resp.success and extract_resp.content:
                    raw = extract_resp.content.strip()
                    if raw.startswith("```"):
                        raw = raw.split("\n", 1)[-1]
                    if raw.endswith("```"):
                        raw = raw.rsplit("```", 1)[0]
                    event_data = json.loads(raw.strip())
            except Exception as _exc:  # noqa: BLE001
                _logger.error(
                    "_node_generate_response: calendar event extraction failed: %s\n%s",
                    _exc, traceback.format_exc(),
                )
                event_data = {}

            if not event_data.get("summary"):
                event_data["summary"] = request.text or "New event"
            event_data["user_id"] = request.user_id

            summary = event_data.get("summary", "New event")
            start = event_data.get("start_datetime", "")
            if start:
                action_desc = f"add '{summary}' to your calendar starting {start}"
                response_text = f"I'll {action_desc}. Confirm?"
            else:
                action_desc = f"add '{summary}' to your calendar"
                response_text = f"I'll {action_desc}. Confirm?"

            return {
                "response_text": response_text,
                "pending_actions": [
                    {
                        "action_type": "calendar_write",
                        "domain": "time",
                        "payload": event_data,
                        "output_channel": "calendar",
                        "autonomy_level": 1,
                        "reversibility": "full",
                        "description": action_desc,
                    }
                ],
            }

        # No tool matched or intent unclear — always try LLM first
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Mithra, a voice-first household AI assistant. "
                    "You help manage daily life across domains like food, home, money, health, "
                    "people, and time. You are connected to calendars, smart home devices, "
                    "and messaging. Be concise and helpful."
                ),
            },
            {"role": "user", "content": request.text or ""},
        ]
        try:
            llm_resp = self._llm_router.complete(
                TaskType.NATURAL_LANGUAGE_DRAFTING,
                request.domain_hint or "general",
                messages,
            )
            if llm_resp.success and llm_resp.content:
                return {"response_text": llm_resp.content}
        except Exception as _exc:  # noqa: BLE001
            _logger.error(
                "_node_generate_response: LLM fallback failed: %s\n%s",
                _exc, traceback.format_exc(),
            )

        # LLM unavailable — fall back to clarification prompt
        return {"response_text": "Could you clarify what you'd like me to do?"}

    # ------------------------------------------------------------------
    # AgentInterface public methods
    # ------------------------------------------------------------------

    def run(self, request: AgentRequest) -> AgentResult:
        """Process a request synchronously via the compiled LangGraph."""
        request_id = request.request_id or str(uuid.uuid4())
        start = time.time()

        initial_state: _GraphState = {
            "request": request,
            "intent": AgentIntent.SYSTEM_UNKNOWN,
            "confidence": 0.0,
            "domain_bundles": [],
            "tool_calls": [],
            "llm_response": None,
            "tool_result": None,
            "pending_actions": [],
            "response_text": "",
            "error": None,
            "stream_events": [],
        }

        try:
            final_state = self._graph.invoke(initial_state)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "LangGraph agent error in run(): %s\n%s",
                exc, traceback.format_exc(),
            )
            return AgentResult(
                request_id=request_id,
                success=False,
                intent=AgentIntent.SYSTEM_UNKNOWN,
                domain="system",
                confidence=0.0,
                actions=[],
                response_text="An internal error occurred.",
                error=AgentError(code=AgentErrorCode.TOOL_FAILED, message=str(exc)),
                latency_ms=int((time.time() - start) * 1000),
            )

        return self._state_to_result(final_state, request_id, start)

    def run_stream(
        self, request: AgentRequest
    ) -> Generator[AgentStreamEvent, None, AgentResult]:
        """Process a request and yield stream events using LangGraph streaming."""
        request_id = request.request_id or str(uuid.uuid4())
        start = time.time()

        initial_state: _GraphState = {
            "request": request,
            "intent": AgentIntent.SYSTEM_UNKNOWN,
            "confidence": 0.0,
            "domain_bundles": [],
            "tool_calls": [],
            "llm_response": None,
            "tool_result": None,
            "pending_actions": [],
            "response_text": "",
            "error": None,
            "stream_events": [],
        }

        final_state: dict[str, Any] = dict(initial_state)
        try:
            for event in self._graph.stream(initial_state):
                for _node_name, node_state in event.items():
                    final_state = {**final_state, **node_state}
                    if node_state.get("response_text"):
                        yield AgentStreamEvent(
                            event_type=StreamEventType.TEXT_DELTA,
                            data={"delta": node_state["response_text"]},
                        )
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "LangGraph agent error in run_stream(): %s\n%s",
                exc, traceback.format_exc(),
            )
            yield AgentStreamEvent(
                event_type=StreamEventType.DONE,
                data={"error": str(exc)},
            )
            return AgentResult(
                request_id=request_id,
                success=False,
                intent=AgentIntent.SYSTEM_UNKNOWN,
                domain="system",
                confidence=0.0,
                actions=[],
                response_text="An internal error occurred.",
                error=AgentError(code=AgentErrorCode.TOOL_FAILED, message=str(exc)),
                latency_ms=int((time.time() - start) * 1000),
            )

        yield AgentStreamEvent(event_type=StreamEventType.DONE, data={})
        return self._state_to_result(final_state, request_id, start)

    def run_briefing(self, user_id: str) -> AgentResult:
        """Generate a proactive daily briefing for the user."""
        request = AgentRequest(
            user_id=user_id,
            device_id="scheduler",
            input_channel=InputChannel.SCHEDULER,
            text="Generate my daily briefing",
            request_id=str(uuid.uuid4()),
            session_id=f"briefing:{user_id}",
            signal={"briefing": True},
        )
        return self.run(request)

    def confirm(self, request_id: str, action_index: int, user_id: str) -> AgentResult:
        """Confirm a pending action — gateway delegates to AutononomyEngine."""
        return AgentResult(
            request_id=request_id,
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="system",
            confidence=1.0,
            actions=[],
            response_text="Confirmed.",
        )

    def reject(self, request_id: str, action_index: int, user_id: str) -> AgentResult:
        """Reject a pending action — gateway delegates to AutononomyEngine."""
        return AgentResult(
            request_id=request_id,
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="system",
            confidence=1.0,
            actions=[],
            response_text="Cancelled.",
        )

    def register_tool(self, tool: MCPTool) -> None:
        """Register an MCP domain tool. Rebuilds the compiled graph."""
        self._tools[tool.name] = tool
        self._graph = self._build_graph()

    def health(self) -> dict[str, Any]:
        """Return agent health status for the health dashboard."""
        return {
            "status": "ok",
            "registered_tools": list(self._tools.keys()),
            "tool_count": len(self._tools),
            "graph_compiled": self._graph is not None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_domain_context(self, request: AgentRequest) -> Any:
        """Build an immutable DomainContext from the current request."""
        _logger.info("_build_domain_context: user=%s", request.user_id)
        from mithra.domains.context import DomainContext

        channel_map = {
            "voice": "voice",
            "telegram": "telegram",
            "whatsapp": "telegram",
            "api": "dashboard",
            "scheduler": "dashboard",
            "upload": "telegram",
            "email": "telegram",
        }
        channel = channel_map.get(str(request.input_channel), "telegram")

        storage_adapter = None
        if self._storage_router:
            try:
                storage_adapter = self._storage_router.get_adapter("general")
            except Exception as _exc:  # noqa: BLE001
                _logger.error(
                    "_build_domain_context: storage adapter lookup failed: %s\n%s",
                    _exc, traceback.format_exc(),
                )

        return DomainContext(
            user_id=request.user_id,
            household_id=self._household_id,
            household_timezone=self._household_tz,
            oauth_manager=self._oauth_manager,
            storage_adapter=storage_adapter,
            file_router=self._file_router,
            member_registry=self._member_registry,
            file_search=self._file_search,
            request_id=request.request_id or "",
            channel=channel,
            language="en-US",
            autonomy_override=None,
            local_db_path=self._local_db_path,
        )

    def _extract_domain_ids(self, intent: str) -> list[str]:
        """Extract registered domain_ids from a classified intent string.

        Intent format: "domain.action" (e.g. "calendar.create_event" → ["time"])
        Multi-domain: "time,money" → ["time", "money"]
        Uses INTENT_DOMAIN_MAP so LLM-facing prefixes map to registered domain_ids.
        """
        from mithra.agent.interface import AgentIntent

        if not intent or intent in (
            str(AgentIntent.SYSTEM_UNKNOWN),
            str(AgentIntent.SYSTEM_CLARIFICATION_NEEDED),
        ):
            return []

        intent_str = str(intent)

        if "," in intent_str:
            domains: list[str] = []
            for part in intent_str.split(","):
                prefix = part.strip().split(".")[0].lower()
                domain_id = INTENT_DOMAIN_MAP.get(prefix, prefix)
                if domain_id and domain_id not in domains:
                    domains.append(domain_id)
            return domains

        prefix = intent_str.split(".")[0].lower()
        domain_id = INTENT_DOMAIN_MAP.get(prefix, prefix)
        if domain_id:
            return [domain_id]
        return []

    def _execute_with_autonomy(
        self,
        bundle: Any,
        tool_input: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        """Execute a ToolBundle with PermissionTier enforcement.

        DESTRUCTIVE is a ONE-WAY DOOR: always requires confirmation, no override.
        WRITE requires confirmation at autonomy_level=1 (SURFACE).
        READ executes immediately — no confirmation needed.
        """
        from mithra.domains.bundle import PermissionTier

        if bundle.metadata.permission in (PermissionTier.DESTRUCTIVE, PermissionTier.WRITE):
            return {
                "requires_confirmation": True,
                "tool_name": bundle.metadata.name,
                "description": f"Requires confirmation: {bundle.metadata.name}",
            }

        try:
            result = bundle.tool.invoke(tool_input)
            from mithra.domains.result import ToolResult
            if isinstance(result, ToolResult):
                return {
                    "success": result.success,
                    "summary": result.summary,
                    "data": result.data,
                    "error": result.error,
                    "events": [
                        {
                            "event_type": e.event_type,
                            "source_domain": e.source_domain,
                            "payload": e.payload,
                        }
                        for e in result.events
                    ],
                    "undo_token": result.undo_token,
                    "response": result.summary,
                }
            if isinstance(result, dict):
                return result
            return {"result": str(result), "success": True, "response": str(result)}
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "_execute_with_autonomy: tool %r failed: %s\n%s",
                getattr(getattr(bundle, "metadata", None), "name", "?"),
                exc, traceback.format_exc(),
            )
            return {"success": False, "error": str(exc)}

    def _format_start_for_confirmation(self, start_raw: str, household_tz: str) -> str:
        """Parse a natural-language start_time and return a display string with timezone.

        Imported locally to avoid coupling the agent layer to a specific domain module.
        Returns empty string on any failure so callers can fall back gracefully.
        """
        try:
            from mithra.domains.time.tools import parse_time
            import pytz
            dt = parse_time(start_raw, household_tz)
            if dt is None:
                return ""
            tz = pytz.timezone(household_tz)
            local = dt.astimezone(tz)
            return local.strftime("%A %B %-d, %-I:%M %p %Z")
        except Exception:
            return ""

    def _state_to_result(
        self, state: dict[str, Any], request_id: str, start: float
    ) -> AgentResult:
        intent_val = state.get("intent", AgentIntent.SYSTEM_UNKNOWN)
        if isinstance(intent_val, str):
            try:
                intent_val = AgentIntent(intent_val)
            except ValueError:
                intent_val = AgentIntent.SYSTEM_UNKNOWN

        confidence = state.get("confidence", 0.0)
        response_text = state.get("response_text", "")
        error_str = state.get("error")

        domain = "system"
        if "." in str(intent_val):
            domain = str(intent_val).split(".")[0]

        clarification: ClarificationRequest | None = None
        if intent_val == AgentIntent.SYSTEM_CLARIFICATION_NEEDED or confidence < 0.75:
            clarification = ClarificationRequest(
                question=response_text or "Could you clarify?",
                suggestions=[],
            )

        agent_error: AgentError | None = None
        if error_str:
            agent_error = AgentError(
                code=AgentErrorCode.TOOL_FAILED,
                message=error_str,
            )

        actions: list[AgentAction] = []
        tool_result = state.get("tool_result") or {}
        # Surface tool-level TOKEN_EXPIRED into AgentResult.error
        if not agent_error and tool_result.get("error_code") == "TOKEN_EXPIRED":
            agent_error = AgentError(
                code=AgentErrorCode.TOKEN_EXPIRED,
                message=tool_result.get("summary", "Google token expired"),
            )
        if "action_type" in tool_result:
            actions.append(
                AgentAction(
                    action_type=tool_result["action_type"],
                    domain=domain,
                    payload=tool_result.get("payload", {}),
                    output_channel=tool_result.get("output_channel", "none"),
                    autonomy_level=tool_result.get("autonomy_level", 1),
                )
            )

        for pa in state.get("pending_actions", []):
            actions.append(
                AgentAction(
                    action_type=pa["action_type"],
                    domain=pa.get("domain", domain),
                    payload=pa.get("payload", {}),
                    output_channel=pa.get("output_channel", "none"),
                    autonomy_level=pa.get("autonomy_level", 1),
                    reversibility=pa.get("reversibility", "full"),
                    description=pa.get("description", ""),
                )
            )

        return AgentResult(
            request_id=request_id,
            success=not bool(error_str),
            intent=intent_val,
            domain=domain,
            confidence=confidence,
            actions=actions,
            response_text=response_text,
            clarification=clarification,
            error=agent_error,
            latency_ms=int((time.time() - start) * 1000),
        )
