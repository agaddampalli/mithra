"""
FastAPI gateway — channel-agnostic lifecycle orchestrator and single HTTP entry point.

Startup delegates all component wiring to mithra.bootstrap.build_state().
Gateway owns only: auth middleware, route definitions, request lifecycle.

Request lifecycle (per CLAUDE.md contract):
  1. Receive normalised AgentRequest from input adapter
  2. Load ContextSnapshot via DomainMemoryRouter
  3. Call agent.run() or agent.run_stream()
  4. Pass AgentResult to AutononomyEngine.process()
  5. Pass ExecutionReceipt to output adapter
  6. Return response to user

Auth: Bearer token middleware validates SECRET_KEY on all /api/v1/* routes.
/api/v1/health is unauthenticated (Docker healthcheck).
Exposed at assistant.mithra-app.com via the Cloudflare tunnel.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared app state
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {}


def get_state() -> dict[str, Any]:
    return _state


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    from mithra.bootstrap import build_state, load_household_config

    household_config = load_household_config()
    state = await build_state(household_config, on_request=_process_request)
    _state.update(state)

    # Scheduler credentials — available from env regardless of Telegram adapter
    _state["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _state["admin_telegram_id"] = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID", "")

    event_bus = _state.get("event_bus")
    if event_bus:
        from mithra.events.bus import set_main_loop
        set_main_loop(asyncio.get_event_loop())
        await event_bus.start()

    telegram_input = _state.get("telegram_input")
    if telegram_input:
        asyncio.create_task(telegram_input.start())

    # Start proactive task scheduler
    scheduler = _build_task_scheduler(_state)
    scheduler.start()
    _state["task_scheduler"] = scheduler

    yield

    scheduler.shutdown(wait=False)

    if event_bus:
        await event_bus.stop()

    if telegram_input:
        await telegram_input.stop()


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------

def _build_task_scheduler(state: dict):
    """Build APScheduler with all 5 proactive task jobs."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    from mithra.scheduler.task_scheduler import (
        job_auto_promote,
        job_morning_briefing,
        job_deadline_alerts,
        job_waiting_followup,
        job_weekly_summary,
    )

    tz_name = state.get("household_timezone", "America/Chicago")
    tz = pytz.timezone(tz_name)
    scheduler = AsyncIOScheduler(timezone=tz)

    def _wrap(fn):
        """Wrap a sync job function so APScheduler errors are caught and logged."""
        def wrapped():
            try:
                fn(state)
            except Exception as exc:
                _logger.error("Scheduler job %s failed: %s", fn.__name__, exc, exc_info=True)
        return wrapped

    scheduler.add_job(
        _wrap(job_auto_promote),
        CronTrigger(hour=6, minute=0, timezone=tz),
        id="auto_promote", replace_existing=True,
    )
    scheduler.add_job(
        _wrap(job_morning_briefing),
        CronTrigger(hour=7, minute=0, timezone=tz),
        id="morning_briefing", replace_existing=True,
    )
    scheduler.add_job(
        _wrap(job_deadline_alerts),
        CronTrigger(hour=8, minute=0, timezone=tz),
        id="deadline_alerts", replace_existing=True,
    )
    scheduler.add_job(
        _wrap(job_waiting_followup),
        CronTrigger(hour=9, minute=0, timezone=tz),
        id="waiting_followup", replace_existing=True,
    )
    scheduler.add_job(
        _wrap(job_weekly_summary),
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=tz),
        id="weekly_summary", replace_existing=True,
    )

    _logger.info("Task scheduler started with 5 jobs in timezone %s", tz_name)
    return scheduler


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(state_override: dict | None = None) -> FastAPI:
    """Construct and configure the FastAPI application.

    state_override is used in tests to inject pre-built components without
    triggering the full startup sequence.
    """
    app = FastAPI(title="Mithra", version="0.1.0", lifespan=lifespan)

    if state_override is not None:
        _state.update(state_override)

    # -----------------------------------------------------------------------
    # Auth middleware
    # -----------------------------------------------------------------------

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Unauthenticated paths: health check, dashboard (HA REST sensors),
        # all webhooks (each has its own auth), OAuth routes (/auth/*),
        # and WebSocket upgrades.
        p = request.url.path
        if (
            p == "/api/v1/health"
            or p.startswith("/api/v1/dashboard")
            or p.startswith("/api/v1/routines")   # HA tablet tap — no auth
            or p.startswith("/api/v1/tasks")       # HA dashboard tiles — no auth
            or p.startswith("/api/v1/debug")       # debug tools
            or p.startswith("/api/v1/webhook/")
            or p.startswith("/connect/")
            or p.startswith("/oauth/")
            or p.startswith("/auth/")
            or request.headers.get("upgrade", "").lower() == "websocket"
        ):
            return await call_next(request)

        secret_key = os.environ.get("SECRET_KEY", "")
        if not secret_key:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        token = auth.removeprefix("Bearer ").strip()
        if token != secret_key:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)

    # -----------------------------------------------------------------------
    # Routes
    # -----------------------------------------------------------------------

    @app.get("/api/v1/health")
    async def health():
        """Aggregated health from all components. Unauthenticated."""
        import time as _time
        _hlog = logging.getLogger(__name__ + ".health")

        async def _collect() -> dict[str, Any]:
            components: dict[str, Any] = {}
            statuses = []

            # Agent health — pure Python but wrapped for consistent async pattern
            agent = _state.get("agent")
            if agent:
                _t = _time.time()
                h = await asyncio.to_thread(agent.health)
                _hlog.debug(f"HEALTH_TIMING agent: {_time.time()-_t:.2f}s")
                components["agent"] = h
                statuses.append(h.get("status", "ok"))

            domain_registry = _state.get("domain_registry")
            if domain_registry:
                try:
                    h = domain_registry.health()
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                components["domain_registry"] = h
                statuses.append(h.get("status", "ok"))
            else:
                components["domain_registry"] = {
                    "status": "ok",
                    "registered_domains": [],
                    "domain_count": 0,
                }

            # Synchronous I/O calls — offloaded to thread pool
            llm_router = _state.get("llm_router")
            if llm_router:
                _t = _time.time()
                try:
                    rh = await asyncio.to_thread(llm_router.health)
                    h = {
                        "status": "ok" if rh.litellm_reachable else "degraded",
                        "litellm_reachable": rh.litellm_reachable,
                    }
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                _hlog.debug(f"HEALTH_TIMING llm_router: {_time.time()-_t:.2f}s")
                components["llm_router"] = h
                statuses.append(h.get("status", "ok"))

            domain_router = _state.get("domain_router")
            if domain_router:
                _t = _time.time()
                try:
                    h = await asyncio.to_thread(domain_router.health)
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                _hlog.debug(f"HEALTH_TIMING domain_router: {_time.time()-_t:.2f}s")
                components["domain_router"] = h
                statuses.append(h.get("status", "ok"))

            context_cache = _state.get("context_cache")
            if context_cache:
                _t = _time.time()
                try:
                    h = await asyncio.to_thread(context_cache.health)
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                _hlog.debug(f"HEALTH_TIMING context_cache: {_time.time()-_t:.2f}s")
                components["context_cache"] = h
                statuses.append(h.get("status", "ok"))

            local_adapter = _state.get("local_adapter")
            if local_adapter:
                _t = _time.time()
                try:
                    h = await asyncio.to_thread(local_adapter.health)
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                _hlog.debug(f"HEALTH_TIMING local_adapter: {_time.time()-_t:.2f}s")
                components.setdefault("services", {})["local_storage"] = h

            overall = "ok"
            if any(s == "error" for s in statuses):
                overall = "error"
            elif any(s == "degraded" for s in statuses):
                overall = "degraded"

            return {"status": overall, **components}

        try:
            return await asyncio.wait_for(_collect(), timeout=15.0)
        except asyncio.TimeoutError:
            return {
                "status": "degraded",
                "error": "Health check timed out after 5s",
            }

    @app.post("/api/v1/webhook/telegram")
    async def webhook_telegram(request: Request):
        """Receive a Telegram Update and process it through the full request lifecycle."""
        # Validate Telegram's secret token when configured.
        # Telegram sends this as X-Telegram-Bot-Api-Secret-Token on every update.
        tg_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
        if tg_secret:
            incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if incoming != tg_secret:
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        telegram_input = _state.get("telegram_input")
        if not telegram_input:
            return {"ok": True}

        try:
            update = await request.json()
        except Exception:
            return {"ok": True}

        agent_request = telegram_input.normalise(update, "telegram")
        if agent_request is None:
            return {"ok": True}

        await _process_request(agent_request)
        return {"ok": True}

    @app.post("/api/v1/agent")
    async def agent_endpoint(request: Request):
        """Accept an AgentRequest JSON directly and return AgentResult + ExecutionReceipt."""
        from mithra.agent.interface import AgentRequest as AR, InputChannel

        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        agent = _state.get("agent")
        if not agent:
            raise HTTPException(status_code=503, detail="Agent not initialised")

        agent_req = AR(
            user_id=body.get("user_id", "household"),
            device_id=body.get("device_id", "api"),
            input_channel=InputChannel(body.get("input_channel", "api")),
            text=body.get("text"),
            domain_hint=body.get("domain_hint"),
            request_id=body.get("request_id", ""),
        )

        domain_router = _state.get("domain_router")
        if domain_router:
            try:
                ctx = domain_router.assemble_context(
                    agent_req.user_id,
                    agent_req.device_id,
                    agent_req.domain_hint,
                )
                agent_req.context = ctx
            except Exception:
                pass

        result = agent.run(agent_req)

        engine = _state.get("engine")
        executor = _state.get("executor")
        receipt = None
        if engine and executor:
            try:
                receipt = await engine.process(result, agent_req.user_id, executor)
            except Exception:
                pass

        return {
            "result": {
                "request_id": result.request_id,
                "success": result.success,
                "intent": str(result.intent),
                "domain": result.domain,
                "confidence": result.confidence,
                "response_text": result.response_text,
            },
            "receipt": (
                {
                    "request_id": receipt.request_id,
                    "notification_sent": receipt.notification_sent,
                }
                if receipt
                else None
            ),
        }

    @app.get("/api/v1/dashboard")
    async def get_dashboard(scope: str = "household", user_id: str = ""):
        """Aggregated dashboard across all domains. Unauthenticated — HA REST sensor path."""
        from dataclasses import asdict
        from mithra.domains.context import DomainContext
        from mithra.domains.dashboard import DashboardData

        registry = _state.get("domain_registry")
        if registry is None:
            return JSONResponse({"error": "Domain registry not available"}, status_code=503)

        hid = _state.get("household_id", "")
        uid = user_id or _state.get("default_user_id", "") or hid
        local_adapter = _state.get("local_adapter")

        ctx = DomainContext(
            user_id=uid,
            household_id=hid,
            household_timezone=_state.get("household_timezone", "UTC"),
            oauth_manager=_state.get("oauth_manager"),
            storage_adapter=None,
            file_router=None,
            member_registry=_state.get("member_registry"),
            file_search=None,
            request_id="dashboard",
            channel="ha",
            local_db_path=local_adapter.db_path if local_adapter else "",
        )

        results = []
        for domain_id in registry.get_all_domain_ids():
            domain = registry.get(domain_id)
            if domain is None:
                continue
            try:
                data = domain.get_dashboard_data(ctx, scope_filter=scope, viewer_user_id=uid)
                results.append(asdict(data))
            except Exception as exc:
                _logger.warning("dashboard: domain %s failed: %s", domain_id, exc)
                results.append(asdict(DashboardData(
                    domain=domain_id,
                    summary="Unavailable",
                    error=str(exc),
                )))
        return results

    @app.get("/api/v1/dashboard/today")
    async def get_today_dashboard(request: Request, user_id: str = ""):
        """Aggregated dashboard — alias kept for backward compat. Unauthenticated."""
        from dataclasses import asdict
        from mithra.domains.context import DomainContext
        from mithra.domains.dashboard import DashboardData

        registry = _state.get("domain_registry")
        if registry is None:
            raise HTTPException(status_code=503, detail="Domain registry not available")

        hid = _state.get("household_id", "")
        uid = user_id or _state.get("default_user_id", "") or hid
        local_adapter = _state.get("local_adapter")

        ctx = DomainContext(
            user_id=uid,
            household_id=hid,
            household_timezone=_state.get("household_timezone", "UTC"),
            oauth_manager=_state.get("oauth_manager"),
            storage_adapter=None,
            file_router=None,
            member_registry=_state.get("member_registry"),
            file_search=None,
            request_id="dashboard",
            channel="ha",
            local_db_path=local_adapter.db_path if local_adapter else "",
        )

        results = []
        for domain_id in registry.get_all_domain_ids():
            domain = registry.get(domain_id)
            if domain is None:
                continue
            try:
                data = domain.get_dashboard_data(ctx, scope_filter="household", viewer_user_id=uid)
                results.append(asdict(data))
            except Exception as exc:
                _logger.warning("dashboard/today: domain %s failed: %s", domain_id, exc)
                results.append(asdict(DashboardData(
                    domain=domain_id,
                    summary="Unavailable",
                    error=str(exc),
                )))
        return results

    @app.get("/api/v1/dashboard/{domain_id}")
    async def get_domain_dashboard(domain_id: str, request: Request, user_id: str = "", scope: str = "household"):
        """Return dashboard data for a single domain. Unauthenticated."""
        from dataclasses import asdict
        from mithra.domains.context import DomainContext

        registry = _state.get("domain_registry")
        if registry is None:
            raise HTTPException(status_code=503, detail="Domain registry not available")
        domain = registry.get(domain_id)
        if domain is None:
            raise HTTPException(status_code=404, detail=f"Domain '{domain_id}' not registered")

        hid = _state.get("household_id", "")
        uid = user_id or _state.get("default_user_id", "") or hid
        local_adapter = _state.get("local_adapter")

        ctx = DomainContext(
            user_id=uid,
            household_id=hid,
            household_timezone=_state.get("household_timezone", "UTC"),
            oauth_manager=_state.get("oauth_manager"),
            storage_adapter=None,
            file_router=None,
            member_registry=_state.get("member_registry"),
            file_search=None,
            request_id="dashboard",
            channel="ha",
            local_db_path=local_adapter.db_path if local_adapter else "",
        )

        data = domain.get_dashboard_data(ctx, scope_filter=scope, viewer_user_id=uid)
        return asdict(data)

    # Generic OAuth routes — works for any configured provider in OAUTH_PROVIDERS.
    # Google Cloud Console must have this redirect URI:
    # https://assistant.mithra-app.com/auth/google/callback

    @app.get("/auth/{provider}")
    async def oauth_redirect(provider: str, request: Request, user_id: str = "household"):
        from fastapi.responses import RedirectResponse

        oauth_manager = _state.get("oauth_manager")
        if not oauth_manager or provider not in oauth_manager.available_providers():
            raise HTTPException(status_code=404, detail=f"OAuth provider '{provider}' not configured")
        base_url = os.environ.get("MITHRA_BASE_URL", "https://assistant.mithra-app.com")
        redirect_uri = f"{base_url}/auth/{provider}/callback"
        url = oauth_manager.get_auth_url(provider, user_id, redirect_uri)
        return RedirectResponse(url)

    @app.get("/auth/{provider}/callback")
    async def oauth_callback(provider: str, request: Request, code: str = "", state: str = "household"):
        _log = logging.getLogger(__name__)
        oauth_manager = _state.get("oauth_manager")
        if not oauth_manager or provider not in oauth_manager.available_providers():
            raise HTTPException(status_code=404, detail=f"OAuth provider '{provider}' not configured")

        user_id = state  # state param carries user_id set in get_auth_url
        base_url = os.environ.get("MITHRA_BASE_URL", "https://assistant.mithra-app.com")
        redirect_uri = f"{base_url}/auth/{provider}/callback"
        success = await oauth_manager.handle_callback(provider, code, user_id, redirect_uri)

        if success:
            handler = _state.get("telegram_handler")
            if handler:
                try:
                    await handler.notify_user(user_id, f"✅ {provider.title()} connected! You're all set.")
                except Exception as exc:
                    _log.warning("OAuth callback: Telegram notify failed: %s", exc)
            return {"ok": True, "provider": provider, "user_id": user_id}

        raise HTTPException(status_code=400, detail="OAuth code exchange failed")

    # -----------------------------------------------------------------------
    # Routine routes — HA tablet tap, no auth required
    # -----------------------------------------------------------------------

    @app.get("/api/v1/routines/today")
    async def get_routines_today(scope: str = "household"):
        """Return today's routine completion state for the HA dashboard.

        scope: household|personal|all
        """
        import sqlite3
        import pytz
        from datetime import datetime as _dt

        local_adapter = _state.get("local_adapter")
        if not local_adapter:
            return JSONResponse({"error": "Storage not available"}, status_code=503)

        db_path = local_adapter.db_path
        hid = _state.get("household_id", "")
        household_tz = _state.get("household_timezone", "UTC")
        tz = pytz.timezone(household_tz)
        today = _dt.now(tz).strftime("%Y-%m-%d")

        scope_clause = ""
        if scope == "household":
            scope_clause = "AND r.scope='household'"
        elif scope == "personal":
            scope_clause = "AND r.scope='personal'"

        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""SELECT r.id, r.title, r.scope, r.reset_time,
                               CASE WHEN rc.id IS NOT NULL THEN 1 ELSE 0 END AS done,
                               rc.completed_by,
                               rc.completed_at
                        FROM routines r
                        LEFT JOIN routine_completions rc
                            ON rc.routine_id = r.id
                            AND rc.completion_date = ?
                            AND rc.household_id = r.household_id
                        WHERE r.household_id = ?
                          AND r.active = 1
                          {scope_clause}
                        ORDER BY r.created_at ASC""",
                    (today, hid),
                ).fetchall()
            routines = [dict(r) for r in rows]
            done_count = sum(1 for r in routines if r["done"])
            return JSONResponse({
                "date": today,
                "routines": routines,
                "done_count": done_count,
                "total": len(routines),
            })
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/v1/routines/{routine_id}/complete")
    async def complete_routine_by_id(routine_id: str, request: Request):
        """Mark a routine done via HTTP tap (HA tablet).

        Body: {"user_id": "usr_xxx"} — optional, falls back to default_user_id.
        Returns {"status": "done"|"already_done", "routine": title, "date": today}.
        """
        import sqlite3
        import pytz
        from datetime import datetime as _dt
        from mithra.domains.tasks.tools import new_routine_completion_id, now_iso

        local_adapter = _state.get("local_adapter")
        if not local_adapter:
            return JSONResponse({"error": "Storage not available"}, status_code=503)

        db_path = local_adapter.db_path
        hid = _state.get("household_id", "")
        household_tz = _state.get("household_timezone", "UTC")
        tz = pytz.timezone(household_tz)
        today = _dt.now(tz).strftime("%Y-%m-%d")
        now = now_iso()

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        user_id = body.get("user_id") or _state.get("default_user_id", "")

        try:
            with sqlite3.connect(db_path) as conn:
                routine = conn.execute(
                    "SELECT id, title FROM routines WHERE id=? AND household_id=? AND active=1",
                    (routine_id, hid),
                ).fetchone()
                if not routine:
                    return JSONResponse({"error": "Routine not found"}, status_code=404)

                try:
                    conn.execute(
                        """INSERT INTO routine_completions
                           (id, routine_id, household_id, completed_by, completion_date, completed_at)
                           VALUES (?,?,?,?,?,?)""",
                        (new_routine_completion_id(), routine_id, hid, user_id, today, now),
                    )
                    return JSONResponse({"status": "done", "routine": routine[1], "date": today})
                except sqlite3.IntegrityError:
                    return JSONResponse({"status": "already_done", "routine": routine[1], "date": today})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # -----------------------------------------------------------------------
    # Tasks API routes (T8) — HA dashboard tiles, no auth required
    # -----------------------------------------------------------------------

    @app.get("/api/v1/tasks/today")
    async def get_tasks_today():
        """Tasks for today: urgent + high priority + overdue.

        Returns {date, tasks, count, urgent_count} per ADR 11.11.
        """
        import sqlite3 as _sqlite3
        import pytz as _pytz
        from datetime import datetime as _dt

        local_adapter = _state.get("local_adapter")
        if not local_adapter:
            return JSONResponse({"error": "Storage not available"}, status_code=503)

        db_path = local_adapter.db_path
        hid = _state.get("household_id", "")
        tz_name = _state.get("household_timezone", "America/Chicago")
        today = _dt.now(_pytz.timezone(tz_name)).strftime("%Y-%m-%d")

        try:
            with _sqlite3.connect(db_path) as conn:
                conn.row_factory = _sqlite3.Row
                tasks = conn.execute(
                    """SELECT * FROM tasks
                       WHERE household_id=?
                         AND status NOT IN ('done','cancelled','snoozed')
                         AND (priority='urgent' OR priority='high' OR due_date<=?)
                       ORDER BY
                           CASE priority WHEN 'urgent' THEN 1 WHEN 'high' THEN 2 ELSE 3 END,
                           due_date ASC NULLS LAST
                       LIMIT 20""",
                    (hid, today),
                ).fetchall()
            rows = [dict(t) for t in tasks]
            urgent_count = sum(1 for t in rows if t.get("priority") == "urgent")
            return JSONResponse({"date": today, "tasks": rows, "count": len(rows), "urgent_count": urgent_count})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/v1/tasks/inbox")
    async def get_tasks_inbox():
        """Unprocessed tasks (status=inbox)."""
        import sqlite3 as _sqlite3

        local_adapter = _state.get("local_adapter")
        if not local_adapter:
            return JSONResponse({"error": "Storage not available"}, status_code=503)

        db_path = local_adapter.db_path
        hid = _state.get("household_id", "")

        try:
            with _sqlite3.connect(db_path) as conn:
                conn.row_factory = _sqlite3.Row
                tasks = conn.execute(
                    "SELECT * FROM tasks WHERE household_id=? AND status='inbox' "
                    "ORDER BY created_at ASC",
                    (hid,),
                ).fetchall()
            rows = [dict(t) for t in tasks]
            return JSONResponse({"tasks": rows, "count": len(rows)})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/v1/tasks/overdue")
    async def get_tasks_overdue():
        """Tasks past their deadline, not done or cancelled."""
        import sqlite3 as _sqlite3
        import pytz as _pytz
        from datetime import datetime as _dt

        local_adapter = _state.get("local_adapter")
        if not local_adapter:
            return JSONResponse({"error": "Storage not available"}, status_code=503)

        db_path = local_adapter.db_path
        hid = _state.get("household_id", "")
        tz_name = _state.get("household_timezone", "America/Chicago")
        today = _dt.now(_pytz.timezone(tz_name)).strftime("%Y-%m-%d")

        try:
            with _sqlite3.connect(db_path) as conn:
                conn.row_factory = _sqlite3.Row
                tasks = conn.execute(
                    "SELECT * FROM tasks WHERE household_id=? AND due_date<? "
                    "AND status NOT IN ('done','cancelled','snoozed') "
                    "ORDER BY due_date ASC",
                    (hid, today),
                ).fetchall()
            rows = [dict(t) for t in tasks]
            return JSONResponse({"date": today, "tasks": rows, "count": len(rows)})
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/v1/tasks/{task_id}/complete")
    async def complete_task_by_id(task_id: str):
        """Mark a task done via HTTP tap (HA tablet). No LLM — direct SQLite UPDATE.

        Idempotent: returns {"status": "done"|"already_done"} on success.
        Returns 404 when task_id is not found in this household.
        """
        import sqlite3 as _sql
        from mithra.domains.tasks.tools import _complete_task_by_id

        local_adapter = _state.get("local_adapter")
        if not local_adapter:
            return JSONResponse({"error": "Storage not available"}, status_code=503)

        hid = _state.get("household_id", "")

        try:
            with _sql.connect(local_adapter.db_path) as conn:
                result = _complete_task_by_id(conn, task_id, hid)
        except Exception as exc:
            _logger.error("complete_task_by_id DB error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

        if result["status"] == "not_found":
            return JSONResponse({"error": "Task not found"}, status_code=404)

        return JSONResponse({"status": result["status"], "task": result["title"]})

    _DEBUG_MODE = os.environ.get("MITHRA_DEBUG", "").lower() == "true"

    if _DEBUG_MODE:
        @app.get("/api/v1/debug/scheduler/{job_name}")
        async def debug_run_scheduler_job(job_name: str):
            """Trigger a scheduler job immediately. Only available when MITHRA_DEBUG=true."""
            from datetime import datetime, timezone
            from mithra.scheduler.task_scheduler import (
                job_morning_briefing,
                job_deadline_alerts,
                job_waiting_followup,
                job_weekly_summary,
                job_auto_promote,
            )
            jobs = {
                "morning_briefing": job_morning_briefing,
                "deadline_alerts":  job_deadline_alerts,
                "waiting_followup": job_waiting_followup,
                "weekly_summary":   job_weekly_summary,
                "auto_promote":     job_auto_promote,
            }
            if job_name not in jobs:
                return JSONResponse(
                    {"error": f"Unknown job: {job_name}", "available": list(jobs.keys())},
                    status_code=404,
                )
            try:
                jobs[job_name](_state)
                return JSONResponse({
                    "status": "ok",
                    "job": job_name,
                    "ran_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)

    # Webhook routes — Telegram and future inbound webhooks.
    from mithra.api.routes.webhooks import router as _webhooks_router
    app.include_router(_webhooks_router, prefix="/api/v1")

    return app


# ---------------------------------------------------------------------------
# Multi-turn capture helpers
# ---------------------------------------------------------------------------

def _is_capture_response(text: str) -> bool:
    """Return True if the entire message is a capture enrichment answer, not a new intent.

    Uses fullmatch patterns so the entire text must be a date/priority expression.
    Single-word day names, priority levels, and short date phrases qualify.
    Sentences that happen to contain a date word are correctly rejected.
    """
    if not text:
        return False
    import re
    lower = text.lower().strip()

    # Single-word tokens
    _SINGLE = frozenset({
        "skip", "done", "no", "nope", "nothing", "finish",
        "urgent", "high", "normal", "someday",
        "today", "tomorrow",
        "monday", "tuesday", "wednesday", "thursday",
        "friday", "saturday", "sunday",
    })
    if lower in _SINGLE:
        return True

    # ISO date
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lower):
        return True

    # Two-word date phrases: "next friday", "this week", "next month"
    _DAY_OR_PERIOD = (
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"week|month|weekend"
    )
    if re.fullmatch(rf"(next|this) ({_DAY_OR_PERIOD})", lower):
        return True

    # "in N days/weeks"
    if re.fullmatch(r"in \d+ (day|days|week|weeks)", lower):
        return True

    # Natural-language month + day: "July 15th", "Dec 3", "January 1st"
    _MONTHS = (
        r"january|february|march|april|may|june|july|august|"
        r"september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
    )
    if re.fullmatch(rf"({_MONTHS}) \d{{1,2}}(st|nd|rd|th)?", lower):
        return True

    return False


def _update_capture_field(db_path: str, task_id: str, field: str, value: str) -> None:
    """Update a single task field during a capture flow."""
    import sqlite3
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE tasks SET {field}=?, updated_at=? WHERE id=?",
            (value, now, task_id),
        )


def _finalize_capture_task(db_path: str, task_id: str) -> None:
    """Promote task from inbox to active once capture is complete."""
    import sqlite3
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET status='active', updated_at=? WHERE id=? AND status='inbox'",
            (now, task_id),
        )


async def _check_capture_context(agent_request, db_path: str, handler) -> bool:
    """Handle a reply that belongs to an active multi-turn capture flow.

    Returns True if the reply was consumed here (caller should skip agent.run).
    Returns False if no active capture or user changed topic (caller proceeds normally).
    """
    from mithra.domains.tasks.tools import (
        _clear_capture_state,
        _get_capture_state,
        _next_capture_question,
        _parse_due_date,
        _store_capture_state,
    )

    logger = logging.getLogger(__name__)
    text = (agent_request.text or "").strip()
    if not text:
        return False

    state = _get_capture_state(db_path, agent_request.user_id)
    if not state:
        return False

    task_id = state["task_id"]
    step = state["step"]
    text_lower = text.lower()

    # Explicit skip/finish — clear state and confirm
    if text_lower in {"skip", "done", "finish", "that's it", "no", "nope", "nothing"}:
        _clear_capture_state(db_path, agent_request.user_id)
        _finalize_capture_task(db_path, task_id)
        if handler:
            await handler.send(agent_request, "Got it, task saved.")
        return True

    # Not a capture-like answer for structured steps (due_date/priority) → topic change.
    # Notes step bypasses this check — any text that isn't a command is a valid note.
    if step != "notes" and not _is_capture_response(text):
        _clear_capture_state(db_path, agent_request.user_id)
        return False

    # Apply the answer to the current step
    response_text = ""
    next_step: str | None = None
    _STEP_ORDER = {"due_date": "priority", "priority": "notes", "notes": "done"}

    if step == "due_date":
        parsed = _parse_due_date(text)
        if parsed:
            _update_capture_field(db_path, task_id, "due_date", parsed)
            next_step = _STEP_ORDER["due_date"]
        else:
            response_text = f"Couldn't parse that as a date. {_next_capture_question(step)}"
    elif step == "priority":
        if text_lower in {"urgent", "high", "normal", "someday"}:
            _update_capture_field(db_path, task_id, "priority", text_lower)
            next_step = _STEP_ORDER["priority"]
        else:
            response_text = f"Priority should be urgent / high / normal / someday. {_next_capture_question(step)}"
    elif step == "notes":
        _update_capture_field(db_path, task_id, "notes", text)
        next_step = _STEP_ORDER["notes"]

    if response_text:
        # Validation failed — re-ask the same question
        if handler:
            await handler.send(agent_request, response_text)
        return True

    if next_step == "done" or next_step is None:
        _clear_capture_state(db_path, agent_request.user_id)
        _finalize_capture_task(db_path, task_id)
        response_text = "Task saved!"
    else:
        _store_capture_state(db_path, agent_request.user_id, task_id, next_step, state["context"])
        response_text = _next_capture_question(next_step)

    logger.info("Capture step %s → %s for task %s", step, next_step, task_id)
    if handler:
        await handler.send(agent_request, response_text)
    return True


# ---------------------------------------------------------------------------
# Step-aware capture gate
# ---------------------------------------------------------------------------

_NEW_COMMAND_PREFIXES: tuple[str, ...] = (
    "add ", "show ", "list ", "create ",
    "what ", "how ", "when ", "cancel ",
    "delete ", "mark ", "remind ",
)


async def _apply_milestone_capture(agent_request, state: dict, db_path: str, handler) -> None:
    """Parse comma-separated milestone text and insert milestones into the project."""
    import sqlite3
    from mithra.domains.tasks.tools import _clear_capture_state, new_milestone_id, now_iso

    logger = logging.getLogger(__name__)
    text = (agent_request.text or "").strip()
    project_id = state["task_id"]  # conversation_state.last_task_id holds project_id
    user_id = agent_request.user_id

    milestone_titles = [m.strip() for m in text.split(",") if m.strip()]
    if not milestone_titles:
        _clear_capture_state(db_path, user_id)
        if handler:
            await handler.send(agent_request, "Project saved ✓")
        return

    now = now_iso()
    try:
        with sqlite3.connect(db_path) as conn:
            proj_row = conn.execute(
                "SELECT household_id, title FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if not proj_row:
                _clear_capture_state(db_path, user_id)
                return
            household_id, proj_title = proj_row

            row = conn.execute(
                "SELECT MAX(order_index) FROM milestones WHERE project_id=?", (project_id,)
            ).fetchone()
            start_index = (row[0] or 0) + 1

            for i, title in enumerate(milestone_titles, start_index):
                conn.execute(
                    """INSERT INTO milestones
                       (id, project_id, household_id, title, order_index, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (new_milestone_id(), project_id, household_id, title, i, "pending", now, now),
                )
    except Exception as exc:
        logger.error("_apply_milestone_capture DB error", exc_info=True)
        _clear_capture_state(db_path, user_id)
        return

    _clear_capture_state(db_path, user_id)
    ms_lines = "\n".join(f"  {i}. {t}" for i, t in enumerate(milestone_titles, start_index))
    response = f"{len(milestone_titles)} milestone(s) added to {proj_title}:\n{ms_lines}"
    logger.info("milestone_capture: %d milestones added to %s", len(milestone_titles), project_id)
    if handler:
        await handler.send(agent_request, response)


async def _check_and_apply_capture(agent_request, db_path: str, handler) -> bool:
    """Decide whether to intercept a message as a capture follow-up, then apply it.

    Step-aware routing:
      - milestone step: intercept comma list or skip; new commands fall through
      - notes step: intercept any text that isn't a clear new command
      - due_date / priority steps: intercept only if _is_capture_response matches

    Returns True if the reply was consumed (caller should return early).
    Returns False if the pipeline should continue normally.
    """
    from mithra.domains.tasks.tools import _clear_capture_state, _get_capture_state

    text = (agent_request.text or "").strip()
    if not text:
        return False

    state = _get_capture_state(db_path, agent_request.user_id)
    if not state:
        return False

    step = state["step"]
    text_lower = text.lower()

    if step == "milestone":
        if text_lower in {"skip", "no", "none", "done"}:
            _clear_capture_state(db_path, agent_request.user_id)
            if handler:
                await handler.send(agent_request, "Project saved ✓")
            return True
        if any(text_lower.startswith(p) for p in _NEW_COMMAND_PREFIXES):
            _clear_capture_state(db_path, agent_request.user_id)
            return False
        await _apply_milestone_capture(agent_request, state, db_path, handler)
        return True

    if step == "notes":
        # New commands start a fresh pipeline; everything else is a note
        if any(text_lower.startswith(p) for p in _NEW_COMMAND_PREFIXES):
            _clear_capture_state(db_path, agent_request.user_id)
            return False
        return await _check_capture_context(agent_request, db_path, handler)

    # due_date and priority: only intercept recognisable capture expressions
    if _is_capture_response(text):
        return await _check_capture_context(agent_request, db_path, handler)

    # Text doesn't match structured capture pattern → user changed topic
    _clear_capture_state(db_path, agent_request.user_id)
    return False


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------

async def _process_request(agent_request) -> None:
    """Execute full request lifecycle for a normalised AgentRequest.

    Gateway is a channel-agnostic orchestrator: receive → context → agent →
    engine → respond. All channel-specific logic lives in the adapter layer.
    """
    logger = logging.getLogger(__name__)
    logger.info("Processing: user=%s text=%r", agent_request.user_id, agent_request.text)

    handler = _state.get("telegram_handler")

    # 0. Capture context check — BEFORE context assembly and classify_intent.
    #    Routes to step-aware gate: notes step intercepts any text that isn't a
    #    new command; due_date/priority steps require a recognised capture expression.
    if agent_request.text:
        local_adapter = _state.get("local_adapter")
        db_path = getattr(local_adapter, "db_path", None) if local_adapter else None
        if db_path:
            try:
                handled = await _check_and_apply_capture(agent_request, db_path, handler)
                if handled:
                    return
            except Exception as exc:
                logger.warning("Capture context check failed: %s", exc)

    # 1. Delegate Telegram-specific signals to the handler
    if handler and await handler.handle_signal(agent_request):
        return

    # 2. Assemble context
    domain_router = _state.get("domain_router")
    if domain_router:
        try:
            ctx = domain_router.assemble_context(
                agent_request.user_id,
                agent_request.device_id,
                agent_request.domain_hint,
            )
            agent_request.context = ctx
            logger.info("Context assembled: %d priority domains", len(ctx.priority_domains))
        except Exception as exc:
            logger.error("Context assembly failed: %s", exc, exc_info=True)

    # 3. Run agent
    agent = _state.get("agent")
    if not agent:
        logger.error("No agent in state")
        return
    try:
        result = agent.run(agent_request)
        logger.info(
            "Agent result: success=%s intent=%s response=%r",
            result.success, result.intent, (result.response_text or "")[:60],
        )
    except Exception as exc:
        logger.error("Agent.run failed: %s", exc, exc_info=True)
        return

    # 4. Run autonomy engine
    engine = _state.get("engine")
    executor = _state.get("executor")
    receipt = None
    if engine and executor:
        try:
            receipt = await engine.process(result, agent_request.user_id, executor)
            logger.info(
                "Engine processed: executed=%d pending=%d",
                len(receipt.executed), len(receipt.pending),
            )
        except Exception as exc:
            logger.error("Engine.process failed: %s", exc, exc_info=True)

    # 5. Engine response takes priority over agent response
    if receipt and receipt.response_text:
        result.response_text = receipt.response_text

    # 6. Send response — all channel knowledge stays in the handler
    if result.response_text and handler:
        actions = None
        if receipt and receipt.pending:
            actions = handler.make_confirmation_buttons(receipt.pending[0])
        elif (
            result.error
            and result.error.code.value == "token_expired"
            and hasattr(handler, "make_reconnect_button")
        ):
            actions = [handler.make_reconnect_button(agent_request.user_id)]
        await handler.send(agent_request, result.response_text, actions=actions)


# ---------------------------------------------------------------------------
# Module-level app instance (for uvicorn / docker-compose)
# ---------------------------------------------------------------------------

app = create_app()
