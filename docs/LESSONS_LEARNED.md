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

---

## L-09: HA template entity_id stuck on "unknown" — name: template evaluating empty at cold boot
Date: 2026-06-26/27
Domain: Home Assistant config (homeassistant_config/)

Symptom:
  10 template button entities all registered as
  button.unknown, button.unknown_2, etc. instead of
  their intended slugs, despite unique_id being set
  correctly on each.

Root cause:
  HA derives entity_id from the `name:` field AT FIRST
  CREATION, not from unique_id (unique_id only prevents
  duplicate registration and preserves history across
  restarts). When name: is a Jinja template that
  evaluates to empty/unavailable at the exact moment of
  first boot (because a backing REST sensor hadn't
  polled yet), the entity_id permanently falls back to a
  generic "unknown" slug — and this is STICKY. It never
  re-derives once the template starts returning real
  values. This is a well-documented HA Community gotcha,
  not specific to our setup.

Fix:
  Use the official `default_entity_id:` field (HA
  template integration) to decouple entity_id generation
  from the dynamic name template entirely:
    name: >-
      {{ states('sensor.x') if ... else 'Fallback' }}
    unique_id: my_entity
    default_entity_id: button.my_entity
  default_entity_id is a fixed string controlling
  entity_id deterministically on first creation,
  regardless of what name: evaluates to at that moment.
  Already-stuck entities (created before this fix) must
  be renamed once via Settings → Entities → gear icon →
  Entity ID field (safe, supported UI action) or via the
  config/entity_registry/update WebSocket API command for
  bulk renames — never edit core.entity_registry as a
  JSON file directly while HA is running.

Prevention:
  ANY template entity (sensor, button, etc.) whose name:
  depends on external/dynamic data (REST sensor, API
  response) MUST also set default_entity_id: from the
  start. Add this to the standard pattern for any new
  Mithra-driven HA entity going forward — do not wait
  for the bug to appear again.

---

## L-10: New Docker container ports need explicit WSL2 portproxy bridge — localhost forwarding is NOT enough
Date: 2026-06-27
Domain: Windows/WSL2 host networking

Symptom:
  HA (port 8123) reachable via `curl localhost:8123` from
  Windows AND from WSL2, but completely unreachable from
  any other device on the LAN (timeout, not refused).

Root cause:
  WSL2 automatically forwards localhost/127.0.0.1 from
  Windows into the WSL2 VM, but does NOT bind new ports
  onto Windows' actual LAN-facing network interface. This
  is the identical root cause already solved for SSH
  (port 2222) earlier in this project via netsh portproxy
  + wsl-watchdog.ps1 — but the lesson wasn't generalized,
  so it had to be rediscovered for port 8123.

Fix:
  netsh interface portproxy add v4tov4 listenport=8123 \
    listenaddress=0.0.0.0 connectport=8123 \
    connectaddress=<current WSL IP>
  Plus a Windows Firewall inbound rule for the port.

Prevention:
  EVERY new service exposed in compose.yaml with a host
  port mapping needs this same portproxy bridge if it
  must be reachable from other LAN devices (not just from
  Windows itself). This should be a standard checklist
  item when adding any new container with published ports
  — do not assume Docker's port publish alone makes it
  LAN-reachable from a WSL2 host. Better long-term fix:
  prefer Cloudflare tunnel routes (as we ultimately did
  for HA) over LAN-IP access entirely, since tunnel
  routes don't depend on WSL's IP at all and survive
  reboots without any watchdog script.

---

## L-11: Cloudflare Access (email) and HA's own login (username/password) are independent layers — don't conflate them
Date: 2026-06-27
Domain: Cloudflare Access + Home Assistant auth

Symptom/confusion:
  After Cloudflare Access let a request through (email +
  one-time code, successful), HA's OWN login screen still
  appeared and rejected credentials — looked like a
  failure, was actually two separate, both-working auth
  layers in sequence.

Clarification for future reference:
  Cloudflare Access protects WHO can reach the hostname
  at all (email-based, checked once per long session).
  HA's own login protects WHICH HA USER is signed in
  (username/password, completely local to HA, NO email
  required or even possible to attach). A "wall tablet"
  or "kiosk" HA user is HA's own documented pattern for
  this ("a local-only account for a wall-mounted tablet
  or kiosk" — home-assistant.io/docs/configuration/
  user-configuration). This does NOT require a second
  email address — it's a local username/password pair,
  same category as a shared-device PIN, not a real
  identity needing its own inbox.

Prevention:
  When debugging "login failed" after Cloudflare Access
  succeeded, always check WHICH layer actually rejected
  the request before assuming Cloudflare itself failed —
  the two error messages look superficially similar but
  come from entirely different systems.

---

## L-12: HACS resource_mode: yaml disables auto-registration of custom card resources
Date: 2026-07-03
Domain: Home Assistant config

Symptom:
  Mushroom Cards installed successfully via HACS but
  rendered as raw/unstyled error cards. HACS Resources
  management UI page was also missing from
  Settings → Dashboards.

Root cause:
  lovelace: resource_mode: yaml (set to keep dashboard
  config git-versioned) disables HACS's automatic
  resource registration AND hides the Resources UI page
  entirely — by design. HA assumes you are managing
  resources manually in YAML when this mode is active.

Fix:
  Manually add each HACS frontend card as a resource
  entry in configuration.yaml under lovelace:
    resources:
      - url: /local/community/<card-name>/<card>.js
        type: module
  The www/ directory maps to /local/ in HA's URL space
  automatically. Confirm the actual filename from:
    find homeassistant_config/www -name "*.js" -not -name "*.gz"
  before writing the URL.

Prevention:
  Any future HACS frontend card install requires this
  manual step when resource_mode: yaml is active. Add
  it as a standard checklist item whenever installing
  a frontend card via HACS.

---

## L-13: Custom HA sidebar cards — two confirmed bugs, Path B abandoned
Date: 2026-07-03
Domain: Home Assistant HACS cards

Symptom:
  Both evaluated sidebar-navigation card alternatives had
  show-stopping bugs when tested on the real deployment:
    DBuit/sidebar-card: "Repository structure for 0.1.9.9
      is not compliant" — failed HACS validation entirely,
      could not install.
    lotablet/ha-dashboard-sidebar: installed and rendered
      correctly for single nav items, but all buttons
      navigated to the same destination regardless of each
      button's configured navigation_path (confirmed not a
      config error — bug is in the card's click handler
      binding).

Root cause:
  Both cards were selected from 2020-2021 era community
  recommendations. Neither had been verified against
  current HACS validation rules or current HA versions
  before recommendation.

Fix:
  Reverted to Path A (HA's native multi-dashboard
  sidebar, proven working in UI-01). Weather and quick-
  action elements moved to the Home page content area
  rather than a persistent sidebar (acceptable compromise
  per ADR 17.3).

Prevention:
  Before recommending any HACS card, verify:
  1. Last commit date — cards >2 years without updates
     are at high risk of HACS non-compliance.
  2. Star count and active issues — low-star projects
     have no community to surface known bugs.
  3. Install it on the real deployment first with a
     minimal test config before rebuilding anything
     that depends on it.
