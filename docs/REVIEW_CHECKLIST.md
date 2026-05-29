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
