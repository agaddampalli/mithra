"""
AgentInterface — abstract base class for all agent implementations.

All agent calls go through this interface. LangGraph is the first adapter;
Strands is the designated second adapter when the product phase warrants it.
No code outside the agent/ directory may import from langgraph.

Gateway owns the full request lifecycle:
  1. Receive normalised AgentRequest from input adapter
  2. Load ContextSnapshot via DomainMemoryRouter
  3. Call agent.run() or agent.run_stream()
  4. Pass AgentResult to AutononomyEngine.process()
  5. Pass ExecutionReceipt to output adapter
  6. Return response to user

Agent is stateless — it never owns sessions or call engine/adapters directly.

AgentIntent: typed enum, pattern = domain.action. Maps 1:1 to MCPTool.name.
  Examples: "people.send_birthday_message", "food.update_pantry"

AgentErrorCode: typed enum for all structured error codes.
  AgentError.code is AgentErrorCode — never a raw string.

ClarificationRequest: returned when intent confidence < 0.75.
  Contains a question + suggestions. Input adapter prompts the user.
  Session stays open. Next turn resolves it.

StreamEventType: TEXT_DELTA | TOOL_START | TOOL_RESULT | ACTION_PROPOSED |
  ACTION_TAKEN | THINKING | DONE

MCPTool: registered at startup via register_tool(). Domain plugins register
  themselves; new domain = new MCPTool, no changes to core agent code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generator

from mithra.routing.domain_router import ContextSnapshot
from mithra.routing.session import ConversationTurn


class InputChannel(str, Enum):
    """The channel through which an AgentRequest arrived."""

    VOICE = "voice"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    UPLOAD = "upload"
    EMAIL = "email"
    SCHEDULER = "scheduler"
    API = "api"


class StreamEventType(str, Enum):
    TEXT_DELTA = "text_delta"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    ACTION_PROPOSED = "action_proposed"
    ACTION_TAKEN = "action_taken"
    THINKING = "thinking"
    DONE = "done"


class AgentIntent(str, Enum):
    """Typed intent enum. Pattern: domain.action. Maps 1:1 to MCPTool.name."""

    # People
    PEOPLE_ADD_CONTACT           = "people.add_contact"
    PEOPLE_SEND_BIRTHDAY_MESSAGE = "people.send_birthday_message"
    PEOPLE_LOG_GIFT              = "people.log_gift"
    # Money
    MONEY_LOG_EXPENSE            = "money.log_expense"
    MONEY_CHECK_BUDGET           = "money.check_budget"
    MONEY_ADD_SUBSCRIPTION       = "money.add_subscription"
    # Home
    HOME_LOG_MAINTENANCE         = "home.log_maintenance"
    HOME_ADD_APPLIANCE           = "home.add_appliance"
    # Food
    FOOD_UPDATE_PANTRY           = "food.update_pantry"
    FOOD_GENERATE_GROCERY_LIST   = "food.generate_grocery_list"
    FOOD_SUGGEST_MEAL            = "food.suggest_meal"
    # System
    SYSTEM_DAILY_BRIEFING        = "system.daily_briefing"
    SYSTEM_UNKNOWN               = "system.unknown"
    SYSTEM_CLARIFICATION_NEEDED  = "system.clarification_needed"


@dataclass
class AgentRequest:
    """Normalised input to the agent, regardless of origin channel."""

    user_id: str
    device_id: str
    input_channel: InputChannel
    text: str | None = None
    file_path: str | None = None
    signal: dict[str, Any] | None = None
    domain_hint: str | None = None
    request_id: str = ""
    session_id: str = ""
    conversation_history: list[ConversationTurn] = field(default_factory=list)
    context: ContextSnapshot | None = None


@dataclass
class AgentAction:
    """A single concrete action the agent wants to take."""

    action_type: str
    domain: str
    payload: dict[str, Any]
    output_channel: str = "none"
    # "calendar"|"whatsapp"|"telegram"|"tts"|"push"|"none"
    # AutononomyEngine uses this to route to the correct OutputAdapter
    autonomy_level: int = 1
    reversibility: str = "full"
    # Human-readable description set by the agent (e.g. "add 'Dentist' to your calendar").
    # Engine uses this directly so it never needs to know individual action types.
    description: str = ""


@dataclass
class ClarificationRequest:
    """Returned when intent confidence is below the 0.75 threshold."""

    question: str
    suggestions: list[str] = field(default_factory=list)


class AgentErrorCode(str, Enum):
    LLM_UNAVAILABLE     = "llm_unavailable"
    LLM_TIMEOUT         = "llm_timeout"
    TOOL_FAILED         = "tool_failed"
    TOOL_NOT_FOUND      = "tool_not_found"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    CONTEXT_TOO_LONG    = "context_too_long"
    LOW_CONFIDENCE      = "low_confidence"
    PERMISSION_DENIED   = "permission_denied"
    INVALID_REQUEST     = "invalid_request"


@dataclass
class AgentError:
    """Structured error returned in AgentResult on failure."""

    code: AgentErrorCode
    message: str
    recoverable: bool = True


@dataclass
class AgentResult:
    """Normalised output from the agent for a single request."""

    request_id: str
    success: bool
    intent: AgentIntent
    domain: str
    confidence: float
    actions: list[AgentAction]
    response_text: str
    pending_confirmations: list[AgentAction] = field(default_factory=list)
    clarification: ClarificationRequest | None = None
    error: AgentError | None = None
    llm_route: str = ""
    latency_ms: int = 0


@dataclass
class AgentStreamEvent:
    """A single event yielded by run_stream()."""

    event_type: StreamEventType
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPTool:
    """A domain capability registered with the agent at startup."""

    name: str
    domain: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    autonomy_default: int
    requires_user_id: bool
    tier: int


class AgentInterface(ABC):
    """Abstract base class for agent orchestration implementations."""

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult:
        """Process a request synchronously. Used by scheduler, signals, background jobs."""
        ...

    @abstractmethod
    def run_stream(
        self, request: AgentRequest
    ) -> Generator[AgentStreamEvent, None, AgentResult]:
        """Process a request and yield stream events. Returns final AgentResult on completion."""
        ...

    @abstractmethod
    def run_briefing(self, user_id: str) -> AgentResult:
        """Generate a proactive morning/evening briefing for the user."""
        ...

    @abstractmethod
    def confirm(self, request_id: str, action_index: int, user_id: str) -> AgentResult:
        """Confirm a pending action from a prior request."""
        ...

    @abstractmethod
    def reject(self, request_id: str, action_index: int, user_id: str) -> AgentResult:
        """Reject a pending action from a prior request."""
        ...

    @abstractmethod
    def register_tool(self, tool: MCPTool) -> None:
        """Register an MCP domain tool at startup."""
        ...

    @abstractmethod
    def health(self) -> dict:
        """Return a health status dict for the health dashboard."""
        ...
