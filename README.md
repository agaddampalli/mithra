# Mithra — Personal Life OS

Mithra is a self-hosted AI assistant that manages your household across 15 life domains. It surfaces what needs attention, drafts messages, updates your calendar, and takes action — all through channels you already use: Telegram, WhatsApp, and smart speakers. No new app. No vendor lock-in. Your data stays on your hardware or your own OneDrive.

---

## What it does

You talk to Mithra via Telegram or by voice (on any Home Assistant-connected speaker). It understands what domain you're in, figures out what you want, and either tells you what it found, asks for confirmation, or acts immediately — depending on the autonomy level you've configured.

**Example interactions:**

- "Remind me to call the vet when I'm near the car" → adds to vehicle domain, schedules reminder
- "Send mum a birthday message on WhatsApp" → drafts it, asks you to confirm before sending
- "What's in the fridge that's about to expire?" → reads food domain, responds via voice
- "How much did we spend on groceries this month?" → reads money domain, never sends data to cloud without anonymisation

---

## The 15 life domains

| Domain | Scope | Privacy tier |
|---|---|---|
| Food | Pantry, meals, expiry | Shared |
| Home | Maintenance, appliances, chores | Shared |
| Environment | Air quality, weather, energy | Shared |
| Shopping | Lists, orders, returns | Shared |
| Travel | Trips, bookings, logistics | Shared |
| Money | Budget, bills, spending | Shared (encrypted, anonymised before cloud LLM) |
| Documents | Receipts, contracts, IDs | Shared (encrypted) |
| People | Contacts, birthdays, relationship notes | Shared (encrypted) |
| Vehicles | Services, MOT, insurance, fuel | Shared |
| Legal | Deadlines, renewals, compliance | Shared |
| Digital | Subscriptions, passwords, accounts | Shared |
| Time | Calendar, scheduling | Personal |
| Goals | Habits, milestones, progress | Personal |
| Family | Family tree, history, traditions | Personal |
| **Health** | Symptoms, meds, appointments | **Restricted — local only, never cloud** |

Shared domains are visible to all household members. Personal domains are tied to the individual device. Health data never leaves your machine — it's hardcoded to local storage with no cloud LLM path.

---

## How a request flows

```
Voice / Telegram / WhatsApp
        ↓
  Input adapter normalises to AgentRequest
        ↓
  DomainMemoryRouter loads ContextSnapshot (top 3 domains, deadlines, preferences)
        ↓
  LangGraph agent reasons over context + MCP domain tools
        ↓
  AutononomyEngine enforces action level (OBSERVE → SURFACE → ACT_TELL → ACT_SILENT)
        ↓
  ActionExecutorRegistry dispatches to output adapters
        ↓
  Response via the same channel the request came in on
```

---

## Autonomy levels

Every action type has a configured level. You can override per domain in `services/autonomy/autonomy.yaml`.

| Level | Behaviour |
|---|---|
| `OBSERVE` (0) | Log the signal, say nothing |
| `SURFACE` (1) | Tell you, you decide — sends a confirmation button |
| `ACT_TELL` (2) | Act immediately, then notify you |
| `ACT_SILENT` (3) | Act silently, notify only on failure |
| `NEVER` (-1) | Hard block — cannot be unlocked at runtime |

Level 1 actions wait up to 24 hours for your confirmation before expiring. All executed actions are logged with undo support where reversible (calendar events, storage records). Irreversible actions (TTS, push) are flagged explicitly.

If a `NEVER`-blocked action is attempted more than 3 times in an hour, a security alert is sent to the household Telegram.

---

## Prerequisites

- **Docker Desktop** (Windows or Mac) — runs everything in containers
- **Home Assistant** — for voice input/output (Wyoming protocol)
- **Telegram Bot** — create one via [@BotFather](https://t.me/BotFather)
- **Microsoft OneDrive** — register an app in Azure to get Graph API credentials (free)
- **Anthropic API key** — for cloud reasoning tasks
- A Cloudflare account (free tier) if you want `assistant.mithra-app.com` accessible outside your home network

For local voice only you can skip Cloudflare. For health and local-only operation you can skip OneDrive.

---

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/agaddampalli/mithra.git
cd mithra
cp .env.example .env
```

Edit `.env` with your credentials. Every variable is documented in `.env.example`.

Generate your encryption key (required — encrypts health data and sensitive domains at rest):

```bash
python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
```

Paste the output as `MITHRA_ENCRYPTION_KEY` in `.env`.

### 2. Configure your household

Edit `household.yaml` to define household members and their devices:

```yaml
members:
  - id: person_a
    display_name: "Alice"
    device_ids:
      - "mobile_alice"          # HA Companion App device ID
    telegram_id: 123456789
    whatsapp_number: "+447700000001"

  - id: person_b
    display_name: "Bob"
    device_ids:
      - "mobile_bob"
    telegram_id: 987654321
    whatsapp_number: "+447700000002"

shared_devices:
  - "tablet_kitchen"
  - "fridge_display"
```

Personal device IDs map to `person_a` or `person_b`. Anything not listed defaults to `household`. No user IDs are hardcoded anywhere in the codebase — this file is the only source.

### 3. Start all services

```bash
docker-compose up -d
```

This starts:

| Service | Port | Purpose |
|---|---|---|
| `agent` | 8000 | FastAPI gateway — main entry point |
| `llm-router` | 4000 | LiteLLM proxy — routes to local or cloud LLM |
| `ollama` | 11434 | Phi-3 Mini — handles simple tasks locally |
| `vector-store` | 8001 | ChromaDB — semantic memory |
| `whatsapp` | — | Baileys Node.js bridge for outbound WhatsApp |

### 4. Connect Telegram

Set your webhook (replace with your actual URL or Cloudflare tunnel hostname):

```bash
curl -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook" \
  -d "url=https://assistant.mithra-app.com/api/v1/webhook/telegram"
```

If you don't have a tunnel yet, the bot auto-falls back to polling mode.

### 5. Connect Home Assistant voice

In Home Assistant, add a custom conversation agent pointing to:

```
http://<your-machine-ip>:8000/api/v1/agent/voice
```

Wyoming STT (Whisper) runs on port 10300. Wyoming TTS (Piper) runs on port 10200. Configure both in your HA `configuration.yaml`.

---

## Verify it's working

```bash
# All services healthy
curl http://localhost:8000/api/v1/health

# LiteLLM proxy reachable
curl http://localhost:4000/health

# Send a test message (replace with your Telegram chat ID)
curl -X POST http://localhost:8000/api/v1/agent/message \
  -H "Content-Type: application/json" \
  -d '{"user_id": "person_a", "device_id": "mobile_alice", "text": "hello"}'
```

---

## Data and privacy

| What | Where | Encrypted |
|---|---|---|
| Health records | Local filesystem only | Yes (AES-256-GCM) |
| Money, Documents, People | Your OneDrive | Yes — field-level at rest, anonymised before any cloud LLM call |
| Everything else | Your OneDrive | No (your own storage) |
| Conversation history | Local SQLite | No |
| LLM routing logs | Local SQLite | No |

Mithra uses a BYOS (bring your own storage) model — all domain data lives in your OneDrive. Mithra holds indexes and caches only. The local SQLite cache has a 30-second TTL and is write-through invalidated on every write.

---

## Development

```bash
# Create venv and install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run unit tests
pytest tests/unit/

# Run integration tests (requires Docker running)
pytest tests/integration/

# Watch logs for the agent service
docker-compose logs -f agent

# Verify all imports resolve
python -m py_compile mithra/**/*.py
```

**Current status:** Phase 0 — base infrastructure complete (216 unit tests passing). No domain logic yet. Domain plugins are added in Phase 1.

See `CLAUDE.md` for the full build order, interface contracts, and architectural decisions.

---

## Project structure

```
mithra/
  compose.yaml              all Docker services
  household.yaml            member and device config
  .env.example              all required environment variables

  mithra/                   main Python package
    agent/                  AgentInterface + LangGraph adapter
    routing/                LLMRouter, DomainMemoryRouter, sessions
    storage/                StorageAdapter, OneDrive, local, encryption
    memory/                 ChromaDB, SQLite, context cache
    autonomy/               engine, executor, action log, dead letter queue
    adapters/               voice (Wyoming), messaging (Telegram, WhatsApp), output (TTS, calendar, push)
    api/                    FastAPI gateway + routes

  domains/                  domain plugins — added in Phase 1+
  services/                 config for litellm, ollama, autonomy, cloudflared
  tests/
    unit/                   216 tests, no Docker required
    integration/            requires Docker
```

---

## Licence

Private project — not licensed for redistribution.
