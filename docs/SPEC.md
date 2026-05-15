# Mithra — Specification Reference

> Condensed reference for requirements and architectural decisions.
> Full detail: docs/requirements.pdf and docs/decisions.pdf
> Last updated: May 2026

---

## 1. What Mithra Is

A self-hosted, voice-first agentic orchestration layer that manages all life
domains for a household. It reasons across 15 life domains, surfaces what
needs attention proactively, and acts through native channels the household
already uses — Google Calendar, WhatsApp, smart speakers — without requiring
a new app or dashboard.

**Core mission:** Reduce cognitive overhead and eliminate per-domain
subscription costs by giving the household a single system that holds full
life context and acts on their behalf.

---

## 2. The 15 Life Domains

| Cluster | Domains |
|---|---|
| People | Family, friends, birthdays, relationships, gift tracking |
| Time | Calendar, deadlines, habits, daily briefing |
| Money | Expenses, budget, bills, subscriptions, receipts |
| Home | Maintenance, appliances, seasonal tasks, repairs |
| Food | Pantry, meal planning, grocery lists, expiry |
| Health | Medications, appointments, family health log |
| Vehicles | Registration, insurance, service history, recalls |
| Documents | IDs, warranties, contracts, insurance policies |
| Shopping | Wishlists, orders, returns, gift ideas |
| Goals | Milestones, habits, streaks, weekly check-ins |
| Travel | Trips, bookings, packing, itineraries |
| Environment | Trash/recycling, energy, sustainability |
| Legal | Licences, government docs, permits, HOA |
| Family & Care | School events, pets, childcare, elder care |
| Digital Life | Password hygiene, subscriptions, account security |

Every domain shares 4 data patterns: **Context** (facts), **Trigger**
(what causes action), **Action** (what the system does), **History**
(what happened).

---

## 3. Design Principles

1. **Native-first** — output to Google Calendar, WhatsApp, speakers. No custom dashboard.
2. **Reasoning over scripting** — LLM-based understanding, not brittle scrapers.
3. **Local-first / privacy** — sensitive data stays on the server. No subscriptions per domain.
4. **Adaptive memory** — seed once, learn continuously.
5. **Extensibility** — new domain = new MCP tool + context doc. No core rewrites.
6. **Hybrid intelligence** — local model for routine, cloud for complex.
7. **Graceful manual touchpoints** — not everything is automated. Surface before acting.

---

## 4. Autonomy Levels

Every action is configured to one of five levels. Per-domain, per-action-type.

| Level | Name | Behaviour |
|---|---|---|
| 0 | OBSERVE | Watch and learn. No output. |
| 1 | SURFACE | Tell user. They decide. |
| 2 | ACT_TELL | Act, then notify. |
| 3 | ACT_SILENT | Act. No notification unless failure. |
| -1 | NEVER | Hard wall. Cannot be configured away. |

**Hard NEVER rules (permanent, non-negotiable):**
- Any financial transaction or payment
- Any social media post
- Health data sent to cloud model
- Health data accessed on a shared device

---

## 5. Data Sensitivity Tiers

| Tier | Domains | Storage | Cloud LLM |
|---|---|---|---|
| 1 | Health | Local only — always | Never |
| 2 | Money, Documents, People | OneDrive (user's own) | Anonymised only |
| 3 | Everything else | OneDrive (user's own) | Fine |

---

## 6. Input Channels

- Voice via Home Assistant Assist + Wyoming (Whisper STT)
- Telegram bot (household ↔ agent)
- Document upload via PWA
- Gmail passive read (receipts, bills, bookings, appointments)
- Google Contacts sync (weekly)
- Google Calendar read (seed + ongoing context)
- OneDrive index (existing documents)

## 7. Output Channels

- Google Calendar (events, reminders)
- WhatsApp via Baileys (social outbound — birthday messages, check-ins)
- Piper TTS → Google Home Mini via HA Google Cast
- Telegram (replies, Level 1 confirmations with inline keyboard)
- Push notifications (urgent alerts)
- Weekly email digest

---

## 8. Architectural Decisions (ADRs)

### OQ-01 — Orchestration Framework
**Decision:** LangGraph behind `AgentInterface` abstraction.
- LangGraph is the first adapter. Strands is the designated second.
- Domain code never imports LangGraph. All calls through `agent/interface.py`.
- MCP tools are the domain plugin contract — framework-agnostic.
- Two-way door: swapping frameworks = new adapter class, zero domain changes.

### OQ-02 — LLM Routing
**Decision:** LiteLLM proxy. Phi-3 Mini local. Claude Sonnet cloud.
- LiteLLM runs as a Docker container (port 4000).
- `local` = Phi-3 Mini via Ollama. Cloud fallback allowed.
- `cloud` = claude-sonnet-4-6. No fallback.
- `local_only` = Phi-3 Mini. NEVER cloud fallback. Health domain only.
- Routing config: `services/litellm/routing.yaml`.
- Token budgets + monthly cloud cap in same file.
- Upgrade path: Mac Mini M4 → replace phi3-mini with qwen3:14b (one YAML line).

### OQ-03 — Messaging Platform
**Decision:** Telegram (household) + Baileys/WhatsApp (social outbound).
- Telegram Bot API: household ↔ agent interaction. Free, zero ToS risk.
- Baileys (whatsapp-mcp-ts): outbound social messages from personal number.
- Both behind `MessagingAdapter` interface. Product migration = adapter swap.
- Multi-user isolation in product phase: per-user Baileys session namespace.

### OQ-04 — Voice Platform & Hardware
**Decision:** Home Assistant Assist + Wyoming pipeline. No Gemini, no Alexa.
- STT: Whisper (wyoming-whisper Docker). TTS: Piper (wyoming-piper Docker).
- Wake word: openWakeWord (wyoming-openwakeword Docker).
- Mithra API gateway is HA's conversation agent.
- Hardware: Samsung fridge screen (HA dashboard) + Android tablet (Fire HD)
  + ESP32-S3-BOX-3 (~$50) + Google Home Mini (TTS speaker via Google Cast).
- Start with HA Companion App on phone to validate pipeline first.

### OQ-05 — Multi-User Voice Identity
**Decision:** Phone = personal user_id. Shared devices = household.
- Personal phone (HA Companion App) → `user_id = person_a or person_b`.
- Shared devices (tablet, ESP32, fridge) → `user_id = household`.
- Health/Goals (PERSONAL/RESTRICTED domains) require personal device.
- Access denied on shared device → "Please ask me that on your phone."
- No speaker recognition needed for Phase 0. Add pyannote.audio later if needed.

### OQ-06 — Data Portability
**Decision:** JSON per domain now. Standard formats at product launch.
- Personal use phase: JSON export per domain (full_export.json).
- Product launch: iCal (Time), OFX (Money), vCard (People).
- `ExportAdapter` per domain: `to_json()` and `to_standard_format()`.
- New format = new adapter method. No core changes.

### OQ-07 — Storage Architecture
**Decision:** StorageAdapter interface. OneDrive primary. Local for health.
- `StorageRouter` enforces: health → `EncryptedStorageAdapter(LocalStorageAdapter())`.
- All other domains → `OneDriveAdapter` (user's own OneDrive = BYOS model).
- Sensitive domains (money, documents, people) → `EncryptedStorageAdapter(OneDriveAdapter())`.
- No domain code ever references a file path directly.
- Upgrade path: `GoogleDriveAdapter`, `S3Adapter` — same interface, on demand.
- Product story: "Your data lives in your OneDrive. Disconnect us — data stays."

---

## 9. Hybrid LLM Routing Table

| Route | Model | Handles | Fallback |
|---|---|---|---|
| `local` | Phi-3 Mini (Ollama) | Structured writes, rule alerts, simple retrieval | Cloud (if config allows) |
| `cloud` | Claude Sonnet | Reasoning, drafting, doc parsing, cross-domain | None |
| `local_only` | Phi-3 Mini (Ollama) | Health domain — ALL health tasks | Never (hard wall) |
| `fallback` | Local → Cloud | Ambiguous voice, low confidence | Automatic via LiteLLM |

**Data sanitiser** applied before every cloud call. Two-pass PII stripping.
**Domain guard** in router validates domain independently of agent TaskType.
Health domain forces `HEALTH_DOMAIN` TaskType regardless of agent input.

---

## 10. Memory Architecture

Each domain maintains 4 layers:

| Layer | Description | Update frequency |
|---|---|---|
| `facts` | Stable truths (birthdays, VINs, policy numbers) | Manual only |
| `state` | Current snapshot (pantry, budget remaining) | Every interaction |
| `history` | Append-only event log | Append only, never modified |
| `preferences` | Learned behaviour (reminder lead time, tone) | Learned over time |

---

## 11. Household Configuration

Defined in `household.yaml`. No hardcoded user IDs anywhere in code.

```yaml
household_id: mithra_household_001
timezone: America/Chicago
members:
  - user_id: person_a
    display_name: Nikitha
    device_ids: [phone_nikitha]
    is_admin: true
    notification_channel: telegram
    calendar_provider: google
  - user_id: person_b
    display_name: Wife
    device_ids: [phone_wife]
    is_admin: false
    notification_channel: telegram
    calendar_provider: google
```

## Household device profile (confirmed May 2026)
- person_a (Nikitha): Android, Google Calendar, Telegram
- person_b (Wife): iPhone, Apple Calendar + Google sync confirmed
  calendar_provider: google — writes appear in Apple Calendar automatically
  notification_channel: telegram
- Shared hardware: Google Home Mini, Samsung Family Hub fridge
- Incoming: Fire HD 8/10 tablet, ESP32-S3-BOX-3
- Single CalendarAdapter (Google API) covers both users ✅
- Single notification channel (Telegram) covers both users ✅
- No APNs required, no iCloud CalDAV adapter needed ✅

---

## 12. Domain Access Map

| Access | Domains |
|---|---|
| SHARED | food, home, environment, shopping, travel, money, documents, people, vehicles, legal, digital |
| PERSONAL | time, goals, family |
| RESTRICTED | health (personal device only — enforced in DomainMemoryRouter) |

---

## 13. Phase 0 Definition of Done

All 22 criteria must be met before any domain work begins:

- [ ] All containers start with `docker-compose up` — zero errors
- [ ] Local LLM responds within 10 seconds on Windows laptop
- [ ] StorageAdapter: read/write/file ops verified for Local and OneDrive
- [ ] AESGCMCipher: encrypt/decrypt roundtrip verified ✅
- [ ] StorageRouter: health always routes to local+encrypted ✅
- [ ] Memory store: fact written/retrieved correctly across both layers
- [ ] ContextCache: write-through invalidation verified
- [ ] DomainMemoryRouter: access denied for health on household device
- [ ] DefaultAutonomyEngine: NEVER→OBSERVE→SURFACE→execute order verified
- [ ] DefaultAutonomyEngine: NEVER >3/hour triggers security alert
- [ ] ActionExecutorRegistry: two action types routing correctly
- [ ] DeadLetterQueue: failed delivery after 3 retries in dead letter store
- [ ] Input adapter: Telegram message → normalised AgentRequest
- [ ] Output adapter: test action writes to Google Calendar
- [ ] Streaming: run_stream() yields TEXT_DELTA, returns AgentResult
- [ ] Conversation: second turn loads history from session store
- [ ] VoiceConfirmationSession: resolves correct pending action on yes/no
- [ ] LLM router: health domain forces HEALTH_DOMAIN regardless of agent
- [ ] LLM router: DataSanitiser applied before every cloud call
- [ ] Cloudflare tunnel: api.mithra-app.com reachable externally
- [ ] Health dashboard: all services show status + last-run + error count
- [ ] 14 consecutive days stable on Windows laptop

---

## 14. System V1 Definition of Done

- [ ] Minimum 5 domains Domain-DoD complete (People, Time, Money, Home, Food)
- [ ] All input channels live and tested
- [ ] All output channels live and tested
- [ ] Hybrid LLM routing operational
- [ ] Daily briefing reliable for 14 consecutive days by 7am
- [ ] Automated backup running, full restore tested
- [ ] 30 consecutive days stable on Windows laptop
- [ ] Both household users using system for real tasks for 2+ weeks
- [ ] Action log accessible, undo tested end-to-end
- [ ] Health dashboard live
- [ ] All NFRs verified
- [ ] New domain addable in under 2 hours, no core code changes
- [ ] System has replaced at least 3 previously used apps/processes

---

## 15. Product Evolution Path

| Stage | Trigger |
|---|---|
| Personal Use | System V1 DoD complete + 30-day stability |
| Open Core | 50+ GitHub stars, 5+ external users |
| Managed Hosted | 10+ households wanting hosted, unit economics validated |
| Platform | Strong community, clear B2B use cases |
