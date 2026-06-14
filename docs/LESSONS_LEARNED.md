# Mithra — Lessons Learned

Hard-won debugging knowledge captured to prevent repeating the same
investigation. Different from ARCHITECTURE_DECISIONS.md (which captures
decisions) — this captures operational and debugging knowledge.

Format per lesson:
  ## L-XX: Short title
  Date: YYYY-MM-DD
  Domain: which part of codebase
  Symptom: what the error looked like
  Root cause: why it happened
  Fix: what solved it
  Prevention: how to avoid in future

---

## L-01: asyncio.ensure_future() fails in worker threads
Date: 2026-06-14
Domain: mithra/events — MES EventBus publishing

Symptom:
  "There is no current event loop in thread
   'ThreadPoolExecutor-2_1'"
  OR: ensure_future() silently does nothing

Root cause:
  LangChain tools execute in ThreadPoolExecutor via
  asyncio.to_thread(bundle.tool.invoke, ...).
  ensure_future() looks for a running loop in the
  CURRENT thread — worker threads have none.
  get_running_loop() also fails for the same reason.

Fix:
  1. Capture main event loop at startup (async context):
       set_main_loop(asyncio.get_event_loop())
  2. From any worker thread:
       asyncio.run_coroutine_threadsafe(coro, main_loop)

Prevention:
  Any async work scheduled from tool _run() methods
  MUST use run_coroutine_threadsafe(coro, get_main_loop()).
  Never use ensure_future() or get_running_loop()
  from tool code — tools always run in threads.

---

## L-02: Patch-after-construction gives silent None
Date: 2026-06-14
Domain: mithra/bootstrap.py — dependency wiring

Symptom:
  ctx.event_bus is None at runtime despite bootstrap
  setting state["event_bus"]. No error thrown.
  Feature silently does nothing.

Root cause:
  bootstrap.py built components first, then patched
  dependencies onto them (agent._event_bus = ...).
  When autonomy engine built its own DomainContext,
  it didn't know about the patch pattern and
  never received event_bus.

Fix:
  Module-level singleton pattern:
    get_event_bus() / set_event_bus() in bus.py
  Any code calls get_event_bus() directly.
  No patching, no DomainContext field needed.

Prevention:
  See Section 15 of ARCHITECTURE_DECISIONS.md.
  Process-scoped singletons use get_X()/set_X().
  Never patch attributes onto already-built objects.
  Never add process singletons to DomainContext.

---

## L-03: UTC vs local time in scheduler_log deduplication
Date: 2026-06-07
Domain: mithra/scheduler/task_scheduler.py

Symptom:
  Scheduler sent duplicate morning briefings.
  scheduler_log had correct rows but dedup failed.

Root cause:
  sent_at stored as UTC ISO timestamp.
  Compared against date.today() which returns
  local date (America/Chicago).
  Past midnight UTC but still same local day →
  DATE(sent_at) didn't match local today.

Fix:
  Store sent_at as local time string "YYYY-MM-DD HH:MM:SS"
  Compare with SUBSTR(sent_at, 1, 10).

Prevention:
  All date comparisons in Mithra use household timezone.
  Never mix UTC storage with local date comparison.
  Store scheduler timestamps in local time or always
  convert both sides to the same timezone before comparing.

---

## L-04: LLM picks wrong tool for delete/snooze intents
Date: 2026-06-07
Domain: mithra/agent/langgraph_adapter.py

Symptom:
  "delete call doctor" → LLM selects task_list
  "snooze ceiling fan" → LLM selects task_list
  Instead of task_delete / task_snooze.

Root cause:
  LLM tool selection is probabilistic. Delete/snooze
  phrasing is ambiguous — LLM defaults to safer list.
  calendar_delete_event already had forced selection
  but task tools did not.

Fix:
  Force tool selection before LLM call:
    if any(k in text for k in _CANCEL_KEYWORDS)
      and has_task_delete_tool:
        force_tool = "task_delete"

Prevention:
  Any DESTRUCTIVE or UNAMBIGUOUS intent tool needs
  forced selection. Check when adding new tools:
  would LLM reliably pick this in all phrasings?
  If no → add force logic in _node_llm_tool_selection.

---

## L-05: Milestone capture falls through to classify_intent
Date: 2026-06-07
Domain: mithra/api/gateway.py — multi-turn capture

Symptom:
  After project_create asks "Add milestones?",
  user sends "buy wood, cut pieces, sand"
  → classify_intent fires → project.create (wrong)
  → LLM hallucinates instead of adding milestones

Root cause:
  Capture state check only handled task capture steps
  (due_date, priority, notes). Milestone step not
  intercepted before classify_intent.

Fix:
  _check_and_apply_capture() reads capture_step
  from conversation_state BEFORE classify_intent.
  Notes step and milestone step both intercept
  non-command text.

Prevention:
  Any multi-turn capture step must be added to
  _check_and_apply_capture() when implemented.
  The gateway capture check is the FIRST thing in
  _process_request(), before ALL other processing.

---

## L-06: Google OAuth tokens expire after 7 days
Date: 2026-05-31
Domain: mithra/integrations/google — OAuth

Symptom:
  "connect google" showed "already connected"
  even for expired tokens. Calendar calls failed.

Root cause:
  Token expiry check only looked at access_token
  presence, not refresh_token or expiry time.
  Google OAuth app was in "Testing" mode →
  tokens expire after 7 days regardless.

Fix:
  Check refresh_token presence for "already connected".
  Published OAuth app to production mode (no expiry).
  Added "reconnect google" bypass command.

Prevention:
  Always publish OAuth apps to production before
  household use. Keep refresh_token check separate
  from access_token check.

---

## L-07: Telegram idempotency — double processing
Date: 2026-06-07
Domain: mithra/adapters/messaging/telegram_input.py

Symptom:
  Same message occasionally processed twice,
  creating duplicate tasks or calendar events.

Root cause:
  Telegram guarantees at-least-once delivery.
  Webhook can fire twice for same update_id
  under network conditions.

Fix:
  In-memory dedup set for update_ids with 5-min expiry.
  _PROCESSED_UPDATE_IDS: set[int]
  Check before processing, mark after.

Prevention:
  All inbound webhooks need idempotency checks.
  event_id in MES events table (INSERT OR IGNORE)
  is the system-wide dedup mechanism going forward.

---

## L-08: DomainEvent published before user confirms
Date: 2026-06-14
Domain: mithra/events — MES design

Symptom: (design issue, not a bug)
  If DomainEvent fires when tool is selected but
  before user taps YES, cross-domain side effects
  happen for actions the user may cancel.

Root cause: (potential future bug)
  Tools could publish events at _run() time
  even if the action is WRITE-tier and needs confirm.

Fix/Prevention:
  ADR 14.1 — one-way door: DomainEvents ONLY fire
  after autonomy confirmation (WRITE-tier tools
  wait for confirm before _run() executes).
  READ-tier tools can fire immediately.
  Never publish DomainEvent from tool selection
  or before _run() executes.
