# Mithra — Personal Life OS

## What this project is
Mithra is a self-hosted, voice-first agentic orchestration layer that manages
all life domains for a household. It reasons across 15 life domains, surfaces
what needs attention proactively, and acts through native channels the
household already uses — Google Calendar, WhatsApp, smart speakers — without
requiring a new app or dashboard.

Full requirements: docs/requirements.pdf
All 7 architectural decisions: docs/decisions.pdf
System architecture diagram: docs/architecture_pass1.html

This file gives Claude Code everything needed to build correctly without
re-reading the PDFs every session.

## Project status
Phase 0 — Base infrastructure. No domain logic yet.
Every infrastructure component must be built and verified before any domain
work begins. See "Phase 0 build order" below.

---

## Architecture decisions (all locked, do not deviate)

### Orchestration
- Framework: LangGraph as the agent brain
- Pattern: AgentInterface abstraction — LangGraph is the first adapter
- All agent calls go through agent/interface.py, never LangGraph directly
- MCP tools are the domain plugin contract — framework-agnostic
- Strands is the designated second adapter when product phase warrants it

### LLM routing
- Proxy: LiteLLM (runs as a Docker container, port 4000)
- Local: Ollama with phi3-mini — handles simple structured tasks
- Cloud: Anthropic claude-sonnet-4-6 — reasoning, drafting, parsing
- Health domain: local model ONLY, hardcoded — never cloud, ever
- All routing defined in services/litellm/routing.yaml
- Agent tags every call with TaskType — router validates domain independently
- domain is a required parameter on LLMRouter.complete() — not optional
- Two independent health guards: agent TaskType + router domain validation

### Messaging
- Household ↔ agent: Telegram Bot API (python-telegram-bot)
- Social outbound (birthday messages etc): WhatsApp via Baileys (Node.js)
- Both sit behind MessagingAdapter interface in adapters/messaging/

### Voice
- Platform: Home Assistant Assist pipeline
- STT: Whisper via Wyoming protocol
- TTS: Piper via Wyoming protocol
- Wake word: openWakeWord
- Mithra API gateway is HA's conversation agent
- No Gemini, no Alexa — both are closed platforms
- Google Home Mini used as TTS speaker via Google Cast only

### User identity
- Personal phone (HA Companion App) → user_id = person_a or person_b
- Shared devices (tablet, ESP32, fridge) → user_id = household
- DomainMemoryRouter in routing/domain_router.py handles the split
- Private domains (Health, Goals) → personal context only
- Shared domains (Food, Home, Money, People) → household context
- Unknown device ID defaults to "household" — safe fallback
- HouseholdConfig loaded from household.yaml at startup
- No hardcoded user IDs anywhere in code — YAML only

### Storage
- Interface: StorageAdapter in storage/adapter.py
- Primary: OneDriveAdapter (Microsoft Graph API + SQLite cache)
- Exception: Health domain → LocalStorageAdapter ALWAYS, hardcoded
- StorageRouter in storage/router.py enforces the health exception
- No domain module ever references a file path directly
- BYOS model: user data lives in their own OneDrive, Mithra holds indexes only
- OneDriveAdapter has local SQLite cache + async write queue for resilience

### Data portability
- JSON per domain now (personal use phase)
- iCal (Time), OFX (Money), vCard (People) added before first external user
- ExportAdapter per domain: to_json() and to_standard_format()

### Deployment
- Single Docker Compose stack — one compose.yaml starts everything
- Target runtime: Windows laptop
- Development: Mac (build and test locally, deploy via git pull)
- Watchtower for automatic container updates
- Cloudflare tunnel → api.mithra-app.com (no port forwarding needed)
- Webhook in production, polling fallback if tunnel is unreachable

### Data sensitivity tiers
- Tier 1 — Health: LocalStorageAdapter ONLY. Never cloud LLM. Hardcoded.
- Tier 2 — Money, Documents, People: OneDrive. Anonymised before cloud LLM.
- Tier 3 — Everything else: OneDrive. Cloud LLM fine.

---

## Naming conventions (strict — never deviate)

```
Package/namespace    mithra  (top-level only)
Internal modules     named by responsibility, never branded
                     e.g. storage/adapter.py NOT storage/mithra_storage.py
Classes              named by role
                     e.g. StorageAdapter, AgentInterface, ActionLog
                     NOT MithraStorage, MithraAgent, MithraLog
Docker services      named by function
                     e.g. agent, llm-router, vector-store
                     NOT mithra-agent, mithra-llm
Config files         named by scope
                     e.g. llm_routing.yaml, storage.yaml
                     NOT mithra.yaml
API routes           /api/v1/...  (not /mithra/v1/...)
```

---

## Tech stack

```
Language             Python 3.12
Agent framework      LangGraph
LLM proxy            LiteLLM (Docker container, port 4000)
Local LLM            Ollama with phi3-mini (Docker container, port 11434)
Cloud LLM            Anthropic claude-sonnet-4-6
Vector DB            ChromaDB (Docker container, port 8001)
Structured DB        SQLite (local file, persisted Docker volume)
Voice STT            Whisper via Wyoming protocol (port 10300)
Voice TTS            Piper via Wyoming protocol (port 10200)
Messaging in         python-telegram-bot
Messaging out        Baileys (Node.js, Docker container)
Storage              Microsoft Graph API (OneDrive) + local filesystem
Tunnel               cloudflared → api.mithra-app.com
Gateway              FastAPI (port 8000)
Orchestration        Docker Compose (single compose.yaml)
```

---

## Project structure

```
mithra/
  CLAUDE.md
  README.md
  compose.yaml                    single file, all services
  .env.example                    all required env vars documented
  .env                            never committed
  .gitignore
  household.yaml                  member config, loaded at startup

  docs/
    requirements.pdf
    decisions.pdf
    architecture_pass1.html

  services/
    litellm/
      routing.yaml                model routing + token budgets
    ollama/
      Dockerfile                  pulls phi3-mini on build
    autonomy/
      autonomy.yaml               action levels + deadline windows
    cloudflared/
      config.yaml                 tunnel config for api.mithra-app.com

  mithra/                         main Python package
    __init__.py

    agent/
      __init__.py
      interface.py                AgentInterface abstract class
      langgraph_adapter.py        LangGraph implementation

    routing/
      __init__.py
      llm_router.py               LLMRouter interface + LiteLLM adapter
      domain_router.py            DomainMemoryRouter — access + context
      session.py                  ConversationSessionManager

    storage/
      __init__.py
      adapter.py                  StorageAdapter abstract class
      router.py                   StorageRouter — health domain hardcoded
      onedrive.py                 OneDriveAdapter (Graph API + cache)
      local.py                    LocalStorageAdapter
      encryption.py               EncryptedStorageAdapter + AESGCMCipher
      sanitiser.py                DataSanitiser — PII stripping for cloud

    memory/
      __init__.py
      vector_store.py             ChromaDB wrapper
      structured_store.py         SQLite wrapper (facts, state, history)
      context_cache.py            ContextSnapshot cache (30s TTL)

    autonomy/
      __init__.py
      engine.py                   AutononomyEngine — enforce levels
      executor.py                 ActionExecutorRegistry
      action_log.py               append-only action history
      dead_letter.py              DeadLetterQueue for failed deliveries

    adapters/
      __init__.py
      voice/
        __init__.py
        wyoming.py                Wyoming protocol voice input adapter
      messaging/
        __init__.py
        telegram_input.py         TelegramInputAdapter
        telegram_output.py        TelegramOutputAdapter
        whatsapp.py               WhatsAppAdapter (Baileys bridge)
      output/
        __init__.py
        calendar.py               Google Calendar API output
        tts.py                    Piper TTS via HA Google Cast
        push.py                   PWA push notifications
        retry.py                  RetryableOutputAdapter wrapper

    api/
      __init__.py
      gateway.py                  FastAPI app — lifecycle orchestrator
      routes/
        __init__.py
        agent.py                  /api/v1/agent/* routes
        health.py                 /api/v1/health/* routes
        domains.py                /api/v1/domains/* routes
        webhooks.py               /api/v1/webhook/* (Telegram etc)

  domains/                        domain plugins (added in Phase 1+)
    people/
    money/
    home/
    food/

  tests/
    __init__.py
    unit/
    integration/
    conftest.py
```

---

## Interface contracts (all locked)

---

## Adapter base types (mithra/adapters/messaging/__init__.py)

All input and output channel adapters share these base types.
Import from mithra.adapters.messaging — never redefine them elsewhere.

  OutboundMessage     text, format (TEXT|MARKDOWN|VOICE|CARD),
                      domain, priority ("urgent"|"normal"|"low"), reply_to_id

  ActionButton        label, action (confirm|reject|modify), payload
                      Level-1 confirmation UI — rendered per channel

  DeliveryReceipt     channel, message_id, delivered_at, success,
                      undoable, undo_strategy, error
                      Stored in ActionLogEntry.undo_payload

  InputAdapter        start(), stop(), normalise(), acknowledge(), health()
  OutputAdapter       send(), send_with_actions(), send_stream(), undo(), health()

---

### 1. StorageAdapter

StorageRecord is the universal container for all domain data across all
15 domains. domain + key + layer + user_id uniquely identifies any record.

```python
@dataclass
class StorageRecord:
    domain: str         # "people" | "money" | "home" | "autonomy" etc
    key: str            # unique identifier within domain + layer
    layer: str          # "facts" | "state" | "history" | "preferences"
    data: dict          # the actual payload
    user_id: str        # "person_a" | "person_b" | "household"
    version: int        # incremented on every write
    updated_at: str     # ISO timestamp
    content_type: str = "record"  # "record" | "document_index"

class StorageAdapter(ABC):
    # Structured records
    def read(self, domain, key, layer, user_id) -> StorageRecord | None
    def write(self, record: StorageRecord) -> StorageRecord
    def list(self, domain, layer, user_id) -> list[StorageRecord]
    def delete(self, domain, key, user_id) -> bool
    # Binary files
    def write_file(self, domain, file_name, content, mime_type, user_id) -> str
    def read_file(self, file_path) -> bytes
    def delete_file(self, file_path) -> bool
    def health(self) -> dict
```

Document index record (content_type="document_index"):
- data.file_path: where file lives in OneDrive
- data.extracted: structured fields pulled out by OCR + LLM
- data.processing_status: pending | processing | complete | failed

Encryption: EncryptedStorageAdapter wraps any StorageAdapter (decorator pattern).
Algorithm: AES-256-GCM. Field-level for structured data, full content for files.
Key: MITHRA_ENCRYPTION_KEY env var, base64-encoded 32-byte key.

StorageRouter.get_adapter(domain) not resolve(domain).
All callers use router.get_adapter(domain).

StorageRouter hard routing:
```
health    → EncryptedStorageAdapter(LocalStorageAdapter())   HARDCODED ALWAYS
money     → EncryptedStorageAdapter(OneDriveAdapter(), encrypted_fields=[...])
documents → EncryptedStorageAdapter(OneDriveAdapter(), encrypted_fields=[...])
people    → EncryptedStorageAdapter(OneDriveAdapter(), encrypted_fields=[...])
default   → OneDriveAdapter()
```

Cache invalidation: StorageAdapter.write() calls cache.invalidate_domain()
immediately. TTL=30s is a safety net only, not the consistency mechanism.

### 2. AgentInterface

```python
@dataclass
class AgentRequest:
    user_id: str
    device_id: str
    input_channel: InputChannel   # VOICE|TELEGRAM|WHATSAPP|UPLOAD|EMAIL|SCHEDULER|API
    text: str | None = None
    file_path: str | None = None
    signal: dict | None = None
    domain_hint: str | None = None
    request_id: str = ""
    session_id: str = ""
    conversation_history: list[ConversationTurn] = []
    context: ContextSnapshot | None = None   # pre-loaded by DomainMemoryRouter

@dataclass
class AgentResult:
    request_id: str
    success: bool
    intent: AgentIntent            # typed enum: domain.action pattern
    domain: str
    confidence: float
    actions: list[AgentAction]
    response_text: str
    pending_confirmations: list[AgentAction] = []
    clarification: ClarificationRequest | None = None
    error: AgentError | None = None
    llm_route: str = ""
    latency_ms: int = 0

class AgentInterface(ABC):
    def run(self, request: AgentRequest) -> AgentResult
    def run_stream(self, request) -> Generator[AgentStreamEvent, None, AgentResult]
    def run_briefing(self, user_id: str) -> AgentResult
    def confirm(self, request_id, action_index, user_id) -> AgentResult
    def reject(self, request_id, action_index, user_id) -> AgentResult
    def register_tool(self, tool: MCPTool) -> None
    def health(self) -> dict
```

AgentAction fields: action_type, domain, payload,
  output_channel (str, default "none"),
  autonomy_level (int, default 1),
  reversibility (str, default "full")

AgentResult.intent is AgentIntent enum (not str)
AgentResult.error.code is AgentErrorCode enum (not str)

AgentIntent: typed enum, pattern = domain.action.
Maps directly to MCPTool.name. Never free-form strings.
Examples: "people.send_birthday_message", "food.update_pantry"

AgentErrorCode: typed enum for all structured error codes.
  llm_unavailable | llm_timeout | tool_failed | tool_not_found |
  storage_unavailable | context_too_long | low_confidence |
  permission_denied | invalid_request

ClarificationRequest: returned when confidence < 0.75.
Contains question + suggestions. Input adapter prompts user.
Session stays open. Next turn resolves it.

ContextSnapshot: pre-loaded by DomainMemoryRouter before every agent call.
Contains upcoming deadlines, domain summaries, preferences, recent actions.
Reduces tool calls for common knowledge queries dramatically.

StreamEventType: TEXT_DELTA | TOOL_START | TOOL_RESULT |
ACTION_PROPOSED | ACTION_TAKEN | THINKING | DONE

MCPTool: name (domain.action), domain, description, parameters (JSON Schema),
handler (Callable), autonomy_default (0-3), requires_user_id, tier (1-3).
Domain plugins register themselves at startup via register_tool().

Gateway owns the full request lifecycle:
1. Receive normalised AgentRequest from input adapter
2. Load ContextSnapshot from DomainMemoryRouter
3. Call agent.run() or agent.run_stream()
4. Pass AgentResult to AutononomyEngine.process()
5. Pass ExecutionReceipt to output adapter
6. Return response to user
Agent does not call engine. Input adapter does not call engine.

### 3. AutononomyEngine

```python
class AutonomyLevel(Enum):
    OBSERVE     = 0    # no output, log signal only
    SURFACE     = 1    # tell user, they decide
    ACT_TELL    = 2    # act then notify
    ACT_SILENT  = 3    # act, no notification unless failure
    NEVER       = -1   # permanent hard wall, not configurable

class AutononomyEngine(ABC):
    def process(self, result: AgentResult, executor: ActionExecutorRegistry) -> ExecutionReceipt
    def confirm(self, request_id, action_index, user_id, executor) -> ExecutionReceipt
    def reject(self, request_id, action_index, user_id) -> ExecutionReceipt
    def undo(self, action_id, user_id, executor) -> ExecutionReceipt
    def get_pending(self, user_id) -> list[PendingAction]
    def get_log(self, user_id, domain=None, limit=50) -> list[ActionLogEntry]
    def expire_pending(self) -> None
    def health(self) -> dict
```

Process order: NEVER check first → OBSERVE → SURFACE → execute.
NEVER violations: logged for security audit. >3 hits/hour = security alert.
ACT_TELL: execute first, notify after. Never notify without acting.
Multiple ACT_TELL actions in one request: batch into one notification.

ActionLogEntry fields: action_id, request_id, user_id, domain,
  action_type, payload, status, reversibility, timestamp, undo_payload,
  intent, autonomy_level, output_channel,
  executed_at, success, error,
  undoable, undo_expires_at, undo_strategy

Reversibility on ActionLogEntry: FULL | PARTIAL | NONE.
FULL: restore previous state (calendar delete, storage revert).
PARTIAL: send correction follow-up (WhatsApp).
NONE: cannot undo (TTS, push).

PendingAction fields: action_id, request_id, action_index, user_id,
  domain, action_type, payload,
  created_at, expires_at,
  intent, description
Expires 24 hours after creation. User notified on expiry.

RejectedAction: intent, domain, user_id, reason, attempted_at
  NEVER-level blocks only. Logged for security audit.
  >3 hits/hour → security alert.

ExecutionReceipt fields: request_id, executed, pending,
  rejected (list[RejectedAction]),
  skipped (list[dict] — OBSERVE level, logged but not executed),
  notification_sent, response_text

Pending store: StorageAdapter domain="autonomy" layer="state".
Action log: StorageAdapter domain="autonomy" layer="history".
Both survive restarts. No in-memory or Redis dependency.

ActionExecutorRegistry:
  execute(action: AgentAction) -> ExecutionResult
  undo(log_entry: ActionLogEntry) -> ExecutionResult
  notify(user_id, channel, message) -> None
  register(action_type, adapter) -> None
  New output type = one new line in the registration block only.

  ExecutionResult fields: action_id, action_type, success,
    delivery_receipt, error,
    undo_payload (dict — stores what is needed to reverse the action)

### 4. LLMRouter

```python
class TaskType(Enum):
    DATA_CAPTURE          = "data_capture"          # local
    RULE_BASED_ALERT      = "rule_based_alert"       # local
    SIMPLE_RETRIEVAL      = "simple_retrieval"       # local
    INTENT_CLASSIFICATION = "intent_classification"  # local → cloud fallback
    VOICE_DISAMBIGUATION  = "voice_disambiguation"   # local → cloud fallback
    CROSS_DOMAIN_INSIGHT  = "cross_domain_insight"   # cloud
    NATURAL_LANGUAGE_DRAFT= "natural_language_drafting" # cloud
    DOCUMENT_PARSING      = "document_parsing"       # cloud
    MONTHLY_SUMMARY       = "monthly_summary"        # cloud
    WEB_UNDERSTANDING     = "web_understanding"      # cloud
    HEALTH_DOMAIN         = "health_domain"          # local_only — no fallback ever

class LLMRouter(ABC):
    def complete(self, task_type, domain, messages, tools=None, stream=False)
    def classify_intent(self, text, context) -> IntentClassification | None
    def get_usage(self, period="month") -> UsageReport
    def health(self) -> RouterHealth
```

domain is required on complete(). Router validates domain independently.
Health domain forces HEALTH_DOMAIN regardless of agent-assigned TaskType.
TaskType mismatch logged as security event.

local: Phi-3 Mini. Cloud fallback allowed if config permits.
cloud: Claude claude-sonnet-4-6. No fallback.
local_only: Phi-3 Mini. NEVER cloud fallback. Health domain only.

Routing + token budgets: services/litellm/routing.yaml.
Budget exhausted: fall back to local, never silent failure.

LLMResponse:    content, model_used, route_taken, task_type,
  prompt_tokens, completion_tokens, latency_ms,
  fallback_triggered, fallback_reason,
  sanitisation (SanitisationReport | None),
  success, error

LLMStreamChunk: delta, model_used, finished

RoutingEvent:   event_id, timestamp, task_type, domain,
  model_used, route_taken, fallback_triggered,
  task_type_mismatch, sanitisation_applied,
  prompt_tokens, completion_tokens, latency_ms,
  success, error
  Written to StorageAdapter domain="observability"

complete() returns LLMResponse | Generator[LLMStreamChunk, None, LLMResponse]

IntentClassification confidence threshold: 0.75.
classify_intent() returns IntentClassification | None.
  None when confidence < 0.75 — caller creates ClarificationRequest.

UsageReport: add local_calls, cloud_calls, fallback_count fields
RouterHealth: add fallback_active, local_latency_ms, cloud_latency_ms fields

DataSanitiser: two-pass approach.
Pass 1: known PII from ContextSnapshot (high confidence).
Pass 2: regex pattern matching for unknown PII.
Applied before ALL cloud calls — not optional.
SanitisationReport returned in LLMResponse.

_fit_context(): truncates + summarises before every call.
Keeps: system message + last 6 turns + current user message.
Handles CONTEXT_TOO_LONG before it errors.

RoutingEvent: structured log per call in StorageAdapter domain="observability".
Feeds health dashboard. Tracks model_used, route_taken, fallback, tokens, cost.

IntentClassification confidence threshold: 0.75.
Below threshold → ClarificationRequest, never guess.

### 5. DomainMemoryRouter

DomainState includes user_id: str field ("person_a" | "person_b" | "household")

```python
DOMAIN_ACCESS_MAP = {
    "food": SHARED, "home": SHARED, "environment": SHARED,
    "shopping": SHARED, "travel": SHARED, "money": SHARED,
    "documents": SHARED, "people": SHARED, "vehicles": SHARED,
    "legal": SHARED, "digital": SHARED,
    "time": PERSONAL, "goals": PERSONAL, "family": PERSONAL,
    "health": RESTRICTED,   # personal device only, always enforced
}

class DomainMemoryRouter(ABC):
    def resolve_access(self, user_id, domain) -> AccessResult
    def assemble_context(self, user_id, device_id, domain_hint=None) -> ContextSnapshot
    def get_domain_state(self, domain, user_id) -> DomainState
    def update_preferences(self, user_id, domain, preferences) -> None
    def health(self) -> dict
```

Priority scoring — 5 weighted signals:
1. Explicit domain_hint: +10.0
2. Deadlines within 7 days: +3.0 urgent, +1.0 soon
3. Recent interaction frequency (decays half/day): +2.0
4. Time-of-day patterns: +1.5 (food 5-7pm, money month-end etc)
5. Pending level 1 confirmations: +2.0 each

Top 3 priority domains: full DomainState loaded.
All others: one-line brief summary only.

Deadline windows per domain (services/autonomy/autonomy.yaml):
health=7, food=3, money_bills=14, vehicles=60, documents=90,
legal=90, environment=2, people=21, home=30, goals=7, default=14.

ContextCache: 30s TTL in local SQLite.
Write-through invalidation: StorageAdapter.write() calls
cache.invalidate_domain() immediately. TTL is safety net only.

Preference learning triggers: CORRECTION | ACTION_REJECTED |
ACTION_CONFIRMED | EXPLICIT_STATEMENT.
LearnedPreference: confidence (0.0-1.0) + observation_count.
Applied only above confidence threshold 0.7.

Shared domain merge: household + all member contributions.
Most recent write wins on key collision.

HouseholdConfig from household.yaml: members, display names,
device_ids, notification channels. No hardcoded IDs.

UserIdentityResolver: maps Telegram IDs, device IDs, WhatsApp numbers
to Mithra user_ids. Unknown → "household" (safe default).

Access denied (health on shared device): PERMISSION_DENIED error.
Response text: "Please ask me that on your phone."

### 6. MessagingAdapter

```python
class InputAdapter(ABC):
    async def start(self) -> None
    async def stop(self) -> None
    def normalise(self, raw, channel) -> AgentRequest | None
    async def acknowledge(self, raw) -> None
    def health(self) -> dict

class OutputAdapter(ABC):
    async def send(self, user_id, message: OutboundMessage) -> DeliveryReceipt
    async def send_with_actions(self, user_id, message, actions) -> DeliveryReceipt
    async def send_stream(self, user_id, stream) -> DeliveryReceipt
    async def undo(self, receipt: DeliveryReceipt) -> bool
    def health(self) -> dict
```

OutboundMessage: text, format (TEXT|MARKDOWN|VOICE|CARD),
domain, priority, reply_to_id.

ActionButton: label, action (confirm|reject|modify), payload.
Rendered per channel for Level 1 confirmations.

DeliveryReceipt: channel, message_id, delivered_at, success,
undoable, undo_strategy, error. Stored in ActionLogEntry.undo_payload.

VoiceConfirmationSession fields: session_id, request_id,
  action_index, user_id, device_id, expires_at
  device_id is required — Wyoming routes confirmation response
  back to the specific device that spoke the question.

TelegramInputAdapter:
- Webhook in production, auto-fallback to polling if tunnel unreachable
- Normalises: text, voice (transcribed inline), document (async), callback_query
- Document download is async non-blocking
- Status message sent immediately, AgentRequest fires on completion

TelegramOutputAdapter:
- Streaming via message edits, flushed every 500ms
- Rate limits: 1msg/s, 500/day, 15 edits/minute (RateLimiter on every send)
- Level 1: inline keyboard buttons

TTSOutputAdapter:
- Streaming: speaks on sentence boundary detection (., !, ?)
- Level 1: VoiceConfirmationSession stored before speaking
  Wyoming listener resolves against session_id
  30-second timeout with explicit cancellation

RetryableOutputAdapter: wraps any OutputAdapter (decorator pattern).
3 attempts, exponential backoff (1s, 2s, 4s, max 30s delay).
Exhausted → DeadLetterQueue in StorageAdapter domain="observability".
Dead letters surfaced on health dashboard.

---

## Streaming and conversation

- run() is blocking — scheduler, signals, background jobs
- run_stream() yields AgentStreamEvent — voice, Telegram, interactive channels
- Sessions in ConversationSessionManager (routing/session.py)
- Session history in StorageAdapter domain="sessions" layer="state"
- Sessions expire after 10 minutes inactivity
- History capped at 20 turns
- Input adapters load session before agent call via load_history(session_id, user_id)
  Returns empty list if session not found. Populates AgentRequest.conversation_history.
- Output adapters save turn after completion
- Agent is stateless — never owns sessions directly

---

## Phase 0 build order (strict — each needs passing tests before next)

```
 1. compose.yaml
 2. .env.example
 3. household.yaml
 4. storage/adapter.py          StorageAdapter interface
 5. storage/local.py            LocalStorageAdapter
 6. storage/onedrive.py         OneDriveAdapter + SQLite cache + write queue
 7. storage/encryption.py       EncryptedStorageAdapter + AESGCMCipher
 8. storage/router.py           StorageRouter — health hardcoded
 9. memory/vector_store.py      ChromaDB wrapper
10. memory/structured_store.py  SQLite wrapper
11. memory/context_cache.py     write-through invalidation cache
12. routing/domain_router.py    DomainMemoryRouter
13. routing/session.py          ConversationSessionManager
14. agent/interface.py          AgentInterface + all dataclasses
15. agent/langgraph_adapter.py  LangGraph implementation
16. routing/llm_router.py       LLMRouter + LiteLLMAdapter + DataSanitiser
17. autonomy/engine.py          AutononomyEngine — all levels
18. autonomy/executor.py        ActionExecutorRegistry
19. autonomy/action_log.py      append-only log
20. autonomy/dead_letter.py     DeadLetterQueue
21. adapters/messaging/telegram_input.py
22. adapters/messaging/telegram_output.py
23. adapters/messaging/whatsapp.py
24. adapters/output/calendar.py
25. adapters/output/tts.py      + VoiceConfirmationSession
26. adapters/output/push.py
27. adapters/output/retry.py    RetryableOutputAdapter
28. adapters/voice/wyoming.py
29. api/gateway.py              lifecycle orchestrator
30. services/cloudflared/config.yaml
31. /api/v1/health dashboard route
```

---

## Phase 0 definition of done

- [ ] All containers start with docker-compose up — zero errors
- [ ] Local LLM responds within 10 seconds on target hardware
- [ ] StorageAdapter: read/write/file ops verified for Local and OneDrive
- [ ] EncryptedStorageAdapter: field-level encrypt/decrypt round-trip verified
- [ ] StorageRouter: health domain always routes to local + encrypted adapter
- [ ] Memory store: fact written and retrieved correctly across both layers
- [ ] ContextCache: write busts cache immediately (write-through verified)
- [ ] DomainMemoryRouter: access denied for health on household device
- [ ] Autonomy engine: levels 0-3 and NEVER verified by tests
- [ ] Autonomy engine: NEVER violation logs security alert after 3 hits/hour
- [ ] ActionExecutorRegistry: two action types routing correctly
- [ ] DeadLetterQueue: failed delivery after 3 retries lands in dead letter store
- [ ] Input adapter: Telegram message produces normalised AgentRequest
- [ ] Output adapter: test action writes to Google Calendar successfully
- [ ] Streaming: run_stream() yields TEXT_DELTA and returns final AgentResult
- [ ] Conversation: second turn loads history from session store correctly
- [ ] Voice confirmation: VoiceConfirmationSession resolves correct pending action
- [ ] LLM router: health domain forces HEALTH_DOMAIN regardless of agent TaskType
- [ ] LLM router: DataSanitiser applied before every cloud call
- [ ] Cloudflare tunnel: api.mithra-app.com reachable from outside home network
- [ ] Health dashboard: all services show status, last-run, error count
- [ ] 14 consecutive days stable on deployment machine (Windows laptop)

---

## Code standards

- Type hints on all function signatures
- Docstrings on all classes and public methods
- No raw file paths in domain code — always StorageAdapter
- No direct LLM provider imports — always LLMRouter
- No LangGraph imports outside agent/ directory
- No hardcoded user IDs — always from HouseholdConfig
- Unit tests before moving to next component
- Integration tests against real Docker services, not mocks
- All secrets via environment variables, never hardcoded
- All adapters are async
- Domain code never imports autonomy, routing, or adapter modules

---

## Config files

```
compose.yaml                      all Docker services, ports, volumes
.env                              secrets and environment values
household.yaml                    members, devices, display names, channels
services/litellm/routing.yaml     model routing, fallback, token budgets
services/autonomy/autonomy.yaml   action levels, deadline windows per domain
services/cloudflared/config.yaml  tunnel hostname and service mapping
```

---

## Environment variables (.env — never commit)

```bash
# LLM
ANTHROPIC_API_KEY=

# Encryption
MITHRA_ENCRYPTION_KEY=         # base64-encoded 32-byte key
                               # generate: python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"

# Storage
ONEDRIVE_CLIENT_ID=
ONEDRIVE_CLIENT_SECRET=
ONEDRIVE_TENANT_ID=

# Messaging
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
WHATSAPP_SESSION_PATH=./sessions/whatsapp

# Google
GOOGLE_CALENDAR_CREDENTIALS=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Home Assistant
HA_URL=http://localhost:8123
HA_TOKEN=

# Cloudflare
CLOUDFLARE_TUNNEL_TOKEN=

# App
SECRET_KEY=
ENVIRONMENT=development
LOG_LEVEL=INFO
```

---

## Observability storage domains

```
domain="autonomy"       layer="state"    pending confirmations
domain="autonomy"       layer="history"  action log (append-only)
domain="sessions"       layer="state"    conversation sessions + history
domain="observability"  layer="history"  LLM routing events + dead letters
```

Health dashboard reads from these domains directly. No separate logging infra.

---

## Useful commands

```bash
# Start all services
docker-compose up -d

# Watch logs for a service
docker-compose logs -f agent

# Run unit tests
pytest tests/unit/

# Run integration tests (requires Docker running)
pytest tests/integration/

# Check LiteLLM
curl http://localhost:4000/health

# Check Mithra health dashboard
curl http://localhost:8000/api/v1/health

# Generate encryption key
python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"

# Verify all imports resolve correctly
python -m py_compile mithra/**/*.py

# Check interface compliance (run after any interface change)
pytest tests/unit/ -k "interface"
```

---

## References
- Living spec:     docs/SPEC.md    ← requirements + decisions summary
- Work tracker:    docs/TRACKER.md ← all units, current status, next action
- Requirements:    docs/requirements.pdf (full detail, archive)
- Decisions:       docs/decisions.pdf (full detail, archive)
- Architecture:    docs/architecture_pass1.html

## Tracker instructions
When you complete a unit of work, update docs/TRACKER.md:
  change [ ] or [~] to [x] for the completed item.
When a new architectural decision is made, add it to docs/SPEC.md
  under section 8 (ADRs) with the decision, rationale, and implications.
Update "Current Focus" at the top of TRACKER.md to the next item.
