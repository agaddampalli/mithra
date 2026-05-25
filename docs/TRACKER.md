# Mithra — Work Tracker

> **Living document.** Update status as work completes.
> Claude Code: when you finish a unit, change `[ ]` or `[~]` to `[x]`.
> Last updated: May 2026

## Legend
- `[x]` Done
- `[~]` In progress
- `[ ]` Todo
- `[!]` Blocked (note reason)

## Current Status — May 2026
Stack running on Windows laptop.
Webhook mode active — instant Telegram delivery.
Both household members registered with admin approval flow.
Claude Sonnet responding end to end.
Identity migration complete — household.yaml reduced to single line, all identity
data in SQLite. MemberRegistry live; UserIdentityResolver fully removed.
OAuth token management complete — Nango removed, OAuthManager (Authlib) handles
Google + Microsoft via generic config-driven routes.

**Permanent IDs (never regenerate):**
| Entity | ID |
|---|---|
| Household | `hh_b76fef08` |
| Abhiram | `usr_ba6d9199` |
| Wife | `usr_4d03f6ea` |

## Current Focus
**Phase C: End-to-End OAuth Verification** — connect Google Calendar, verify token stored, test calendar write

---

## Track 1 — Design

- [x] D-01 Problem statement — Systems of Execution vs Systems of Reasoning
- [x] D-02 15 domain taxonomy — all domains defined
- [x] D-03 Functional requirements (FR-*) — cross-cutting + per-domain
- [x] D-04 Non-functional requirements (NFR-*) — 6 categories
- [x] D-05 Definition of Done — Feature / Domain / System levels
- [x] D-06 Autonomy matrix — 4 levels + NEVER hard wall
- [x] D-07 Seed model & onboarding — 6 sources, 5-step flow
- [x] D-08 Hybrid LLM routing table — local/cloud/local-only
- [x] D-09 OQ-01 Orchestration — LangGraph + AgentInterface abstraction
- [x] D-10 OQ-02 LLM routing — LiteLLM + Phi-3 Mini + Claude Sonnet
- [x] D-11 OQ-03 Messaging — Telegram + Baileys/WhatsApp
- [x] D-12 OQ-04 Voice & hardware — HA Assist + Wyoming + devices
- [x] D-13 OQ-05 User identity — phone=personal, shared=household
- [x] D-14 OQ-06 Data portability — JSON now, standards at product
- [x] D-15 OQ-07 Storage — StorageAdapter + OneDrive + BYOS
- [x] D-16 Architecture Pass 1 — 6-layer system diagram
- [x] D-17 StorageAdapter interface contract
- [x] D-18 AgentInterface contract
- [x] D-19 AutononomyEngine contract
- [x] D-20 LLMRouter contract
- [x] D-21 DomainMemoryRouter contract
- [x] D-22 MessagingAdapter contract
- [x] D-23 CLAUDE.md — complete with all contracts and build order
- [ ] D-24 Architecture Pass 2 — component internals per layer
- [ ] D-25 Architecture Pass 3 — 3 end-to-end scenario flows

---

## Track 2 — Phase 0: Base Infrastructure

> Gate: all 22 Phase 0 DoD criteria met + 14-day stability run
> before any domain work begins.

### Group A — Storage Layer

- [x] P0-01 compose.yaml — all 12 services, volumes, networks, healthchecks, env refs; Dockerfile + requirements.txt
- [x] P0-02 .env.example — all variables documented
- [x] P0-03 household.yaml — confirmed May 2026 — google calendar covers both users
- [x] P0-04 StorageAdapter interface — abstract class + StorageRecord
- [x] P0-05 LocalStorageAdapter — SQLite + filesystem, cache injection, file_count health
- [x] P0-06 OneDriveAdapter — Graph API + SQLite cache + write queue + cold-cache fallback, 38 tests
- [x] P0-07 EncryptedStorageAdapter — adapter + 26 integration tests, 242 total passing
- [x] P0-08 StorageRouter — get_adapter() done ✅, health constraint verified

### Group B — Memory Layer

- [x] P0-09 VectorStore (ChromaDB) — 19 tests, client injection, LiteLLM embeddings, cosine similarity
- [x] P0-10 StructuredStore (SQLite) — 38 tests, facts/state/history/prefs, list_*/delete_*/health()
- [x] P0-11 ContextCache — 33 tests, pickle serialisation, LIKE invalidation, health()

### Group C — Routing Layer

- [x] P0-12 DomainMemoryRouter — DefaultDomainMemoryRouter impl, 57 tests (access control, context assembly, cache invalidation, PII extraction)
- [x] P0-13 ConversationSessionManager — TTL expiry, MAX_TURNS cap, known_user_ids, 28 tests (23 new)
- [x] P0-14 DataSanitiser — two-pass PII stripping, 36 tests (6 scaffold + 30 new)

### Group D — Agent Layer

- [x] P0-15 AgentInterface — abstract class + enums + dataclasses, 18 tests
- [x] P0-16 LangGraphAdapter — 4-node StateGraph (classify_intent→select_tool→execute_tool→generate_response), streaming, 22 tests
- [x] P0-17 LLMRouter + LiteLLMAdapter — LiteLLMRouterAdapter impl, 38 tests (routing, health guard, sanitisation, fallback, observability, classify_intent, get_usage)

### Group E — Autonomy Layer

- [x] P0-18 AutononomyEngine (abstract) — base class + dataclasses, 9 tests
- [x] P0-19 DefaultAutonomyEngine — all levels, confirm/reject/undo/expire_pending, 29 tests (17 existing + 12 new)
- [x] P0-20 ActionExecutorRegistry — execute/undo/notify/register_notify, 25 tests (18 existing + 7 new)
- [x] P0-21 ActionLog — append/get/list_recent, 8 tests
- [x] P0-22 DeadLetterQueue — enqueue/resolve/list_unresolved/health, 13 tests

### Group F — Messaging & Output Adapters

- [x] P0-23 adapters/messaging/__init__ — base types, 24 tests passing
- [x] P0-24 TelegramInputAdapter — MemberRegistry (replaced UserIdentityResolver), 4 message types, session history, admin approval flow, _seed_admin_channel bootstrap
- [x] P0-25 TelegramOutputAdapter — rate limiting, inline keyboard, register_chat, undo, 7 tests
- [x] P0-26 WhatsAppAdapter — Baileys bridge via HTTP, register_phone, numbered actions, 12 tests
  - whatsapp-bridge/index.js created — scan QR on first start
- [x] P0-27 CalendarAdapter — http_client injection, send() JSON parsing, undo, 7 tests
- [x] P0-28 TTSAdapter — Piper via HA + VoiceConfirmationSession, speak/stream/send_with_actions, 28 tests
- [x] P0-29 PushAdapter — Web Push VAPID, lazy pywebpush import, graceful degradation, 10 tests
- [x] P0-30 RetryableOutputAdapter — 3 attempts + exponential backoff + dead letter on exhaustion, 9 tests
- [x] P0-31 WyomingAdapter — Wyoming protocol, member_registry (device channel resolution), confirmation resolution, 18 tests

### Group G — API Gateway

- [x] P0-32 FastAPI gateway — lifespan startup, /api/v1/health + /agent + /webhook/telegram, auth middleware, 20 tests (P0-33/34/36 complete)
- [ ] P0-35 Route /api/v1/domains — domain status and management

### Group A-Ident — Identity Migration (Phase A)

- [x] A1 — New SQLite identity tables created (households, members, member_channels, member_preferences, household_config)
- [x] A1 — Migration script ran successfully (scripts/migrate_identity.py)
- [x] A2 — MemberRegistry implemented (mithra/identity/member_registry.py)
- [x] A3 — UserIdentityResolver removed; all callers migrated to MemberRegistry
- [x] A3 — Telegram verified end to end with new IDs — user=usr_ba6d9199 confirmed in logs
- [x] A3 — household.yaml reduced to single line: `household_id: hh_b76fef08`

### Group B-OAuth — Config-Driven OAuth (Phase B)

- [x] B1 — oauth_providers.py: OAUTH_PROVIDERS dict + _resolve_url() + get_configured_providers()
- [x] B2 — oauth_token_store.py: encrypted token storage via StorageAdapter (domain="oauth")
- [x] B3 — oauth_manager.py: generic Authlib OAuth2 orchestration, token refresh with 5-min buffer
- [x] B4 — gateway.py: generic GET /auth/{provider} + GET /auth/{provider}/callback routes
- [x] B5 — bootstrap.py: _build_oauth_manager() wired after storage, before calendar
- [x] B6 — calendar.py: oauth_manager.get_token("google", user_id) replaces Nango
- [x] B7 — telegram_handler.py: oauth_manager.is_connected() replaces Nango in _handle_integration
- [x] B8 — Nango removed: nango_client.py, google_oauth.py, oauth_proxy.py, auth.py deleted; compose.yaml cleaned
- [x] B9 — Redirect URI comment: https://assistant.mithra-app.com/auth/google/callback
- [x] B-tests — 54 new unit tests (providers, token_store, oauth_manager); 753 total passing

### Group H — Home Assistant Setup

- [ ] HA-01 HA Core Docker — add to compose.yaml, port 8123
- [ ] HA-02 HA first-run — onboarding via browser, create admin account
- [ ] HA-03 HA long-lived token — save to .env as HA_TOKEN
- [ ] HA-04 Wyoming integration — add Whisper (10300) + Piper (10200) in HA
- [ ] HA-05 openWakeWord — add port 10400, select wake word
- [ ] HA-06 Assist pipeline — STT=Whisper, TTS=Piper, WW=openWakeWord
- [ ] HA-07 Mithra as conversation agent — configure HA to call Mithra API
- [ ] HA-08 Google Cast integration — discover Google Home Mini
- [ ] HA-09 Samsung Family Hub — SmartThings integration, expose cameras
- [ ] HA-10 HA Companion App — Person A's phone
- [ ] HA-11 HA Companion App — Person B's phone
- [ ] HA-12 Voice test via phone — Assist → command → Mithra → response

### Group I — Wyoming Voice Pipeline

- [ ] WY-01 wyoming-whisper — add to compose.yaml, tiny-int8 model, port 10300 (correct image: rhasspy/wyoming-whisper, not wyoming-faster-whisper)
- [ ] WY-02 wyoming-piper — add to compose.yaml, en_US-lessac-medium, port 10200
- [ ] WY-03 wyoming-openwakeword — add to compose.yaml, ok_nabu, port 10400
- [ ] WY-04 Wire all three to compose.yaml with volumes
- [ ] WY-05 Test Whisper STT — curl WAV file → verify transcript
- [ ] WY-06 Test Piper TTS — curl text → verify audio returned
- [ ] WY-07 Test openWakeWord — speak wake word, verify HA detects
- [ ] WY-08 Full pipeline test — wake → STT → Mithra → TTS → speaker
- [ ] WY-09 Upgrade path note — document Mac Mini → Whisper base/small model

### Group J — OAuth & Credentials

- [x] OA-01 Anthropic API key — console.anthropic.com
- [ ] OA-02 Encryption key — generate 32-byte key, save as MITHRA_ENCRYPTION_KEY
- [ ] OA-03 Google Cloud project — create, enable Calendar + Gmail + People APIs
- [ ] OA-04 Google OAuth credentials — create client ID, download JSON
- [ ] OA-05 Google Calendar auth — authorize, test create event via API
- [ ] OA-06 Google Gmail auth — authorize readonly, test list emails
- [ ] OA-07 Google Contacts auth — authorize read, test list contacts
- [ ] OA-08 Microsoft Azure app — register for OneDrive Graph API
- [ ] OA-09 OneDrive auth — authorize, test create /PLOS/ folder
- [x] OA-10 Telegram bot — @BotFather → /newbot, save BOT_TOKEN
- [x] OA-11 Telegram test — send message, verify round-trip
- [ ] OA-12 Baileys WhatsApp — start container, scan QR with Person A's phone
- [ ] OA-13 WhatsApp test — send message to external contact
- [ ] OA-14 Cloudflare tunnel token — create tunnel, save token

### Group K — Cloudflare & Networking

- [x] CF-01 cloudflared — add to compose.yaml
- [x] CF-02 Tunnel route — assistant.mithra-app.com → api-gateway:8000
- [ ] CF-03 Cloudflare Access — Zero Trust policy for assistant.mithra-app.com
- [x] CF-04 Verify external access — confirmed from phone off home Wi-Fi
- [x] CF-05 Telegram webhook — register setWebhook against tunnel URL
- [x] CF-06 Webhook test — instant delivery confirmed end-to-end
- [ ] CF-07 ha.mithra-app.com — optional HA remote access route

### Group L — Hardware Setup

- [ ] HW-01 ESP32-S3-BOX-3 — purchase (~$50)
- [ ] HW-02 ESP32-S3-BOX-3 — flash ESPHome firmware via Chrome + USB-C
- [ ] HW-03 ESP32-S3-BOX-3 — connect to 2.4GHz Wi-Fi
- [ ] HW-04 ESP32-S3-BOX-3 — HA integration via ESPHome
- [ ] HW-05 ESP32-S3-BOX-3 — configure wake word (microWakeWord)
- [ ] HW-06 ESP32-S3-BOX-3 — full pipeline test end-to-end
- [ ] HW-07 Fire tablet — purchase (Fire HD 8 ~$100 or Fire HD 10 ~$130)
- [ ] HW-08 Fire tablet — sideload Google Play Store (4 APKs via xda-developers)
- [ ] HW-09 Fire tablet — install DashVoice, configure HA URL
- [ ] HW-10 Fire tablet — create Lovelace dashboard for tablet resolution
- [ ] HW-11 Fire tablet — kiosk mode: auto-start, always-on, no nav bar
- [ ] HW-12 Fire tablet — voice test end-to-end
- [ ] HW-13 Fire tablet — wall mount or stand + charging cable
- [ ] HW-14 Samsung fridge — SmartThings → HA integration
- [ ] HW-15 Samsung fridge — verify internal camera as HA entity
- [ ] HW-16 Samsung fridge — load HA dashboard in fridge browser
- [ ] HW-17 Samsung fridge — camera → Food domain pantry detection test
- [ ] HW-18 Google Home Mini — Google Cast integration in HA
- [ ] HW-19 Google Home Mini — TTS playback test via HA
- [ ] HW-20 Full room test — all devices active, no conflicts

### Group M — Deployment to Windows Laptop

- [x] DP-00 GitHub Actions build pipeline — tests run on PR, image pushed to ghcr.io on merge to main; GHA cache, metadata-action tags, Watchtower polls every hour (label-enable)
- [x] DP-01 Docker Desktop — install on Windows, enable WSL2
- [x] DP-02 Git — install on Windows, configure SSH for GitHub
- [x] DP-03 Clone repo on Windows laptop
- [x] DP-04 Create .env on Windows with all credentials
- [x] DP-05 First docker-compose up — all containers start
- [x] DP-06 Watchtower — add to compose.yaml, auto-update on push
- [ ] DP-07 Windows sleep settings — never sleep
- [ ] DP-08 Startup on boot — Docker Desktop + compose stack
- [x] DP-09 Health check — curl assistant.mithra-app.com/api/v1/health from Mac
- [x] DP-10 Full smoke test on laptop — Telegram round-trip

### Group N — End-to-End Integration Tests

- [ ] ET-01 Telegram text → agent → Telegram reply
- [ ] ET-02 Voice (phone) → HA → Mithra → TTS response
- [ ] ET-03 Voice (ESP32) → wake → STT → Mithra → TTS → speaker
- [ ] ET-04 Voice (tablet) → DashVoice → Mithra → spoken response
- [ ] ET-05 Document upload → OCR → domain update
- [ ] ET-06 Gmail passive read → expense → Money domain
- [ ] ET-07 Calendar write — event appears in Google Calendar on phone
- [ ] ET-08 WhatsApp outbound — message delivered from personal number
- [ ] ET-09 Health on shared device → PERMISSION_DENIED response
- [ ] ET-10 Health on personal phone → correct response
- [ ] ET-11 Level 1 confirmation via Telegram — inline keyboard → confirm
- [ ] ET-12 Level 1 confirmation via voice — spoken prompt → "yes"
- [ ] ET-13 Undo test — Level 2 action → undo within 60 minutes
- [ ] ET-14 Daily briefing at 7am — delivered via configured channel
- [ ] ET-15 14-day stability run — no manual intervention

### Phase 0 — Definition of Done Gate

- [ ] All containers start with `docker-compose up` — zero errors
- [ ] Local LLM responds within 10 seconds on Windows laptop
- [ ] StorageAdapter: read/write/file ops verified for Local and OneDrive
- [x] AESGCMCipher: encrypt/decrypt roundtrip verified
- [x] StorageRouter: health always routes to local+encrypted
- [ ] Memory store: fact written/retrieved correctly across both layers
- [ ] ContextCache: write-through invalidation verified
- [ ] DomainMemoryRouter: access denied for health on household device
- [~] DefaultAutonomyEngine: NEVER→OBSERVE→SURFACE→execute verified
- [ ] DefaultAutonomyEngine: NEVER >3/hour triggers security alert
- [ ] ActionExecutorRegistry: two action types routing correctly
- [ ] DeadLetterQueue: 3 retries exhausted → dead letter store
- [x] Telegram input → normalised AgentRequest
- [ ] Calendar write → event appears in Google Calendar
- [x] run_stream() yields TEXT_DELTA → returns AgentResult
- [ ] Second conversation turn loads history from session store
- [ ] VoiceConfirmationSession resolves correct pending action
- [ ] LLM router: health forces HEALTH_DOMAIN regardless of agent
- [ ] LLM router: DataSanitiser applied before every cloud call
- [x] assistant.mithra-app.com reachable from outside home network
- [ ] Health dashboard: all services show status + last-run + errors
- [ ] 14 consecutive days stable on Windows laptop

---

## Track 3 — Phase 1: People, Time, Money

> Blocked until Phase 0 DoD complete.

- [ ] PH1-00 Open source research — CRM tools, receipt parsers, budget trackers, calendar NLP
- [ ] PH1-01 People domain
- [ ] PH1-02 Time domain
- [ ] PH1-03 Money domain
- [ ] PH1-04 Google Contacts seed
- [ ] PH1-05 Gmail passive read
- [ ] PH1-06 Daily briefing
- [ ] PH1-07 Phase 1 integration tests + 2-week real use

---

## Track 4 — Phase 2: Home, Food

> Blocked until Phase 0 DoD complete.

- [ ] PH2-00 Open source research — home maintenance trackers, pantry managers, recipe engines
- [ ] PH2-01 Home domain
- [ ] PH2-02 Food domain
- [ ] PH2-03 Samsung fridge camera → Food domain
- [ ] PH2-04 Phase 2 integration tests + 2-week real use

---

## Track 5 — Phase 3: Documents, Vehicles, Health, Goals

> Blocked until Phase 0 DoD complete.

- [ ] PH3-00 Open source research — OCR extractors, vehicle trackers, medication tools, habit trackers (health: local-only tools only)
- [ ] PH3-01 Documents domain
- [ ] PH3-02 Vehicles domain
- [ ] PH3-03 Health domain (local model only, never cloud)
- [ ] PH3-04 Goals domain
- [ ] PH3-05 OneDrive existing document seed
- [ ] PH3-06 Phase 3 integration tests + 2-week real use

---

## Track 6 — Phase 4: Shopping, Travel, Environment, Legal

> Blocked until Phase 0 DoD complete.

- [ ] PH4-00 Open source research — wishlist trackers, travel parsers, municipal scrapers, LLM-based web tools
- [ ] PH4-01 Shopping domain
- [ ] PH4-02 Travel domain
- [ ] PH4-03 Environment domain (trash + holiday shift detection)
- [ ] PH4-04 Legal & Admin domain
- [ ] PH4-05 Phase 4 integration tests + 2-week real use

---

## Track 7 — Phase 5: Family & Care, Digital Life + V1 Sign-off

> Blocked until Phase 0 DoD complete.

- [ ] PH5-00 Open source research — school calendars, pet care, password audit, subscription monitors. Also: retrospective review of Phases 1-4.
- [ ] PH5-01 Family & Care domain
- [ ] PH5-02 Digital Life domain
- [ ] PH5-03 System V1 DoD — all 13 criteria verified by both users
- [ ] PH5-04 30-day stability run on Windows laptop
- [ ] PH5-05 Product readiness review

---

## Track 8 — Product Evolution

- [~] PR-01 Personal Use — household, self-hosted, all 15 domains
- [ ] PR-02 Open Core — open source, Docker setup, community plugins
- [ ] PR-03 Managed Hosted — one-click deploy, BYOS, subscription
- [ ] PR-04 Platform — third-party plugins, white-label, marketplace

---

## Test Suite Status

| File | Tests | Status |
|---|---|---|
| routing/test_llm_router.py | 25 | ✅ passing |
| adapters/test_messaging.py | 24 | ✅ passing |
| routing/test_domain_router.py | 23 | ✅ passing |
| autonomy/test_executor.py | 18 | ✅ passing |
| agent/test_interface.py | 18 | ✅ passing |
| adapters/test_tts.py | 18 | ✅ passing |
| autonomy/test_default_engine.py | 17 | ✅ passing |
| memory/test_context_cache.py | 16 | ✅ passing |
| adapters/test_voice.py | 13 | ✅ passing |
| test_storage_encryption.py | 10 | ✅ passing |
| test_storage_local.py | 9 | ✅ passing |
| test_autonomy.py | 9 | ✅ passing |
| test_sanitiser.py | 6 | ✅ passing |
| test_storage_router.py | 5 | ✅ passing |
| test_session.py | 5 | ✅ passing |
| memory/test_vector_store.py | 19 | ✅ passing |
| memory/test_structured_store.py | 48 | ✅ passing |
| memory/test_context_cache.py | 33 | ✅ passing |
| routing/test_domain_memory_router.py | 57 | ✅ passing |
| routing/test_session.py | 23 | ✅ passing |
| adapters/test_telegram_impl.py | ~130 | ✅ passing (MemberRegistry) |
| identity/test_member_registry.py | — | covered in test_telegram_impl |
| **Total** | **699** | **✅ all passing** |
