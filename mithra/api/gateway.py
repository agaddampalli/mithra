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

    telegram_input = _state.get("telegram_input")
    if telegram_input:
        asyncio.create_task(telegram_input.start())

    yield

    if telegram_input:
        await telegram_input.stop()


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
        async def _collect() -> dict[str, Any]:
            components: dict[str, Any] = {}
            statuses = []

            # Pure-Python health checks — run synchronously
            agent = _state.get("agent")
            if agent:
                h = agent.health()
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
                try:
                    rh = await asyncio.to_thread(llm_router.health)
                    h = {
                        "status": "ok" if rh.litellm_reachable else "degraded",
                        "litellm_reachable": rh.litellm_reachable,
                    }
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                components["llm_router"] = h
                statuses.append(h.get("status", "ok"))

            domain_router = _state.get("domain_router")
            if domain_router:
                try:
                    h = await asyncio.to_thread(domain_router.health)
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                components["domain_router"] = h
                statuses.append(h.get("status", "ok"))

            context_cache = _state.get("context_cache")
            if context_cache:
                try:
                    h = await asyncio.to_thread(context_cache.health)
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                components["context_cache"] = h
                statuses.append(h.get("status", "ok"))

            local_adapter = _state.get("local_adapter")
            if local_adapter:
                try:
                    h = await asyncio.to_thread(local_adapter.health)
                except Exception as exc:
                    h = {"status": "error", "error": str(exc)}
                components.setdefault("services", {})["local_storage"] = h

            overall = "ok"
            if any(s == "error" for s in statuses):
                overall = "error"
            elif any(s == "degraded" for s in statuses):
                overall = "degraded"

            return {"status": overall, **components}

        try:
            return await asyncio.wait_for(_collect(), timeout=5.0)
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

    @app.get("/api/v1/dashboard/{domain_id}")
    async def get_domain_dashboard(domain_id: str, request: Request, user_id: str = ""):
        """Return dashboard data for a single domain. Authenticated."""
        registry = _state.get("domain_registry")
        if registry is None:
            raise HTTPException(status_code=503, detail="Domain registry not available")
        domain = registry.get(domain_id)
        if domain is None:
            raise HTTPException(status_code=404, detail=f"Domain '{domain_id}' not registered")
        hid = _state.get("household_id", "")
        uid = user_id or hid
        from dataclasses import asdict
        data = await domain.get_dashboard_data(user_id=uid, household_id=hid)
        return asdict(data)

    @app.get("/api/v1/dashboard/today")
    async def get_today_dashboard(request: Request, user_id: str = ""):
        """Return aggregated dashboard data across all registered domains. Authenticated."""
        registry = _state.get("domain_registry")
        if registry is None:
            raise HTTPException(status_code=503, detail="Domain registry not available")
        hid = _state.get("household_id", "")
        uid = user_id or hid
        from dataclasses import asdict
        results = []
        for domain_id in registry.get_all_domain_ids():
            domain = registry.get(domain_id)
            if domain is None:
                continue
            try:
                data = await domain.get_dashboard_data(user_id=uid, household_id=hid)
                results.append(asdict(data))
            except Exception:
                pass
        return results

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

    # Webhook routes — Telegram and future inbound webhooks.
    from mithra.api.routes.webhooks import router as _webhooks_router
    app.include_router(_webhooks_router, prefix="/api/v1")

    return app


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

    # 1. Delegate Telegram-specific signals to the handler
    handler = _state.get("telegram_handler")
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
        await handler.send(agent_request, result.response_text, actions=actions)


# ---------------------------------------------------------------------------
# Module-level app instance (for uvicorn / docker-compose)
# ---------------------------------------------------------------------------

app = create_app()
