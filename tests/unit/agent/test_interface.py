"""Structural and behavioural tests for mithra/agent/interface.py."""

import dataclasses
import inspect

import pytest

from mithra.agent.interface import (
    AgentAction,
    AgentError,
    AgentErrorCode,
    AgentIntent,
    AgentResult,
    ClarificationRequest,
    InputChannel,
    MCPTool,
)


class TestAgentIntent:
    def test_all_values_match_domain_action_pattern(self):
        for member in AgentIntent:
            parts = member.value.split(".")
            assert len(parts) == 2, (
                f"{member.name} value '{member.value}' must contain exactly one dot"
            )
            domain, action = parts
            assert domain and action, f"{member.name} has empty domain or action segment"

    def test_system_unknown_exists(self):
        assert AgentIntent.SYSTEM_UNKNOWN == "system.unknown"

    def test_system_clarification_needed_exists(self):
        assert AgentIntent.SYSTEM_CLARIFICATION_NEEDED == "system.clarification_needed"


class TestAgentErrorCode:
    _EXPECTED = {
        "LLM_UNAVAILABLE",
        "LLM_TIMEOUT",
        "TOOL_FAILED",
        "TOOL_NOT_FOUND",
        "STORAGE_UNAVAILABLE",
        "CONTEXT_TOO_LONG",
        "LOW_CONFIDENCE",
        "PERMISSION_DENIED",
        "INVALID_REQUEST",
    }

    def test_all_nine_codes_exist(self):
        member_names = {m.name for m in AgentErrorCode}
        missing = self._EXPECTED - member_names
        assert not missing, f"Missing AgentErrorCode members: {missing}"

    def test_exactly_nine_codes(self):
        assert len(list(AgentErrorCode)) == 9


class TestAgentAction:
    def test_output_channel_defaults_to_none(self):
        action = AgentAction(action_type="do_thing", domain="food", payload={})
        assert action.output_channel == "none"

    def test_autonomy_level_defaults_to_one(self):
        action = AgentAction(action_type="do_thing", domain="food", payload={})
        assert action.autonomy_level == 1

    def test_reversibility_defaults_to_full(self):
        action = AgentAction(action_type="do_thing", domain="food", payload={})
        assert action.reversibility == "full"


class TestAgentResult:
    def _minimal(self, **kwargs):
        defaults = dict(
            request_id="r1",
            success=True,
            intent=AgentIntent.SYSTEM_UNKNOWN,
            domain="system",
            confidence=1.0,
            actions=[],
            response_text="ok",
        )
        defaults.update(kwargs)
        return AgentResult(**defaults)

    def test_intent_field_accepts_agent_intent(self):
        result = self._minimal(intent=AgentIntent.FOOD_SUGGEST_MEAL)
        assert isinstance(result.intent, AgentIntent)

    def test_error_field_accepts_agent_error_with_error_code(self):
        err = AgentError(code=AgentErrorCode.TOOL_FAILED, message="oops")
        result = self._minimal(error=err)
        assert result.error.code is AgentErrorCode.TOOL_FAILED

    def test_error_field_defaults_to_none(self):
        assert self._minimal().error is None

    def test_pending_confirmations_defaults_to_empty_list(self):
        result = self._minimal()
        assert result.pending_confirmations == []
        assert isinstance(result.pending_confirmations, list)

    def test_clarification_defaults_to_none(self):
        assert self._minimal().clarification is None


class TestClarificationRequest:
    def test_has_question_field(self):
        cr = ClarificationRequest(question="What did you mean?")
        assert cr.question == "What did you mean?"

    def test_suggestions_defaults_to_empty_list(self):
        cr = ClarificationRequest(question="What?")
        assert cr.suggestions == []

    def test_suggestions_accepts_list(self):
        cr = ClarificationRequest(question="What?", suggestions=["a", "b"])
        assert cr.suggestions == ["a", "b"]


class TestMCPTool:
    _REQUIRED_FIELDS = {
        "name", "domain", "description", "parameters",
        "handler", "autonomy_default", "requires_user_id", "tier",
    }

    def test_has_all_eight_fields(self):
        field_names = {f.name for f in dataclasses.fields(MCPTool)}
        missing = self._REQUIRED_FIELDS - field_names
        assert not missing, f"MCPTool missing fields: {missing}"

    def test_can_be_instantiated_with_all_fields(self):
        tool = MCPTool(
            name="food.update_pantry",
            domain="food",
            description="Update pantry contents",
            parameters={"item": {"type": "string"}},
            handler=lambda **_: None,
            autonomy_default=2,
            requires_user_id=True,
            tier=3,
        )
        assert tool.name == "food.update_pantry"
        assert tool.tier == 3
