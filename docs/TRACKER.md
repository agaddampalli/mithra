# Mithra — Work Tracker (Updated 2026-06-06)

## SESSION HANDOFF (update after each session)

Last updated: 2026-06-06

### Current State
  Stack: Docker Compose on Windows laptop
  Household: hh_b76fef08
  Owner: usr_ba6d9199 (Abhiram)
  Wife: usr_4d03f6ea
  Tunnel: assistant.mithra-app.com
  Timezone: America/Chicago

### Completed
  Phase 1 storage: COMPLETE
  Time domain: COMPLETE (all 8 tools verified)
  Domain plugin architecture: COMPLETE
  798 tests passing

### In Progress
  Tasks domain: ADR section 11 complete, next T1 schema

### Immediate Next Steps
  1. T1 schema — tasks, projects, milestones, routines tables
  2. T2–T6 tools (13 total)
  3. T7–T9 domain class + API routes
  4. T10 DoD verification
  5. HA setup (after Tasks domain)

### Key Decisions Made
  - Mithra SQLite as source of truth (not Google Tasks)
  - Tasks domain separate from Time domain
  - Three entity types: tasks, projects+milestones, routines
  - Routines = daily-reset checklists (separate from tasks)
  - Priority: urgent|high|normal|someday
  - Capture: ask questions to make actionable
  - Baby routines = household scope, both mark done
  - No external task app (Vikunja rejected — wrong use case)
  - Calendar blocking opt-in not automatic
  - HA tablet = primary display (Scenario B)

### Known Open Issues
  - llm-router health check flapping (low priority)
  - Docker cleanup automation (deferred to phase close)

---

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
  [x] ARCHITECTURE_DECISIONS.md (10 sections)
      Section 1: Identity & Household Management
      Section 2: OAuth Token Management
      Section 3: File Storage and Document RAG
      Section 4: Document Interaction UX
      Section 5: Document Processing Libraries (Kreuzberg)
      Section 6: Phase 1 Storage Infrastructure
      Section 7: Storage Architecture
      Section 8: Dashboard and Visual Interface
      Section 9: Domain Plugin Architecture
      Section 10: Design Principles
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
  [x] OA-08 Microsoft Azure app registered
  [x] OA-09 OneDrive auth — connect microsoft working
            upload/download/list/delete verified
  [x] Microsoft OAuth token stored for usr_ba6d9199

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

## Phase 1 — Storage Foundation (COMPLETE)

  Architecture documented in ARCHITECTURE_DECISIONS.md section 6.

  [x] P1-01 files table in SQLite (+ 3 indexes)
  [x] P1-02 FileStorageAdapter abstract base
  [x] P1-03 LocalFileAdapter (/data/mithra/files/)
  [x] P1-04 MicrosoftGraphClient — fully tested
  [x] P1-05 OneDriveFileAdapter — verified
  [x] P1-06 FileStorageRouter (health=local hardcoded)
  [x] P1-07 DocumentIndexer (Kreuzberg + ChromaDB)
  [x] P1-08 FileSearchService (3-tier retrieval)
  [x] P1-09 Wired into bootstrap
  [x] P1-10 SQLite weekly backup (scripts/backup.py)

  All P1-01 through P1-10 implemented and verified
  OneDrive CRUD verified end to end
  Google OAuth working
  Microsoft OAuth working
  FileStorageRouter health=local enforced
  DocumentIndexer (Kreuzberg) deployed
  FileSearchService (3-tier) ready
  Image size: 1.24GB (acceptable)
  Kreuzberg: replaces Docling, 71MB vs 4-9GB
  Tesseract: installed for OCR support
  784 tests passing

## Phase 1 — Complete (2026-05-29)

  Storage infrastructure: FileStorageAdapter stack
  OAuth: Google + Microsoft working end to end
  Identity: MemberRegistry with proper IDs
  Domain infrastructure: base classes + registry
  784 → 834 tests passing

### Domain plugin infrastructure
  [x] I1 mithra/domains/ package — Domain ABC, DomainContext (frozen),
          ToolResult, DomainEvent, PermissionTier, ToolBundle,
          ToolMetadata, DashboardData, DashboardItem, DomainRegistry
  [x] I2 LangGraphAdapter — domain_registry param, _build_domain_context(),
          _extract_domain_ids(), _execute_with_autonomy()
  [x] I3 Dashboard routes
          GET /api/v1/dashboard/{domain_id}
          GET /api/v1/dashboard/today
          Excluded from auth middleware (HA REST sensors)
  [x] I4 bootstrap.py — DomainRegistry wired into build_state()
  [x] MicrosoftGraphClient CRUD verified
          upload/download/list/delete end to end

### Health endpoint fixed
  [x] asyncio.to_thread for all sync I/O calls
  [x] agent.health() decoupled from llm_router
          (was causing double 3s blocking call)
  [x] llm_router health timeout 3s → 2s
  [x] Per-component HEALTH_TIMING logs added
  [x] 5s overall timeout (raised to 15s temporarily for diagnosis)
  [x] /api/v1/dashboard/today route order fixed (was 404)

## Pending Hardware (blocked until hardware arrives)
  [ ] HA-01 to HA-12 (Home Assistant setup)
  [ ] WY-01 to WY-09 (Wyoming voice pipeline)
  [ ] HW-01 to HW-06 (ESP32-S3-BOX-3)
  [ ] HW-07 to HW-13 (Fire tablet)
  [ ] HW-14 to HW-20 (Samsung fridge, Google Home Mini)

## End-to-End Tests (pending domains)
  [ ] ET-01 to ET-15

## Time Domain — Complete (2026-06-06)

  [x] PH1-02 Time domain — ALL DoD items verified
      822 → 798 tests passing
      Completed: 2026-06-06

  [x] T1 mithra/domains/time/ package — __init__.py, service.py, tools.py, domain.py
  [x] T2 GoogleCalendarService (gcsa wrapper, OAuthManager cred injection, cache)
  [x] T3 8 tools implemented and tested:
          calendar_read_events, calendar_check_availability,
          calendar_daily_briefing, calendar_create_event,
          calendar_create_recurring, calendar_update_event,
          calendar_delete_event, calendar_find_time
  [x] T4 TimeDomain class implementing Domain ABC
  [x] T5 Registered in bootstrap.py (_register_domains after oauth build)
  [x] T6 67 tests passing (parsing, tools, domain, service)
  [x] ADR Section 10 — Time domain architecture documented (sections 10.9–10.14)
  [x] requirements.txt: gcsa>=2.0.0, dateparser>=1.4.0 added
  [x] gcsa Event format: tz-aware datetimes + timezone= param (required for recurring)
  [x] Token expiry handling: invalid_grant detected, TOKEN_EXPIRED surfaced to gateway
  [x] Reconnect flow: expired token → reconnect button in Telegram
  [x] "reconnect google" / "reauth google" bypass is_connected() check
  [x] Dashboard: client_id/secret passed to GoogleCalendarService
  [x] Dashboard: default_user_id fallback (owner uid, not household_id)
  [x] calendar_events table in SQLite (scope, source, indexes)
  [x] classify_event_scope() keyword heuristic (personal / household)
  [x] _record_calendar_event() persists Mithra-created events
  [x] household_config timezone seeded from HOUSEHOLD_TIMEZONE env var
  [x] members table: managed_by + date_of_birth columns (additive migration)
  [x] local_db_path threaded through DomainContext → DomainToolAdapter
  [x] local_db_path also wired in DefaultAutonomyEngine._build_execution_context
  [x] calendar_events recording verified end to end:
      ('Dentist Appointment', 'personal', 0, 'mithra') confirmed in SQLite
  [x] T6b End-to-end verified: "add dentist tuesday 3pm" → Google Calendar
  [x] T7 Dashboard endpoint: GET /api/v1/dashboard/time — returning events

  Time domain closed: 2026-06-06. All DoD items verified end to end.

## Current Focus → Tasks Domain (implementation)
  ADR section 11 committed. Next: T1 schema implementation.

## Tasks Domain

  [x] TD-00 Deep dive complete — use cases, rules, capture flow,
             accountability model, alternatives considered
  [x] TD-01 Architecture decision (ADR section 11) — committed 2026-06-06

  ### Definition of Done

  CODE COMPLETE:
    Schema:
    [ ] tasks table — all fields including source, source_ref
    [ ] projects table
    [ ] milestones table
    [ ] routines table
    [ ] routine_completions table
    [ ] conversation_state table (already in SQLite)
    [ ] All indexes
    [ ] Migration safe for existing installs

    Domain tools (13 total):
    [ ] task_create — captures immediately, asks questions
    [ ] task_list — today/inbox/overdue/urgent/household
    [ ] task_complete — low friction, ACT_TELL
    [ ] task_update — used in capture flow + direct updates
    [ ] task_snooze — defer to date
    [ ] task_delete — DESTRUCTIVE, always confirms
    [ ] task_suggest — "what should I work on today?"
    [ ] project_create — with milestone prompting
    [ ] project_update — status/target date
    [ ] milestone_complete — marks phase done
    [ ] routine_create — household or personal
    [ ] routine_complete — today's completion
    [ ] routine_list — today's done/not-done state

    Autonomy:
    [ ] task_create → WRITE → confirms
    [ ] task_complete → WRITE → ACT_TELL (no confirm)
    [ ] task_delete → DESTRUCTIVE → always confirms
    [ ] routine_complete → READ → immediate

    Classification:
    [ ] classify_task_scope() keyword heuristic
    [ ] classify_task_priority() from title+context
    [ ] Auto-promotion: normal/high → urgent near deadline
    [ ] Waiting follow-up after 7 days

    Capture flow:
    [ ] Quick capture: "add task X" → creates immediately
    [ ] conversation_state tracks enrichment questions
    [ ] One question at a time with Telegram buttons
    [ ] Rich capture: all fields in one message
    [ ] State clears when user done or changes topic

    API:
    [ ] GET /api/v1/tasks/today
    [ ] GET /api/v1/tasks/inbox
    [ ] GET /api/v1/tasks/overdue
    [ ] GET /api/v1/dashboard/tasks → DashboardData
    [ ] GET /api/v1/routines/today
    [ ] POST /api/v1/routines/{id}/complete

    Proactive:
    [ ] Morning task briefing (urgent + overdue)
    [ ] Deadline alert 3 days before
    [ ] Waiting follow-up 7 days
    [ ] Weekly inbox summary Sunday
    [ ] Auto-promotion Telegram notification

  TESTS COMPLETE:
    [ ] Unit tests all 5 tables
    [ ] Unit tests all 13 tools
    [ ] Unit tests classify_task_scope()
    [ ] Unit tests classify_task_priority()
    [ ] Unit tests auto-promotion logic
    [ ] Unit tests routine reset
    [ ] Unit tests conversation_state flow
    [ ] Unit tests all API endpoints
    [ ] Minimum 60 new tests
    [ ] All existing tests pass (798+)

  MANUAL VERIFICATION:
    Capture:
    [ ] "add task fix ceiling fan"
        → task created in INBOX immediately
        → Mithra asks deadline (with buttons)
        → User sets June 15 → asks priority
        → User taps Urgent → task ACTIVE
        → status=active, priority=urgent, due=June 15

    [ ] "add task return amazon package deadline friday urgent"
        → single message capture, no questions
        → status=active immediately

    [ ] "add household task schedule pest control"
        → scope=household

    [ ] "add project backyard deck by July 4"
        → project created, asks milestones

    [ ] "add daily routine mix baby formula for household"
        → routine created, scope=household
        → resets at midnight

    Querying:
    [ ] "what tasks do I have today"
        → urgent + overdue + due today

    [ ] "show my inbox"
        → tasks with status=inbox

    [ ] "what's overdue"
        → due_date < today, not done

    [ ] "how is backyard deck going"
        → project + milestone % complete

    [ ] "what routines are done today"
        → today's completions

    Updating:
    [ ] "mark ceiling fan done"
        → status=done, completed_at set
        → no confirmation needed

    [ ] "snooze insurance call to next Monday"
        → snoozed_until=next Monday

    [ ] "ceiling fan is waiting for Amazon"
        → status=waiting

    [ ] "change ceiling fan to urgent"
        → priority=urgent

    Projects:
    [ ] "complete phase 1 of backyard deck"
        → milestone done, project shows 25%

    Routines:
    [ ] Wife marks "baby formula done"
        → routine_completions row for wife
    [ ] You check routines
        → shows baby formula done by wife at X time
    [ ] Next morning: resets, all unchecked

    API:
    [ ] GET /api/v1/dashboard/tasks → 200 with tasks
    [ ] GET /api/v1/routines/today → completion state
    [ ] POST /api/v1/routines/xxx/complete → marks done
        GET again → shows done

    Boundary:
    [ ] "add task call mom sunday" → tasks (no clock time)
    [ ] "remind me to call mom sunday at 3pm" → time domain
    [ ] task_create never writes to calendar_events
    [ ] No calendar tool creates tasks

    Proactive (test after scheduler added):
    [ ] Morning briefing mentions urgent tasks
    [ ] Alert fires 3 days before deadline
    [ ] Waiting task resurfaces after 7 days

  ARCHITECTURE VERIFIED:
    [ ] Tasks domain never imports Time domain
    [ ] No HA-specific format in domain tools
    [ ] API layer handles format translation
    [ ] DomainContext.local_db_path used for SQLite writes
    [ ] PermissionTier.DESTRUCTIVE on task_delete
    [ ] REVIEW_CHECKLIST.md completed before marking done

  ### Gmail Integration (Phase 2 — after Tasks domain stable)

    [ ] Add gmail.readonly to Google OAuth scope
    [ ] mithra/integrations/gmail/gmail_client.py
        historyId incremental sync (15 min scheduler)
        email body extraction + JSON-LD parsing
    [ ] mithra/integrations/gmail/email_classifier.py
        LLM classifies email type
        return_label|flight_booking|receipt|other
    [ ] Return task auto-creation
        Extract product name + deadline from email
        Create task with source="gmail" source_ref=message_id
        Telegram notification with [View task] button
    [ ] historyId stored in household_config
    [ ] DoD: Amazon return email → task created automatically

  ### Next Steps
    → Step 1: ADR committed ✓ (this commit)
    → Step 2: T1 schema — tasks, projects, milestones,
               routines, routine_completions tables
    → Step 3: T2–T6 tools implementation
    → Step 4: T7–T9 domain class + API routes
    → Step 5: T10 DoD verification
    → Step 6: Gmail Phase 2

## Travel Domain (Phase 3 — after Documents)
  [ ] Gmail FlightReservation JSON-LD parsing
  [ ] Gmail LodgingReservation JSON-LD parsing
  [ ] Auto-create calendar events from reservations
  [ ] Auto-create pre-trip task templates
  [ ] Packing list generation
  [ ] Check-in reminder automation

## Test Baseline
  798 tests passing (2026-06-06)

## Infra Cleanup (deferred to end of Phase 1)
  Automate as part of closing infrastructure phases.
  [ ] Remove unused Docker volumes on phase close
  [ ] docker system prune scheduled monthly
  [ ] Compact docker.vhdx after major cleanups
  [ ] Add cleanup step to CI/CD on phase tags
