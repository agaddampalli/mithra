from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class PermissionTier(Enum):
    """Permission level controlling whether a tool auto-executes.

    DESTRUCTIVE is a ONE-WAY DOOR: always requires user confirmation
    regardless of any autonomy override. Cannot be bypassed.
    """

    READ = "read"               # always execute, no confirmation
    WRITE = "write"             # confirm per autonomy config
    DESTRUCTIVE = "destructive" # ALWAYS confirm, no override ever


@dataclass
class ToolMetadata:
    """Runtime metadata for a domain tool.

    Decoupled from the tool function so AutononomyEngine, DashboardAPI,
    and future MCP migration can inspect properties without calling the tool.
    """

    name: str                       # matches @tool name; format: {domain}_{action}_{object}
    domain_id: str                  # owning domain e.g. "time"
    permission: PermissionTier
    is_reversible: bool             # True if undo is supported
    autonomy_level: int             # default autonomy level (1=SURFACE, 3=ACT_SILENT)
    event_emitted: str | None = None  # event_type emitted after success


@dataclass
class ToolBundle:
    """A LangChain tool paired with its Mithra runtime metadata.

    tool.args_schema (Pydantic) generates valid JSON Schema for both
    LangChain bind_tools AND future MCP server migration.
    """

    tool: Any           # langchain_core.tools.BaseTool
    metadata: ToolMetadata
