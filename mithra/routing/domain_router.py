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

import asyncio
import calendar
import concurrent.futures
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _call_async(coro):
    """Run a coroutine synchronously, handling both sync and async contexts.

    If called from within a running event loop (e.g. FastAPI handler), the
    coroutine is offloaded to a fresh thread with its own loop. If called from
    a plain sync context, asyncio.run() is used directly.
    """
    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        return asyncio.run(coro)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Concrete implementation
# ---------------------------------------------------------------------------

class DefaultDomainMemoryRouter(DomainMemoryRouter):
    """Production implementation of DomainMemoryRouter.

    All dependencies are injected so the class is fully testable without
    real storage backends or network calls.
    """

    def __init__(
        self,
        storage_router,
        context_cache,
        structured_store,
        household_config: dict,
    ) -> None:
        self._storage_router = storage_router
        self._context_cache = context_cache
        self._structured_store = structured_store
        self._household_config = household_config

    # ------------------------------------------------------------------
    # resolve_access
    # ------------------------------------------------------------------

    def resolve_access(self, user_id: str, domain: str) -> AccessResult:
        """Map (user_id, domain) to an AccessResult using DOMAIN_ACCESS_MAP."""
        scope = DOMAIN_ACCESS_MAP.get(domain, AccessScope.SHARED)

        if scope == AccessScope.SHARED:
            return AccessResult(
                allowed=True,
                scope=scope,
                context_key=f"{domain}:household",
            )

        if scope == AccessScope.PERSONAL:
            if user_id == "household":
                return AccessResult(
                    allowed=False,
                    scope=scope,
                    context_key="",
                    denial_reason="Please ask me that on your phone.",
                )
            return AccessResult(
                allowed=True,
                scope=scope,
                context_key=f"{domain}:{user_id}",
            )

        # RESTRICTED (health only)
        if user_id == "household":
            return AccessResult(
                allowed=False,
                scope=scope,
                context_key="",
                denial_reason="Health information requires your personal device.",
            )
        return AccessResult(
            allowed=True,
            scope=scope,
            context_key=f"{domain}:{user_id}",
        )

    # ------------------------------------------------------------------
    # assemble_context
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        user_id: str,
        device_id: str,
        domain_hint: str | None = None,
    ) -> ContextSnapshot:
        """Build and cache a ContextSnapshot, or return a cached one if fresh."""
        # Step 1 — check cache
        cached = self._context_cache.get(user_id, device_id)
        if cached is not None:
            return cached

        now = _now_utc()

        # Step 2 — get domains accessible to this user
        if user_id == "household":
            accessible = [d for d, s in DOMAIN_ACCESS_MAP.items() if s == AccessScope.SHARED]
        else:
            accessible = list(DOMAIN_ACCESS_MAP.keys())

        # Step 3 — score every accessible domain
        scores: dict[str, float] = {
            domain: self._score_domain(domain, user_id, domain_hint, now)
            for domain in accessible
        }

        # Step 4 — pick top 3; always include domain_hint first if provided
        sorted_domains = sorted(scores, key=lambda d: scores[d], reverse=True)
        priority: list[str] = []
        if domain_hint and domain_hint in accessible:
            priority.append(domain_hint)
        for d in sorted_domains:
            if d not in priority:
                priority.append(d)
            if len(priority) == 3:
                break

        # Step 5 — load full DomainState for top 3
        priority_states: list[DomainState] = []
        for domain in priority:
            try:
                state = self._load_domain_state(domain, user_id, scores.get(domain, 0.0))
                priority_states.append(state)
            except Exception:
                priority_states.append(
                    DomainState(domain=domain, priority_score=scores.get(domain, 0.0))
                )

        # Step 6 — brief summaries for non-priority domains
        priority_set = set(priority)
        briefs: list[DomainBrief] = [
            DomainBrief(domain=d, summary="", priority_score=scores[d])
            for d in sorted_domains
            if d not in priority_set
        ]

        # Step 7 — extract known PII from people domain for DataSanitiser
        known_pii = self._extract_known_pii(user_id)

        # Step 8 — assemble, cache, return
        snapshot = ContextSnapshot(
            user_id=user_id,
            device_id=device_id,
            priority_domains=priority_states,
            domain_briefs=briefs,
            known_pii=known_pii,
            assembled_at=now.isoformat(),
        )
        self._context_cache.set(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # get_domain_state
    # ------------------------------------------------------------------

    def get_domain_state(self, domain: str, user_id: str) -> DomainState:
        """Return a fresh (non-cached) DomainState for a domain."""
        return self._load_domain_state(domain, user_id, priority_score=0.0)

    # ------------------------------------------------------------------
    # update_preferences
    # ------------------------------------------------------------------

    def update_preferences(self, user_id: str, domain: str, preferences: dict) -> None:
        """Persist preferences and invalidate the user's cached context."""
        for key, value in preferences.items():
            _call_async(self._structured_store.set_pref(user_id, f"{domain}.{key}", value))
        self._context_cache.invalidate_user(user_id)

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """Return aggregated health status."""
        try:
            cache_health = self._context_cache.health()
            return {"status": "ok", "cache": cache_health, "error": None}
        except Exception as exc:
            return {"status": "error", "cache": None, "error": str(exc)}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _uid_for_domain(self, domain: str, user_id: str) -> str:
        """Storage user_id: shared domains always use 'household'."""
        scope = DOMAIN_ACCESS_MAP.get(domain, AccessScope.SHARED)
        return "household" if scope == AccessScope.SHARED else user_id

    def _score_domain(
        self,
        domain: str,
        user_id: str,
        domain_hint: str | None,
        now: datetime,
    ) -> float:
        score = 0.0

        # Signal 1 — explicit hint
        if domain == domain_hint:
            score += 10.0

        # Signal 2 — upcoming deadlines in state records
        try:
            uid = self._uid_for_domain(domain, user_id)
            records = self._storage_router.get_adapter(domain).list(domain, "state", uid)
            today = now.date()
            for record in records:
                for date_key in ("due_date", "expiry_date"):
                    raw = record.data.get(date_key)
                    if raw is None:
                        continue
                    try:
                        due = datetime.fromisoformat(str(raw)).date()
                        days_until = (due - today).days
                        if days_until <= 3:
                            score += 3.0
                        elif days_until <= 14:
                            score += 1.0
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

        # Signal 3 — recent interaction frequency (proxy: history entry count)
        try:
            history = _call_async(
                self._structured_store.get_history(domain, limit=50, user_id=user_id)
            )
            score += min(len(history) * 0.5, 2.0)
        except Exception:
            pass

        # Signal 4 — time-of-day / calendar patterns
        if domain == "food" and now.hour in (17, 18, 19):
            score += 1.5
        elif domain == "money":
            last_day = calendar.monthrange(now.year, now.month)[1]
            if now.day == last_day:
                score += 1.5
        elif domain == "home" and now.weekday() in (5, 6):
            score += 1.5

        # Signal 5 — pending level-1 confirmations for this domain
        try:
            pending_records = self._storage_router.get_adapter("autonomy").list(
                "autonomy", "state", user_id
            )
            count = sum(1 for r in pending_records if r.data.get("domain") == domain)
            score += count * 2.0
        except Exception:
            pass

        return score

    def _load_domain_state(
        self, domain: str, user_id: str, priority_score: float
    ) -> DomainState:
        """Load all 4 storage layers for a domain and assemble DomainState."""
        uid = self._uid_for_domain(domain, user_id)
        adapter = self._storage_router.get_adapter(domain)

        facts: dict = {}
        recent_actions: list[dict] = []
        upcoming_deadlines: list[dict] = []
        preferences: dict = {}

        try:
            for r in adapter.list(domain, "facts", uid):
                facts[r.key] = r.data
        except Exception:
            pass

        try:
            for r in adapter.list(domain, "history", uid):
                recent_actions.append(r.data)
        except Exception:
            pass

        try:
            for r in adapter.list(domain, "state", uid):
                if "due_date" in r.data or "expiry_date" in r.data:
                    upcoming_deadlines.append(r.data)
        except Exception:
            pass

        try:
            for r in adapter.list(domain, "preferences", uid):
                preferences[r.key] = r.data
        except Exception:
            pass

        return DomainState(
            domain=domain,
            user_id=uid,
            facts=facts,
            recent_actions=recent_actions,
            pending_confirmations=[],
            upcoming_deadlines=upcoming_deadlines,
            preferences=preferences,
            priority_score=priority_score,
        )

    def _extract_known_pii(self, user_id: str) -> dict[str, str]:
        """Build known_pii map from people domain facts for DataSanitiser."""
        known_pii: dict[str, str] = {}
        try:
            uid = self._uid_for_domain("people", user_id)
            for record in self._storage_router.get_adapter("people").list("people", "facts", uid):
                data = record.data
                if "name" in data:
                    token = f"[PERSON_{record.key.upper()}]"
                    known_pii[data["name"]] = token
                if "phone" in data:
                    known_pii[data["phone"]] = "[PHONE]"
                if "email" in data:
                    known_pii[data["email"]] = "[EMAIL]"
        except Exception:
            pass
        return known_pii
