# Mithra — Work Tracker (Updated 2026-06-21)

## SESSION HANDOFF (update after each session)

Last updated: 2026-06-21

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
  Tasks domain: COMPLETE 2026-06-13 — 13 tools + 5 scheduler jobs + 6 API routes verified
  Section 12 Response Standards: COMPLETE — ADR + all Time/Tasks tools updated
  routing.yaml fallback chain: COMPLETE — gemini-flash primary, full cross-fallback
  Debug scheduler endpoint: COMPLETE — /api/v1/debug/scheduler/{job} behind MITHRA_DEBUG
  Telegram webhook idempotency: COMPLETE — instance-level dedup, 5 min expiry
  MES ADR: COMPLETE — Section 14 documented, MES-00 through MES-23 tracked
  MES Phase 0: COMPLETE 2026-06-14 — all MES-00 through MES-07 verified end-to-end
              get_event_bus()/set_event_bus() singleton, get_main_loop()/set_main_loop()
              for thread-safe publish from ThreadPoolExecutor workers
              CalendarCreateEventTool + CalendarDeleteEventTool verified: events table
              shows processed=1, error_count=0, full payload with request_id
              1182 tests passing
  Section 15 ADR: COMPLETE 2026-06-14 — dependency management standards documented
              Three categories: request-scoped (DomainContext), process singletons
              (get_X()), config (household_config). Patch anti-pattern FORBIDDEN.
  MES Phase 1: COMPLETE 2026-06-21 — MES-08 through MES-10 verified end-to-end
              TelegramNotificationHandler (httpx async, chat_id lookup via member_channels)
              NotificationEvent(BaseEvent) added; _publish_notification_event() fire-and-forget
              wired into all 5 scheduler jobs alongside unchanged direct _send_telegram()
              replay_failed() filters notification.* + error_count >= 3
              Verified: scheduler_log both members recorded, events table processed=1
              error_count=0, handler delivered to both chat_ids; 1196 tests passing

### In Progress
  T9 — HA setup on personal Android tablet

### Immediate Next Steps
  1. HA setup on personal Android tablet
  2. MES Phase 2 (MES-11 to MES-19) — Gmail InboundAdapter + email classifier
  3. People domain

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
  - MES: InboundEvent (external) vs DomainEvent (internal) — separate contracts
  - MES: EventBus two-way door — AsyncioSQLiteEventBus → Redis → Kafka
  - MES: max causation depth 5, error isolation per handler, idempotency via event_id

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
  [x] ARCHITECTURE_DECISIONS.md (14 sections)
      Section 1:  Identity & Household Management
      Section 2:  OAuth Token Management
      Section 3:  File Storage and Document RAG
      Section 4:  Document Interaction UX
      Section 5:  Document Processing Libraries (Kreuzberg)
      Section 6:  Phase 1 Storage Infrastructure
      Section 7:  Storage Architecture
      Section 8:  Dashboard and Visual Interface
      Section 9:  Domain Plugin Architecture
      Section 10: Time Domain
      Section 11: Tasks Domain
      Section 12: Response Message Standards
      Section 13: Design Principles
      Section 14: Mithra Event System (MES)
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

## Current Focus → T9: HA REST sensor wiring for tablet tiles, then People domain

## Tasks Domain — COMPLETE 2026-06-13

  Schema: tasks, projects, milestones, routines,
          routine_completions, conversation_state,
          scheduler_log — all verified

  13 tools verified end to end:
  [x] task_create — immediate capture, active status when priority/deadline provided
  [x] task_list — today/inbox/overdue/urgent/household filters
  [x] task_complete — READ tier, immediate
  [x] task_update — capture flow enrichment
  [x] task_snooze — READ tier, title search, immediate
  [x] task_delete — soft delete, title search, confirms
  [x] task_suggest — top 3 by priority/deadline
  [x] project_create — milestone capture flow
  [x] project_update — status/date updates
  [x] milestone_complete — ordinal resolution (phase 1/2)
  [x] project_status — 📋 format with ✓/○
  [x] routine_create — READ tier, household scope
  [x] routine_complete — idempotent, timezone-aware
  [x] routine_list — done/pending split

  Proactive scheduler (5 jobs, America/Chicago):
  [x] auto_promote (6am)
  [x] morning_briefing (7am) — duplicate prevention ✅
  [x] deadline_alerts (8am)
  [x] waiting_followup (9am)
  [x] weekly_summary (Sunday 9am)

  API routes verified:
  [x] GET /api/v1/tasks/today
  [x] GET /api/v1/tasks/inbox
  [x] GET /api/v1/tasks/overdue
  [x] GET /api/v1/dashboard/tasks
  [x] GET /api/v1/routines/today
  [x] POST /api/v1/routines/{id}/complete

  Multi-turn capture: conversation_state ✅
  Response standards: Section 12 applied ✅
  Force tool selection: task_delete, task_snooze ✅
  Telegram webhook idempotency ✅
  Tests: 1133 passing

---

  [x] TD-00 Deep dive complete — use cases, rules, capture flow,
             accountability model, alternatives considered
  [x] TD-01 Architecture decision (ADR section 11) — committed 2026-06-06

  ### T1 — Schema (COMPLETE 2026-06-06)
  [x] tasks table — all 17 fields including source, source_ref
  [x] projects table
  [x] milestones table
  [x] routines table
  [x] routine_completions table (UNIQUE idx on routine_id+completion_date)
  [x] conversation_state table (already present)
  [x] All 5 indexes (household, user, due, priority, routine_day)
  [x] DDL executed in LocalStorageAdapter constructor

  ### T2 — Classification helpers (COMPLETE 2026-06-06)
  [x] classify_task_scope() keyword heuristic
  [x] classify_task_priority() from title+context+due_date
  [x] should_auto_promote() — NORMAL≤7d→urgent, HIGH≤3d→urgent
  [x] new_task_id(), new_project_id(), new_milestone_id(), new_routine_id()
  [x] 56 unit tests all passing

  ### T5 — Routine tools (COMPLETE 2026-06-07)
  [x] routine_create — scope auto-classify, reset_time HH:MM, WRITE tier
  [x] routine_complete — household timezone date, idempotent (IntegrityError),
                         detects same-user vs other-member duplicate
  [x] routine_list — LEFT JOIN completions, pending before done, 🏠 badge,
                     scope filter (all|household|personal)
  [x] GET /api/v1/routines/today — unauthenticated, HA tablet
  [x] POST /api/v1/routines/{id}/complete — idempotent, done|already_done
  [x] Auth middleware exclusion for /api/v1/routines
  [x] Dashboard summary extended with routine done count
  [x] classify_intent prompt: routine.create / routine.complete / routine.list
  [x] 1080 tests passing (36 new)
  Manual checks verified:
    show routines → correct done/pending split
    baby formula done → immediate, no confirm
    double-mark → "already marked done today" (friendly, no crash)
    API /routines/today → correct JSON
    dashboard includes "2/3 routines done"

  ### T7 — Proactive scheduler (COMPLETE 2026-06-09)
  [x] mithra/scheduler/task_scheduler.py — 5 jobs
  [x] job_morning_briefing — urgent + overdue summary per member, 7am Chicago
  [x] job_deadline_alerts — tasks due in ≤3 days, 8am Chicago
  [x] job_waiting_followup — waiting tasks silent 7+ days, 9am Chicago
  [x] job_weekly_summary — inbox count + top urgent, Sunday 9am Chicago
  [x] job_auto_promote — normal/high → urgent at threshold, 6am Chicago
  [x] scheduler_log UNIQUE INDEX dedup — INSERT OR IGNORE, 5 min expiry
  [x] APScheduler AsyncIOScheduler wired into FastAPI lifespan
  [x] Timezone: America/Chicago via CronTrigger(timezone=tz)
  [x] _send_telegram via stdlib urllib (no httpx dependency)
  [x] Admin failure alert on job exception
  [x] 42 unit tests passing

  ### T8 — Remaining API routes (COMPLETE 2026-06-09)
  [x] GET /api/v1/tasks/today — urgent + high + overdue, limit 20
  [x] GET /api/v1/tasks/inbox — status=inbox tasks
  [x] GET /api/v1/tasks/overdue — due_date < today, not done
  [x] Auth middleware exclusion for /api/v1/tasks and /api/v1/debug
  [x] GET /api/v1/debug/scheduler/{job_name} behind MITHRA_DEBUG=true
  [x] Telegram webhook idempotency — instance-level dedup, 5 min TTL

  ### T4 — Project and milestone tools (COMPLETE 2026-06-07)
  [x] project_create — milestone capture flow (gateway intercepts comma list)
  [x] project_update — title/status/target_date, SET clause dynamic
  [x] milestone_complete — ordinal resolution (phase 1/2/first/second → order_index)
                           title LIKE → ordinal → next-pending fallback chain
  [x] project_status — ✓/○ milestone format, % complete
  [x] _find_project() — LEFT JOIN milestones, milestone-rich project wins
  [x] _resolve_milestone() — title LIKE → digit/ordinal → next-pending
  [x] Milestone capture gateway flow (project_create stores step=milestone)
  [x] INTENT_DOMAIN_MAP: project/milestone prefixes → tasks domain
  [x] classify_intent low-confidence project keyword fallback (0.75)
  [x] 1044 tests passing

  ### T3 — Core task tools (COMPLETE 2026-06-06)
  [x] task_create — scope+priority auto-classify, ISO due date, emits event
  [x] task_list — today/inbox/overdue/urgent/household/waiting/all filters
  [x] task_complete — status→done, completed_at, emits task_completed event
  [x] task_update — title/due_date/priority/status/notes, SET clause dynamic
  [x] task_snooze — status→snoozed, snoozed_until date
  [x] task_delete — DESTRUCTIVE, household-scoped DELETE
  [x] task_suggest — top-3 by priority+overdue, excludes done/snoozed/cancelled
  [x] build_tasks_tools() factory → 7 ToolBundles
  [x] TasksDomain.get_tools() wired to factory
  [x] TasksDomain.get_dashboard_data() — live SQLite queries (overdue+inbox alerts)
  [x] 65 unit tests all passing (121 total in tasks/)
  [x] _parse_due_date() helper (dateparser, ISO passthrough)
  [x] capture state helpers (_store/_get/_clear/_next_question)
  [x] FIX: _strip_fences handles truncated JSON + JSON embedded in text
  [x] FIX: classify_intent max_tokens=400 (prevents truncation)
  [x] FIX: task_create writes conversation_state after insert
  [x] FIX: task_update reads capture state when task_id empty, advances step
  [x] FIX: gateway _check_capture_context routes follow-up replies before agent.run

  ### Definition of Done

  CODE COMPLETE:
    Domain tools (14 total):
    [x] task_create
    [x] task_list
    [x] task_complete
    [x] task_update
    [x] task_snooze
    [x] task_delete
    [x] task_suggest
    [x] project_create — with milestone prompting
    [x] project_update — status/target date
    [x] milestone_complete — marks phase done
        phase 1/2/next all resolve correctly
    [x] project_status — shows ✓/○ milestone format
    [x] routine_create — household or personal
    [x] routine_complete — today's completion (idempotent)
    [x] routine_list — today's done/not-done state

    Autonomy:
    [x] task_create → WRITE (autonomy_level=2)
    [x] task_complete → WRITE, ACT_TELL (autonomy_level=2)
    [x] task_delete → DESTRUCTIVE → always confirms
    [x] routine_complete → READ → immediate

    Classification:
    [x] classify_task_scope() keyword heuristic
    [x] classify_task_priority() from title+context
    [x] Auto-promotion: normal/high → urgent near deadline

    API:
    [x] GET /api/v1/tasks/today
    [x] GET /api/v1/tasks/inbox
    [x] GET /api/v1/tasks/overdue
    [x] GET /api/v1/dashboard/tasks → DashboardData (via DomainRegistry)
    [x] GET /api/v1/routines/today
    [x] POST /api/v1/routines/{id}/complete

    Proactive:
    [x] Morning task briefing (urgent + overdue)
    [x] Deadline alert 3 days before
    [x] Waiting follow-up 7 days
    [x] Weekly inbox summary Sunday
    [x] Auto-promotion Telegram notification

  TESTS COMPLETE:
    [x] Unit tests all 5 tables (TestTasksSchema — 5 tests)
    [x] Unit tests 7 core tools (65 tests)
    [x] Unit tests classify_task_scope() (15 tests)
    [x] Unit tests classify_task_priority() (16 tests)
    [x] Unit tests auto-promotion logic (10 tests)
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

## Mithra Event System (MES) — Phase 0 starts after People domain

  Full design: ARCHITECTURE_DECISIONS.md Section 14

  ### MES Roadmap Position
    Phase 0 (now): AsyncioSQLiteEventBus contracts — in-process, durable
    Phase 1:       Notification refactor — wired into all domains
    Phase 2:       Gmail integration — first InboundAdapter
    Phase 3:       Travel/Money handlers, HA sensors, Redis migration

  ### MES Phase 0 — Contracts (COMPLETE 2026-06-14)
  [x] MES-00  BaseEvent, InboundEvent, DomainEvent dataclasses
              mithra/events/base.py
              Fields: event_id, topic, household_id, user_id,
              timestamp, causation_id, causation_depth (BaseEvent);
              source, raw_data, classified (InboundEvent);
              event_type, source_domain, payload, request_id (DomainEvent)
  [x] MES-01  EventBus ABC
              mithra/events/bus.py
              Methods: publish(), subscribe(), unsubscribe(), replay(), health()
  [x] MES-02  AsyncioSQLiteEventBus + module-level singletons
              mithra/events/asyncio_sqlite_bus.py
              asyncio queues + SQLite events table for durability
              Handler error isolation — each handler in isolated try/except
              Dead letter after error_count >= 3
              get_event_bus()/set_event_bus() — process-level singleton (ADR 15.2)
              get_main_loop()/set_main_loop() — main loop captured at startup for
              thread-safe publish via asyncio.run_coroutine_threadsafe()
  [x] MES-03  events table DDL in LocalStorageAdapter.__init__
              Columns: event_id, topic, household_id, user_id,
              timestamp, causation_id, causation_depth,
              payload_json, processed, processed_at, error_count, last_error
              Indexes: idx_events_topic, idx_events_household, idx_events_causation
  [x] MES-04  InboundAdapter ABC
              mithra/events/inbound.py
              Methods: source (property), start(), stop(), _normalise(), health()
  [x] MES-05  EventHandler ABC
              mithra/events/handler.py
              Methods: topic_patterns (property), handle(event, context) → None
  [x] MES-06  EventBus wired end-to-end; CalendarCreateEventTool and
              CalendarDeleteEventTool publish domain.time.event_created /
              domain.time.event_deleted after successful Google Calendar ops
              bootstrap: set_event_bus() + set_main_loop() at startup
              Tools: get_event_bus() + get_main_loop() + run_coroutine_threadsafe()
              Verified: events table processed=1, error_count=0, full payload
              Key fixes: patch anti-pattern replaced (ADR 15), asyncio thread safety
  [x] MES-07  Unit tests for Phase 0 contracts
              publish/subscribe happy path
              idempotency (same event_id twice, handler called once)
              causation depth guard (depth > 5 → dead letter, not delivered)
              error isolation (handler[0] fails, handler[1] still runs)
              replay (processed=0 events re-delivered on restart)
              49 tests passing

  ### MES Phase 1 — Notification Refactor (COMPLETE 2026-06-21)
  [x] MES-08  TelegramNotificationHandler
              mithra/events/handlers/notification_handler.py
              Subscribes to notification.telegram.send
              Delivers via httpx.AsyncClient (async-native, non-blocking)
              chat_id: payload["chat_id"] → fallback event.user_id → member_channels DB lookup
              Raises on HTTP error so bus logs error_count (ADR 14.7 error isolation)
  [x] MES-09  NotificationEvent(BaseEvent) added to mithra/events/base.py
              _publish_notification_event() fire-and-forget helper in task_scheduler.py
              run_coroutine_threadsafe() — never blocks scheduler thread (LESSONS_LEARNED L-01)
              Wired into all 5 scheduler jobs after _record_sent() succeeds
              Existing _send_telegram() + scheduler_log path UNCHANGED (additive strategy)
              bootstrap.py wires TelegramNotificationHandler when TELEGRAM_BOT_TOKEN set
  [x] MES-10  replay_failed() filters notification.* topics (no duplicate sends on restart)
              _load_unprocessed(): AND topic NOT LIKE 'notification.%' AND error_count < 3
              _reconstruct_event(): notification.* → NotificationEvent
              14 new tests, 1196 total passing
  [ ] MES-11  Cross-domain handler: tasks → time
              domain.tasks.task_overdue → calendar_create_event (time-block opt-in)
  [ ] MES-12  Cross-domain handler: time → tasks
              domain.time.appointment_created → task_create (prep task opt-in)
  [ ] MES-13  Integration tests: event published → handler invoked → domain action

  ### MES Phase 2 — Gmail Integration
  [ ] MES-14  GmailInboundAdapter
              mithra/events/adapters/gmail_inbound.py
              historyId incremental polling, 15 min CronTrigger
              Publishes inbound.gmail.email_received
  [ ] MES-15  EmailClassifier
              mithra/events/adapters/email_classifier.py
              LLM classifies: return_label | flight_booking | receipt | other
  [ ] MES-16  inbound.gmail.* router
              return_label → task_create (source="gmail", source_ref=message_id)
              flight_booking → travel domain (Phase 3)
              receipt → money domain (Phase 3)
  [ ] MES-17  Return task auto-creation from email
              Extract product name + deadline from email body
              Telegram notification with [View task] button
  [ ] MES-18  DoD: Amazon return email → task created automatically
              historyId stored in household_config

  ### MES Phase 3 — Deferred
  [ ] MES-19  Travel domain: FlightReservation JSON-LD parsing handler
  [ ] MES-20  Travel domain: inbound.gmail.flight_booking →
              calendar_create_event (flight) + calendar_create_event (hotel)
  [ ] MES-21  Money domain: domain.money.expense_recorded fan-out handler
  [ ] MES-22  HA sensor handler: inbound.ha_sensor.* → domain tools
  [ ] MES-23  RedisEventBus adapter (swap AsyncioSQLiteEventBus when needed)

---

## Travel Domain (Phase 3 — after Documents)
  [ ] Gmail FlightReservation JSON-LD parsing
  [ ] Gmail LodgingReservation JSON-LD parsing
  [ ] Auto-create calendar events from reservations
  [ ] Auto-create pre-trip task templates
  [ ] Packing list generation
  [ ] Check-in reminder automation

## Test Baseline
  1182 tests passing (2026-06-13)

## Infra Cleanup (deferred to end of Phase 1)
  Automate as part of closing infrastructure phases.
  [ ] Remove unused Docker volumes on phase close
  [ ] docker system prune scheduled monthly
  [ ] Compact docker.vhdx after major cleanups
  [ ] Add cleanup step to CI/CD on phase tags
