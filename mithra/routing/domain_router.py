"""
DomainMemoryRouter — maps (user_id, domain) to the correct context scope and
assembles a ContextSnapshot before every agent call.

Access scopes:
  SHARED     — household + all member contributions; most-recent write wins
  PERSONAL   — personal device only; other users denied
  RESTRICTED — health domain; personal device only; shared device → PERMISSION_DENIED

Access denied response text: "Please ask me that on your phone."

Priority scoring — 5 weighted signals assembled per context request:
  1. Explicit domain_hint           +10.0
  2. Deadline within 7 days         +3.0 urgent  /  +1.0 soon
  3. Recent interaction frequency   +2.0 (decays by half per day)
  4. Time-of-day patterns           +1.5 (food 5–7 pm, money month-end, etc.)
  5. Pending level-1 confirmations  +2.0 per action

Top-3 priority domains: full DomainState loaded.
All others: one-line brief summary only.

Preference learning triggers: CORRECTION | ACTION_REJECTED | ACTION_CONFIRMED
| EXPLICIT_STATEMENT. LearnedPreference applied only above confidence 0.7.

HouseholdConfig loaded from household.yaml at startup. No hardcoded user IDs.
UserIdentityResolver maps Telegram IDs / device IDs → Mithra user_ids.
Unknown device → "household" (safe default).

Deadline windows per domain sourced from services/autonomy/autonomy.yaml.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class AccessScope(str, Enum):
    SHARED = "shared"
    PERSONAL = "personal"
    RESTRICTED = "restricted"


DOMAIN_ACCESS_MAP: dict[str, AccessScope] = {
    "food": AccessScope.SHARED,
    "home": AccessScope.SHARED,
    "environment": AccessScope.SHARED,
    "shopping": AccessScope.SHARED,
    "travel": AccessScope.SHARED,
    "money": AccessScope.SHARED,
    "documents": AccessScope.SHARED,
    "people": AccessScope.SHARED,
    "vehicles": AccessScope.SHARED,
    "legal": AccessScope.SHARED,
    "digital": AccessScope.SHARED,
    "time": AccessScope.PERSONAL,
    "goals": AccessScope.PERSONAL,
    "family": AccessScope.PERSONAL,
    "health": AccessScope.RESTRICTED,
}


@dataclass
class AccessResult:
    """Outcome of an access check for a (user_id, domain) pair."""

    allowed: bool
    scope: AccessScope
    context_key: str
    denial_reason: str | None = None


@dataclass
class DomainBrief:
    """One-line summary for a non-priority domain."""

    domain: str
    summary: str
    priority_score: float


@dataclass
class DomainState:
    """Full state snapshot for a priority domain."""

    domain: str
    user_id: str = ""
    # "person_a" | "person_b" | "household"
    facts: dict = field(default_factory=dict)
    recent_actions: list[dict] = field(default_factory=list)
    pending_confirmations: list[dict] = field(default_factory=list)
    upcoming_deadlines: list[dict] = field(default_factory=list)
    preferences: dict = field(default_factory=dict)
    priority_score: float = 0.0


@dataclass
class ContextSnapshot:
    """Pre-assembled context loaded before every agent call.

    Top-3 priority domains have full DomainState. All others have brief summaries.
    Reduces tool calls for common knowledge queries dramatically.
    """

    user_id: str
    device_id: str
    priority_domains: list[DomainState] = field(default_factory=list)
    domain_briefs: list[DomainBrief] = field(default_factory=list)
    known_pii: dict[str, str] = field(default_factory=dict)
    assembled_at: str = ""


@dataclass
class LearnedPreference:
    """A preference inferred from observed user behaviour."""

    domain: str
    key: str
    value: object
    confidence: float
    observation_count: int
    trigger: str


class DomainMemoryRouter(ABC):
    """Resolves access rules and assembles ContextSnapshots for agent calls."""

    @abstractmethod
    def resolve_access(self, user_id: str, domain: str) -> AccessResult:
        """Return the access result for a (user_id, domain) pair."""
        ...

    @abstractmethod
    def assemble_context(
        self,
        user_id: str,
        device_id: str,
        domain_hint: str | None = None,
    ) -> ContextSnapshot:
        """Build a ContextSnapshot for an agent call.

        Scores all domains, loads full DomainState for top 3,
        brief summaries for the rest.
        """
        ...

    @abstractmethod
    def get_domain_state(self, domain: str, user_id: str) -> DomainState:
        """Return the current full DomainState for a domain."""
        ...

    @abstractmethod
    def update_preferences(
        self, user_id: str, domain: str, preferences: dict
    ) -> None:
        """Persist updated preferences for a user in a domain."""
        ...

    @abstractmethod
    def health(self) -> dict:
        """Return a health status dict for the health dashboard."""
        ...
