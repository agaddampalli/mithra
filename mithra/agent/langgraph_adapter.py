"""
LangGraph adapter — implements AgentInterface using LangGraph as the underlying
orchestration engine.

LangGraph imports are confined to this module. No other file in the codebase
may import from langgraph directly.

StateGraph nodes: classify_intent → select_tool → execute_tool → generate_response

Conditional routing after classify_intent:
  CLARIFICATION_NEEDED or UNKNOWN, or confidence < 0.75 → generate_response
  otherwise → select_tool → execute_tool → generate_response
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Generator, TypedDict

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
# Graph state
# ---------------------------------------------------------------------------

class _GraphState(TypedDict):
    request: AgentRequest
    intent: str
    confidence: float
    selected_tool: str | None
    tool_result: dict[str, Any] | None
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
    ) -> None:
        self._llm_router = llm_router
        self._storage_router = storage_router
        self._context_cache = context_cache
        self._tools: dict[str, MCPTool] = {}
        self._graph = self._build_graph()

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _build_graph(self) -> Any:
        graph = StateGraph(_GraphState)

        graph.add_node("classify_intent", self._node_classify_intent)
        graph.add_node("select_tool", self._node_select_tool)
        graph.add_node("execute_tool", self._node_execute_tool)
        graph.add_node("generate_response", self._node_generate_response)

        graph.set_entry_point("classify_intent")
        graph.add_conditional_edges(
            "classify_intent",
            self._route_after_classify,
            {
                "select_tool": "select_tool",
                "generate_response": "generate_response",
            },
        )
        graph.add_edge("select_tool", "execute_tool")
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
        return "select_tool"

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

    def _node_select_tool(self, state: _GraphState) -> dict[str, Any]:
        intent = state.get("intent", "")
        if intent in self._tools:
            return {"selected_tool": intent}
        # Best-effort: find a tool in the same domain
        domain_prefix = intent.split(".")[0] if "." in intent else ""
        for name in self._tools:
            if name.startswith(domain_prefix + "."):
                return {"selected_tool": name}
        return {"selected_tool": None}

    def _node_execute_tool(self, state: _GraphState) -> dict[str, Any]:
        tool_name = state.get("selected_tool")
        if tool_name is None or tool_name not in self._tools:
            return {
                "tool_result": None,
                "error": f"Tool not found: {tool_name}",
            }
        tool = self._tools[tool_name]
        request: AgentRequest = state["request"]
        try:
            kwargs: dict[str, Any] = {}
            if tool.requires_user_id:
                kwargs["user_id"] = request.user_id
            result = tool.handler(**kwargs)
            if isinstance(result, dict):
                return {"tool_result": result}
            return {"tool_result": {"result": result}}
        except Exception as exc:  # noqa: BLE001
            return {"tool_result": None, "error": str(exc)}

    def _node_generate_response(self, state: _GraphState) -> dict[str, Any]:
        request: AgentRequest = state["request"]
        intent = state.get("intent", AgentIntent.SYSTEM_UNKNOWN)
        confidence = state.get("confidence", 0.0)
        tool_result = state.get("tool_result")
        error = state.get("error")

        if intent == AgentIntent.SYSTEM_CLARIFICATION_NEEDED or confidence < 0.75:
            return {"response_text": "Could you clarify what you'd like me to do?"}
        if error:
            return {"response_text": f"I encountered an error: {error}"}
        if tool_result:
            return {"response_text": tool_result.get("response", "Done.")}

        # No tool matched — ask LLM to draft a response
        messages = [
            {"role": "system", "content": "You are Mithra, a household assistant."},
            {"role": "user", "content": request.text or ""},
        ]
        try:
            llm_resp = self._llm_router.complete(
                TaskType.NATURAL_LANGUAGE_DRAFTING,
                request.domain_hint or "general",
                messages,
            )
            return {"response_text": llm_resp.content}
        except Exception as exc:  # noqa: BLE001
            return {"response_text": f"I'm unable to respond right now: {exc}"}

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
            "selected_tool": None,
            "tool_result": None,
            "response_text": "",
            "error": None,
            "stream_events": [],
        }

        try:
            final_state = self._graph.invoke(initial_state)
        except Exception as exc:  # noqa: BLE001
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
            "selected_tool": None,
            "tool_result": None,
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
        try:
            router_health = self._llm_router.health()
            llm_ok = router_health.litellm_reachable
        except Exception:  # noqa: BLE001
            llm_ok = False
        return {
            "status": "ok" if llm_ok else "degraded",
            "registered_tools": list(self._tools.keys()),
            "tool_count": len(self._tools),
            "graph_compiled": self._graph is not None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
