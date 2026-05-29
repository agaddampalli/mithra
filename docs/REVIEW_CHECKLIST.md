# Mithra — Implementation Review Checklist

## Purpose
Prevent hotfixes by catching architectural divergence
before deployment. Every implementation must pass
this checklist before the PR is merged.

## How to Use
After implementing a task, Claude Code runs through
every applicable item. If ANY item fails, the
implementation is NOT complete — fix it before committing.

---

## 1. Architecture Conformance

Before implementing ANYTHING, verify against ADR:
  [ ] Read the relevant ARCHITECTURE_DECISIONS.md section
  [ ] Every interface in the code matches the ADR spec
  [ ] No new abstractions invented without ADR update
  [ ] If implementation differs from ADR, update ADR FIRST

Specific checks:
  [ ] DomainContext is frozen dataclass (section 9.6)
  [ ] ToolResult always has summary field (section 9.9)
  [ ] PermissionTier.DESTRUCTIVE never auto-executes (9.7)
  [ ] Tool args_schema uses Pydantic (section 9.15)
  [ ] Tools return dict not ToolResult object (9.9)
  [ ] Domains never import other domain modules (9.5)
  [ ] Events only emitted after confirmation (9.10)

## 2. No Hotfixes Rule

Before every code change, ask:
  [ ] Does this fix the ROOT CAUSE or the SYMPTOM?
  [ ] If fixing symptom: stop, find root cause first
  [ ] Does this change touch more than one layer?
       If yes: stop and redesign before implementing

Signs you are writing a hotfix:
  - Adding a mapping/lookup table to paper over a design gap
  - Adding try/except to hide an error instead of fixing it
  - Hardcoding a value that should come from config/context
  - Adding a special case for one specific input

## 3. Interface Verification

After implementing, verify every interface:

  [ ] Run: python -c "from mithra.X import Y; print('ok')"
      for every new module

  [ ] Check constructor signatures match ADR:
      python -c "import inspect; from X import Y;
                 print(inspect.signature(Y.__init__))"

  [ ] Verify required fields have defaults where appropriate:
      DomainContext.language has default "en-US"
      DomainContext.autonomy_override has default None

  [ ] Verify method names match what callers expect:
      If ADR says make_tools(), don't implement build_tools()
      If ADR says get_tools(context), don't implement
      get_tools(service, timezone, context)

## 4. End-to-End Path Verification

Before marking any feature complete, trace the FULL path:

  For domain tools:
  [ ] classify_intent → correct domain_ids extracted
  [ ] domain_ids → correct tools loaded from registry
  [ ] tools loaded → LLM bind_tools called
  [ ] LLM selects tool → correct tool name returned
  [ ] tool executed → ToolResult returned (not exception)
  [ ] ToolResult.summary → used in final response

  Verify each step with a targeted test:
  [ ] python -c "test the specific step in isolation"

## 5. Dependency Field Check

Before using any field from an object:
  [ ] Verify the field EXISTS on that object type
      python -c "print(dir(ObjectType))"
      OR check the dataclass definition

  Known failure patterns to check explicitly:
  [ ] request.context.preferences → DOES NOT EXIST
      Use self._household_timezone instead
  [ ] ContextSnapshot.X → always verify against definition
  [ ] token["access_token"] → token may be a string not dict
      get_token() returns str, not dict

## 6. Test Coverage

  [ ] Every new public method has at least one test
  [ ] Every error path has a test
  [ ] Tests use real interfaces (no mocking of internals)
  [ ] Tests verify behavior not implementation details

  Minimum test counts:
  [ ] New domain: 40+ tests
  [ ] New service: 10+ tests
  [ ] New utility: 5+ tests

## 7. Integration Smoke Test

Before final commit, run this sequence:

  [ ] pytest tests/unit/ -x -q (all pass)
  [ ] python -c "import mithra.domains.X.domain" (imports ok)
  [ ] python -c "
      domain = XDomain(...)
      bundles = domain.get_tools(mock_context)
      print(len(bundles), 'tools loaded')
      print([b.tool.name for b in bundles])
      " (tools load correctly)

## 8. Architecture Debt Log

If you implement something that deviates from ADR:
  [ ] Add a TODO comment in the code:
      # TODO: This deviates from ADR section X because Y.
      # Correct approach: Z. Track in TRACKER.md.
  [ ] Add to docs/TRACKER.md under "Architecture Debt"
  [ ] NEVER leave a divergence undocumented

---

## Common Failure Patterns (learn from these)

PATTERN 1 — Wrong function name
  ADR says: make_tools()
  Built:    build_time_tools()
  Result:   ImportError at runtime
  Fix:      Match ADR function names exactly

PATTERN 2 — Missing default values
  ADR says: language: str = "en-US" (optional)
  Built:    language: str (required)
  Result:   TypeError at runtime when context created
  Fix:      Fields with sensible defaults must have them

PATTERN 3 — Wrong return type
  ADR says: get_token() returns str
  Used as:  token["access_token"] (treats as dict)
  Result:   TypeError at runtime
  Fix:      Check return type before using

PATTERN 4 — Missing bind_tools
  ADR says: LLM selects tool via bind_tools
  Built:    String matching intent → tool name
  Result:   Wrong tools selected, no LLM involvement
  Fix:      bind_tools is not optional, it's the architecture

PATTERN 5 — Silent exception swallowing
  Built:    except Exception: return error_response
  Result:   Real errors hidden, impossible to debug
  Fix:      Always log with traceback before returning error

---

## 9. Confirmation Flow Checklist

When any WRITE or DESTRUCTIVE tool triggers a confirmation,
trace the full round-trip before marking the feature complete.

  [ ] _node_execute_tool stores user_id in pending payload:
      pending_actions[0]["payload"]["user_id"] = request.user_id
      Verify: grep for "user_id" in the payload dict at plan time

  [ ] DefaultAutonomyEngine.confirm() reads user_id from payload:
      exec_user_id = pending.payload.get("user_id") or user_id
      NEVER uses the caller's user_id directly — caller may be
      a different user than the one who made the original request

  [ ] Engine has _domain_registry set at runtime:
      bootstrap.py patches engine._domain_registry after build
      Verify: engine._domain_registry is not None at confirm() call

  [ ] _build_execution_context() called with exec_user_id (not household):
      DomainContext.user_id must match the requesting member's ID
      Google OAuth lookup uses this user_id — wrong ID = auth failure

  [ ] DomainToolAdapter registered as executor fallback:
      executor.register_fallback(adapter) called in bootstrap
      Verify: executor._fallback is not None before first confirm

  [ ] Tool name in pending payload matches domain tool name exactly:
      pending.payload["tool_name"] must equal bundle.tool.name
      Mismatch = "tool not found" at confirm time, not at plan time

  [ ] ActionLogEntry written on confirm (success AND failure):
      Status "confirmed" written regardless of tool outcome
      Success/failure recorded in entry.success field

  [ ] Pending action removed from store after confirm or reject:
      get_pending() must return empty list after resolution
      Verify: _storage.delete() called with correct key

### Round-Trip Test Procedure (run before marking any WRITE tool done)

  Step 1 — Plan:
    Send: "add dentist tuesday 3pm"
    Verify: response contains confirmation buttons [Yes] [No]
    Verify: pending action stored with correct user_id in payload

  Step 2 — Inspect pending:
    Call engine.get_pending(user_id)
    Verify: one entry, domain="time", payload has user_id

  Step 3 — Confirm:
    Simulate "Yes" → engine.confirm(request_id, 0, user_id, executor)
    Verify: ExecutionReceipt.executed has one entry

  Step 4 — Check calendar:
    Call calendar_read_events for the scheduled day
    Verify: event appears in Google Calendar

  Step 5 — Verify pending cleared:
    Call engine.get_pending(user_id)
    Verify: empty list returned

---

## 10. Logging and Exception Handling Standards

Every module must follow these rules. Deviations hide bugs.

### Module Requirements

  [ ] Module-level logger defined at top of every file:
      _logger = logging.getLogger(__name__)
      Never use print(). Never use logging.info() directly.
      Never use a logger named "mithra" — always __name__.

  [ ] No bare logger passed into constructors:
      Modules own their own logger. Never inject a logger
      as a constructor argument unless building a test stub.

### Required Log Points

  [ ] Domain tool success — INFO level:
      _logger.info("calendar_create_event succeeded: %s", event_id)

  [ ] Domain tool failure — WARNING level (user-facing failure):
      _logger.warning("calendar_create_event failed: %s", error_msg)

  [ ] Infrastructure failure — ERROR level with exc_info:
      _logger.error("Google Calendar API error", exc_info=True)

  [ ] Confirmation flow entry — DEBUG level:
      _logger.debug("confirm() called: request_id=%s user_id=%s",
                    request_id, user_id)

  [ ] NEVER violations — ERROR level (security audit):
      _logger.error("NEVER violation: domain=%s action=%s user=%s",
                    domain, action_type, user_id)

### Exception Handling Rules

  [ ] Tools NEVER propagate exceptions to the agent graph:
      All exceptions caught at tool boundary, returned as
      ToolResult(success=False, summary="...", error="...")
      Agent graph receives ToolResult, not exception

  [ ] Infrastructure exceptions logged BEFORE returning error:
      try:
          result = gc.create_event(event)
      except Exception as exc:
          _logger.error("create_event failed", exc_info=True)
          return ToolResult(success=False,
                            summary=f"Could not create event: {exc}",
                            error=str(exc))

  [ ] Never use bare except without re-raise or log:
      except Exception:           ← BANNED (silent swallow)
      except Exception as exc:    ← REQUIRED
          _logger.error(..., exc_info=True)
          # then return error or re-raise

  [ ] confirm() catches per-path exceptions, falls through:
      Domain registry path fails → log warning → try executor path
      Executor path fails → log error → return failed receipt
      Never let one path's exception kill the whole confirm()

### Mandatory Domain Tool Exception Pattern

Every domain tool's _run() must follow this exact structure:

    def _run(self, **kwargs) -> dict:
        try:
            # do the work
            return ToolResult(success=True, summary="...").model_dump()
        except SomeSpecificError as exc:
            _logger.warning("%s: %s", self.name, exc)
            return ToolResult(
                success=False,
                summary=f"Could not complete: {exc}",
                error=str(exc),
                error_code="SPECIFIC_ERROR_CODE",
            ).model_dump()
        except Exception as exc:
            _logger.error("%s unexpected error", self.name, exc_info=True)
            return ToolResult(
                success=False,
                summary=f"Unexpected error in {self.name}",
                error=str(exc),
            ).model_dump()
