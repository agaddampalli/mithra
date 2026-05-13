"""
LangGraph adapter — implements AgentInterface using LangGraph as the underlying
orchestration engine.

LangGraph imports are confined to this module. No other file in the codebase
may import from langgraph directly.

The compiled graph is built from registered MCPTools at startup. Each MCPTool
becomes a LangGraph node; the router node calls LLMRouter to classify intent
and dispatch to the correct tool node.
"""

from __future__ import annotations

import time
import uuid
from typing import Generator

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
    MCPTool,
    StreamEventType,
)
from mithra.routing.llm_router import LLMRouter, TaskType


class LangGraphAdapter(AgentInterface):
    """LangGraph implementation of AgentInterface."""

    def __init__(self, llm_router: LLMRouter | None = None) -> None:
        self._tools: dict[str, MCPTool] = {}
        self._graph = None
        self._llm_router = llm_router

    def _build_graph(self) -> None:
        """Compile the LangGraph state machine from registered tools."""
        # Import here to confine LangGraph dependency to this module
        from langgraph.graph import StateGraph, END
        from typing import TypedDict, Annotated
        import operator

        class AgentState(TypedDict):
            request: AgentRequest
            intent: str
            domain: str
            confidence: float
            actions: list[AgentAction]
            response_text: str
            error: AgentError | None
            clarification: ClarificationRequest | None

        graph = StateGraph(AgentState)

        def router_node(state: AgentState) -> AgentState:
            request = state["request"]
            text = request.text or ""
            context = {"domain_hint": request.domain_hint, "user_id": request.user_id}
            if self._llm_router:
                classification = self._llm_router.classify_intent(text, context)
                if classification is None:
                    state["clarification"] = ClarificationRequest(
                        question="Could you clarify what you'd like me to do?",
                        suggestions=[t.name for t in list(self._tools.values())[:3]],
                    )
                    state["confidence"] = 0.0
                    state["intent"] = AgentIntent.SYSTEM_CLARIFICATION_NEEDED
                    state["domain"] = "system"
                else:
                    state["intent"] = classification.intent
                    state["domain"] = classification.domain
                    state["confidence"] = classification.confidence
            else:
                state["intent"] = AgentIntent.SYSTEM_UNKNOWN
                state["domain"] = "system"
                state["confidence"] = 0.0
            return state

        def tool_node(state: AgentState) -> AgentState:
            intent = state.get("intent", "")
            tool = self._tools.get(intent)
            if tool is None:
                state["error"] = AgentError(
                    code=AgentErrorCode.TOOL_NOT_FOUND,
                    message=f"No tool registered for intent '{intent}'",
                )
                state["response_text"] = "I don't know how to do that yet."
                return state
            try:
                request = state["request"]
                result = tool.handler(
                    payload=request.signal or {},
                    user_id=request.user_id if tool.requires_user_id else None,
                )
                state["actions"] = result.get("actions", [])
                state["response_text"] = result.get("response_text", "Done.")
            except Exception as exc:
                state["error"] = AgentError(
                    code=AgentErrorCode.TOOL_FAILED,
                    message=str(exc),
                )
                state["response_text"] = "Something went wrong with that request."
            return state

        def should_execute(state: AgentState) -> str:
            if state.get("clarification"):
                return END
            if state.get("error"):
                return END
            return "tool"

        graph.add_node("router", router_node)
        graph.add_node("tool", tool_node)
        graph.set_entry_point("router")
        graph.add_conditional_edges("router", should_execute, {"tool": "tool", END: END})
        graph.add_edge("tool", END)

        self._graph = graph.compile()

    def _run_graph(self, request: AgentRequest, request_id: str, start: float) -> AgentResult:
        if self._graph is None:
            self._build_graph()

        initial_state = {
            "request": request,
            "intent": AgentIntent.SYSTEM_UNKNOWN,
            "domain": "system",
            "confidence": 0.0,
            "actions": [],
            "response_text": "",
            "error": None,
            "clarification": None,
        }
        try:
            final_state = self._graph.invoke(initial_state)
        except Exception as exc:
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

        intent_val = final_state.get("intent", AgentIntent.SYSTEM_UNKNOWN)
        if isinstance(intent_val, str):
            try:
                intent_val = AgentIntent(intent_val)
            except ValueError:
                intent_val = AgentIntent.SYSTEM_UNKNOWN

        return AgentResult(
            request_id=request_id,
            success=final_state.get("error") is None,
            intent=intent_val,
            domain=final_state.get("domain", "system"),
            confidence=final_state.get("confidence", 0.0),
            actions=final_state.get("actions", []),
            response_text=final_state.get("response_text", ""),
            clarification=final_state.get("clarification"),
            error=final_state.get("error"),
            latency_ms=int((time.time() - start) * 1000),
        )

    def run(self, request: AgentRequest) -> AgentResult:
        """Process a request synchronously via the compiled LangGraph."""
        request_id = request.request_id or str(uuid.uuid4())
        start = time.time()
        return self._run_graph(request, request_id, start)

    def run_stream(
        self, request: AgentRequest
    ) -> Generator[AgentStreamEvent, None, AgentResult]:
        """Process a request and yield stream events using LangGraph streaming."""
        if self._graph is None:
            self._build_graph()

        request_id = request.request_id or str(uuid.uuid4())
        start = time.time()

        initial_state = {
            "request": request,
            "intent": AgentIntent.SYSTEM_UNKNOWN,
            "domain": "system",
            "confidence": 0.0,
            "actions": [],
            "response_text": "",
            "error": None,
            "clarification": None,
        }

        final_state = initial_state
        try:
            for event in self._graph.stream(initial_state):
                for node_name, state in event.items():
                    final_state = {**final_state, **state}
                    if state.get("response_text"):
                        yield AgentStreamEvent(
                            event_type=StreamEventType.TEXT_DELTA,
                            data={"delta": state["response_text"]},
                        )
                    if state.get("actions"):
                        yield AgentStreamEvent(
                            event_type=StreamEventType.ACTION_PROPOSED,
                            data={"actions": [a.__dict__ for a in state["actions"]]},
                        )
        except Exception as exc:
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

        intent_val = final_state.get("intent", AgentIntent.SYSTEM_UNKNOWN)
        if isinstance(intent_val, str):
            try:
                intent_val = AgentIntent(intent_val)
            except ValueError:
                intent_val = AgentIntent.SYSTEM_UNKNOWN

        return AgentResult(
            request_id=request_id,
            success=final_state.get("error") is None,
            intent=intent_val,
            domain=final_state.get("domain", "system"),
            confidence=final_state.get("confidence", 0.0),
            actions=final_state.get("actions", []),
            response_text=final_state.get("response_text", ""),
            clarification=final_state.get("clarification"),
            error=final_state.get("error"),
            latency_ms=int((time.time() - start) * 1000),
        )

    def run_briefing(self, user_id: str) -> AgentResult:
        """Generate a proactive briefing for the user."""
        from mithra.agent.interface import InputChannel
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
        """Confirm a pending action — delegated to AutononomyEngine by the gateway."""
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
        """Reject a pending action — delegated to AutononomyEngine by the gateway."""
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
        """Register an MCP domain tool. Triggers graph rebuild."""
        self._tools[tool.name] = tool
        self._graph = None  # force rebuild on next call

    def health(self) -> dict:
        """Return agent health status."""
        return {
            "status": "ok",
            "registered_tools": list(self._tools.keys()),
            "graph_compiled": self._graph is not None,
        }
