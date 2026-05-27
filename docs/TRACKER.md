# Mithra — Work Tracker (Updated 2026-05-25)

## Current IDs (generated 2026-05-24, never change)
  household_id: hh_b76fef08
  Abhiram:      usr_ba6d9199  (owner)
  Wife:         usr_4d03f6ea  (member)

## Stack Status
  api-gateway:    running, healthy, port 9000
  llm-router:     running, port 4000
  vector-store:   running, port 8001
  cloudflared:    running (assistant.mithra-app.com)
  healthmonitor:  running (Telegram alerts)

## What's Complete

### Design Track (T1)
  [x] D-01 to D-23 — all design complete
  [x] ARCHITECTURE_DECISIONS.md (6 sections)
      Section 1: Identity & Household Management
      Section 2: OAuth Token Management
      Section 3: File Storage and Document RAG
      Section 4: Document Interaction UX
      Section 5: Document Processing Libraries (Docling)
      Section 6: Phase 1 Storage Infrastructure
  [ ] D-24 Architecture Pass 2 (component diagrams)
  [ ] D-25 Architecture Pass 3 (end-to-end scenarios)

### Phase 0 Infrastructure (T2)

#### Group A — Storage Layer
  [x] P0-01 compose.yaml (Mithra stack running)
  [x] P0-02 .env configured
  [x] P0-03 household.yaml (single line: household_id)
  [x] P0-04 StorageAdapter interface
  [x] P0-05 LocalStorageAdapter (SQLite + filesystem)
  [x] P0-06 OneDriveAdapter — DEFERRED
              (File-only via FileStorageAdapter instead)
  [x] P0-07 EncryptedStorageAdapter (AESGCMCipher verified)
  [x] P0-08 StorageRouter (health→local hardcoded, verified)

#### Group B — Memory Layer
  [x] P0-09 VectorStore (ChromaDB, running port 8001)
  [x] P0-10 StructuredStore (SQLite)
  [x] P0-11 ContextCache (write-through invalidation)

#### Group C — Routing Layer
  [x] P0-12 DomainMemoryRouter (assemble_context)
  [x] P0-13 ConversationSessionManager
  [x] P0-14 DataSanitiser (two-pass PII stripping)

#### Group D — Agent Layer
  [x] P0-15 AgentInterface (AgentIntent/AgentErrorCode enums)
  [x] P0-16 LangGraphAdapter (graph + tool calling + streaming)
  [x] P0-17 LLMRouter (TaskType routing, LiteLLM, verified)

#### Group E — Autonomy Layer
  [x] P0-18 AutononomyEngine abstract base
  [x] P0-19 DefaultAutonomyEngine
              (NEVER→OBSERVE→SURFACE→execute order)
  [x] P0-20 ActionExecutorRegistry
  [x] P0-21 ActionLog (append-only)
  [x] P0-22 DeadLetterQueue

#### Group F — Adapters
  [x] P0-23 adapters/messaging base types
  [x] P0-24 TelegramInputAdapter (webhook, working)
  [x] P0-25 TelegramOutputAdapter (streaming, rate limiting)
  [x] P0-26 WhatsAppAdapter (Baileys bridge, untested)
  [x] P0-27 CalendarAdapter (Google Calendar write, verified)
  [x] P0-28 TTSAdapter (stub, awaiting hardware)
  [x] P0-29 PushAdapter (stub)
  [x] P0-30 RetryableOutputAdapter
  [x] P0-31 WyomingAdapter (stub, awaiting hardware)

#### Group G — API Gateway
  [x] P0-32 FastAPI gateway (running, all routes)
  [x] P0-33 /api/v1/agent (Telegram → agent → response)
  [x] P0-34 /api/v1/health
  [x] P0-36 /api/v1/webhook/telegram

#### Identity (completed this session)
  [x] MemberRegistry replaces UserIdentityResolver
  [x] households, members, member_channels,
      member_preferences, household_config tables
  [x] Migration: person_a→usr_ba6d9199, person_b→usr_4d03f6ea
  [x] Telegram verified: user=usr_ba6d9199 in logs

#### OAuth (completed this session)
  [x] Config-driven OAuthManager (no Nango)
  [x] OAuthTokenStore (encrypted, StorageAdapter)
  [x] Google OAuth working end-to-end
  [x] Calendar write verified (POST /calendar → Google)
  [x] Nango removed entirely from stack
  [ ] Microsoft OAuth (code ready, needs Azure registration)

#### Stability (completed this session)
  [x] DP-07 Windows never sleep + lid close keeps running
  [x] DP-08 Docker Desktop auto-start on login
  [x] DP-09 Health monitoring (Telegram alerts)
  [x] Log rotation (max 10MB × 3 per service)
  [x] Watchtower conflict resolved

#### Phase 0 DoD Gate
  [x] All containers start — zero errors
  [x] AESGCMCipher encrypt/decrypt verified
  [x] StorageRouter health→local hardcoded, verified
  [x] Telegram round trip verified
  [x] Google Calendar write verified
  [ ] Local LLM (Phi-3 Mini) not yet configured
  [ ] 14-day stability run (starting now)

## Phase 1 — Storage Foundation (IMPLEMENTED)

  Architecture documented in ARCHITECTURE_DECISIONS.md section 6.

  [x] P1-01 files table in SQLite
  [x] P1-02 FileStorageAdapter abstract base
  [x] P1-03 LocalFileAdapter (/data/mithra/files/)
  [x] P1-04 MicrosoftGraphClient (Graph API wrapper)
  [x] P1-05 OneDriveFileAdapter
  [x] P1-06 FileStorageRouter (local + OneDrive)
  [x] P1-07 DocumentIndexer (Docling extraction + ChromaDB)
  [x] P1-08 FileSearchService (tiered retrieval)
  [x] P1-09 Wire into bootstrap
  [x] P1-10 SQLite weekly backup job

## Phase 1 — Domains (PENDING Phase 1 storage)

  Pending: domain plugin architecture decision first.
  Then Time domain (calendar read), People, Documents.

## Pending Hardware (blocked until hardware arrives)
  [ ] HA-01 to HA-12 (Home Assistant setup)
  [ ] WY-01 to WY-09 (Wyoming voice pipeline)
  [ ] HW-01 to HW-06 (ESP32-S3-BOX-3)
  [ ] HW-07 to HW-13 (Fire tablet)
  [ ] HW-14 to HW-20 (Samsung fridge, Google Home Mini)

## End-to-End Tests (pending domains)
  [ ] ET-01 to ET-15

## Current Focus
  7-day stability run (started 2026-05-25)
  Daily: send "hi" to bot, verify response
  Next: Phase 1 Domains — Time (calendar read), People, Documents

## Test Baseline
  784 tests passing (2026-05-26)
