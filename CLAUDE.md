# Mithra — Personal Life OS

## What this project is
Mithra is a self-hosted, voice-first agentic orchestration layer that manages
all life domains for a household. It reasons across 15 life domains, surfaces
what needs attention proactively, and acts through native channels the
household already uses — Google Calendar, WhatsApp, smart speakers — without
requiring a new app or dashboard.

Full requirements and architectural decisions are in docs/. Read those first
if you need deep context. This file gives Claude Code everything needed to
build correctly without re-reading the PDFs every session.

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

### LLM routing
- Proxy: LiteLLM (runs as a Docker container)
- Local: Ollama with phi3-mini — handles simple structured tasks
- Cloud: Anthropic claude-sonnet-4-6 — handles reasoning, drafting, parsing
- Health domain: local model ONLY, hardcoded — never cloud
- All routing defined in services/litellm/config.yaml
- Agent code calls LiteLLM endpoint exclusively, never model providers directly

### Messaging
- Household ↔ agent: Telegram Bot API (python-telegram-bot)
- Social outbound (birthday messages etc): WhatsApp via Baileys (Node.js)
- Both sit behind a MessagingAdapter interface in adapters/messaging/

### Voice
- Platform: Home Assistant Assist pipeline
- STT: Whisper via Wyoming protocol
- TTS: Piper via Wyoming protocol
- Wake word: openWakeWord
- PLOS API gateway is HA's conversation agent
- No Gemini, no Alexa — both are closed platforms

### User identity
- Personal phone (HA Companion App) → user_id = person_a or person_b
- Shared devices (tablet, ESP32, fridge) → user_id = household
- DomainMemoryRouter in routing/domain_router.py handles the split
- Private domains (Health, Goals) → personal context only
- Shared domains (Food, Home, Money, People) → household context

### Storage
- Interface: StorageAdapter in storage/adapter.py
- Primary: OneDriveAdapter (Microsoft Graph API + SQLite cache)
- Exception: Health domain → LocalStorageAdapter ALWAYS, hardcoded
- StorageRouter in storage/router.py enforces the health exception
- No domain module ever references a file path directly
- BYOS model: user data lives in their own OneDrive, Mithra holds indexes only

### Data portability
- JSON per domain now (personal use phase)
- iCal, OFX, vCard added before first external product user

### Deployment
- Single Docker Compose stack — one file starts everything
- Target: Windows laptop (development on Mac, deploy via git pull)
- Watchtower for automatic container updates
- Cloudflare tunnel → api.mithra-app.com (no port forwarding needed)

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
  compose.yaml                  single file, all services
  .env.example                  all required env vars documented
  .env                          never committed
  .gitignore

  docs/
    requirements.pdf
    decisions.pdf
    architecture_pass1.html

  mithra/                       main Python package
    __init__.py
    agent/
      __init__.py
      interface.py              AgentInterface abstract class
      langgraph_adapter.py      LangGraph implementation of AgentInterface
    routing/
      __init__.py
      llm_router.py             LiteLLM config and routing helpers
      domain_router.py          user_id → shared/private memory routing
    storage/
      __init__.py
      adapter.py                StorageAdapter abstract class
      router.py                 StorageRouter — enforces health exception
      onedrive.py               OneDriveAdapter implementation
      local.py                  LocalStorageAdapter implementation
    memory/
      __init__.py
      vector_store.py           ChromaDB wrapper
      structured_store.py       SQLite wrapper (facts, state, history, prefs)
    autonomy/
      __init__.py
      engine.py                 autonomy level enforcement (0-3 + NEVER)
      action_log.py             append-only action history store
    adapters/
      __init__.py
      voice/
        __init__.py
        wyoming.py              Wyoming protocol voice input adapter
      messaging/
        __init__.py
        telegram.py             TelegramAdapter
        whatsapp.py             WhatsAppAdapter (Baileys bridge)
      output/
        __init__.py
        calendar.py             Google Calendar API output
        tts.py                  Piper TTS via HA Google Cast
        push.py                 PWA push notifications
    api/
      __init__.py
      gateway.py                FastAPI app — all routes, auth middleware
      routes/
        __init__.py
        agent.py                /api/v1/agent/* routes
        health.py               /api/v1/health/* routes
        domains.py              /api/v1/domains/* routes

  services/
    litellm/
      config.yaml               model routing config
    ollama/
      Dockerfile                pulls phi3-mini on build
    cloudflared/
      config.yaml               tunnel config for api.mithra-app.com

  domains/                      domain plugins (added in Phase 1+)
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

## Phase 0 build order (strict — build in this sequence)

Each component must have passing tests before moving to the next.
Do not skip ahead. Each item is a dependency for the next.

```
 1. compose.yaml                all services defined, images named
 2. .env.example                all required variables documented
 3. storage/adapter.py          StorageAdapter abstract interface
 4. storage/local.py            LocalStorageAdapter (SQLite + filesystem)
 5. storage/onedrive.py         OneDriveAdapter (Graph API + SQLite cache)
 6. storage/router.py           StorageRouter — health domain hardcoded local
 7. memory/vector_store.py      ChromaDB wrapper with standard read/write API
 8. memory/structured_store.py  SQLite wrapper (facts, state, history, prefs)
 9. routing/domain_router.py    user_id → shared/private context routing
10. agent/interface.py          AgentInterface abstract class
11. agent/langgraph_adapter.py  LangGraph implementation
12. routing/llm_router.py       LiteLLM config helpers and routing logic
13. autonomy/engine.py          levels 0-3 and NEVER enforced, configurable
14. autonomy/action_log.py      append-only action history (SQLite backed)
15. adapters/messaging/telegram.py    TelegramAdapter
16. adapters/messaging/whatsapp.py    WhatsAppAdapter (Baileys bridge)
17. adapters/output/calendar.py       Google Calendar API write
18. adapters/output/tts.py            Piper TTS via Home Assistant
19. adapters/voice/wyoming.py         Wyoming protocol voice input
20. api/gateway.py                    FastAPI gateway, auth, all routes wired
21. services/cloudflared/config.yaml  tunnel to api.mithra-app.com
22. Dashboard route                   /api/v1/health with full system status
```

---

## Phase 0 definition of done

Before any domain work starts, every item below must be true:

- [ ] All containers start with `docker-compose up` with zero errors
- [ ] Local LLM responds within 10 seconds on target hardware
- [ ] StorageAdapter: read/write verified for both Local and OneDrive
- [ ] StorageRouter: health domain always routes to LocalStorageAdapter
- [ ] Memory store: fact written and retrieved correctly across both layers
- [ ] Autonomy engine: levels 0, 1, 2, 3 and NEVER all verified by tests
- [ ] Input adapter: Telegram message produces a normalised AgentRequest
- [ ] Output adapter: test action successfully writes to Google Calendar
- [ ] Cloudflare tunnel: api.mithra-app.com reachable from outside home network
- [ ] Health dashboard: all services show status and last-run timestamp
- [ ] 14 consecutive days stable on deployment machine (Windows laptop)

---

## Code standards

- Type hints on all function signatures
- Docstrings on all classes and public methods
- No raw file paths in domain code — always through StorageAdapter
- No direct LLM provider imports — always through LiteLLM endpoint
- No LangGraph imports outside agent/ directory
- Every new component gets unit tests before next component starts
- Integration tests run against real Docker services, not mocks
- All secrets via environment variables, never hardcoded

---

## Environment variables (never commit .env)

```bash
# LLM
ANTHROPIC_API_KEY=

# Storage
ONEDRIVE_CLIENT_ID=
ONEDRIVE_CLIENT_SECRET=
ONEDRIVE_TENANT_ID=

# Messaging
TELEGRAM_BOT_TOKEN=
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

## Useful commands

```bash
# Start all services
docker-compose up -d

# Watch logs
docker-compose logs -f agent

# Run tests
pytest tests/unit/
pytest tests/integration/

# Check LiteLLM routing
curl http://localhost:4000/health

# Check Mithra health
curl http://localhost:8000/api/v1/health
```

---

## References
- Full requirements: docs/requirements.pdf
- All 7 architectural decisions: docs/decisions.pdf
- System architecture diagram: docs/architecture_pass1.html
- Mithra domain: mithra-app.com (Cloudflare)
- Deployment target: Windows laptop running Docker + Watchtower
