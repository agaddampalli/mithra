"""
FastAPI gateway -- lifecycle orchestrator and single HTTP entry point for Mithra.

Startup sequence (FastAPI lifespan):
  1. Load HouseholdConfig from household.yaml
  2. Build StorageRouter (LocalStorageAdapter + OneDriveAdapter +
     EncryptedStorageAdapter wired correctly)
  3. Build ContextCache
  4. Build DomainMemoryRouter (DefaultDomainMemoryRouter)
  5. Build LLMRouter (LiteLLMRouterAdapter)
  6. Build AutononomyEngine (DefaultAutonomyEngine)
  7. Build ActionExecutorRegistry
  8. Build AgentInterface (LangGraphAdapter)
  9. Build TelegramInputAdapter + TelegramOutputAdapter
 10. Register output adapters with ActionExecutorRegistry

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Shared app state
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {}


def get_state() -> dict[str, Any]:
    return _state


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

def _load_household_config(path: str = "household.yaml") -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _build_storage_router(household_config: dict, tmp_dir: str | None = None):
    """Wire the full StorageRouter with local + OneDrive adapters."""
    from mithra.storage.local import LocalStorageAdapter
    from mithra.storage.encryption import EncryptedStorageAdapter, AESGCMCipher
    from mithra.storage.router import StorageRouter

    storage_dir = tmp_dir or os.environ.get("MITHRA_STORAGE_DIR", "/data/mithra/files")
    db_path = os.path.join(tmp_dir or os.environ.get("MITHRA_DB_DIR", "/data/mithra"), "mithra.db")

    local = LocalStorageAdapter(storage_dir, db_path)

    enc_key = os.environ.get("MITHRA_ENCRYPTION_KEY", "")
    if enc_key:
        cipher = AESGCMCipher(enc_key)
        encrypted_local = EncryptedStorageAdapter(local, cipher)
    else:
        encrypted_local = local  # dev fallback

    # OneDrive adapter -- fallback to local if credentials not configured
    onedrive_client_id = os.environ.get("ONEDRIVE_CLIENT_ID", "")
    if onedrive_client_id:
        from mithra.storage.onedrive import OneDriveAdapter
        onedrive = OneDriveAdapter(
            client_id=onedrive_client_id,
            client_secret=os.environ.get("ONEDRIVE_CLIENT_SECRET", ""),
            tenant_id=os.environ.get("ONEDRIVE_TENANT_ID", ""),
        )
        encrypted_onedrive = onedrive  # add encryption when needed
    else:
        onedrive = local
        encrypted_onedrive = encrypted_local

    return StorageRouter(
        local_adapter=local,
        onedrive_adapter=onedrive,
        encrypted_local_adapter=encrypted_local,
        encrypted_onedrive_adapter=encrypted_onedrive,
    ), local, cipher if enc_key else None


def _build_domain_router(storage_router, household_config: dict):
    from mithra.routing.domain_router import DefaultDomainMemoryRouter
    from mithra.memory.context_cache import ContextCache
    from mithra.memory.structured_store import StructuredStore

    db_dir = os.environ.get("MITHRA_DB_DIR", "/data/mithra")
    cache_db_path = os.path.join(db_dir, "context_cache.db")
    structured_db_path = os.path.join(db_dir, "structured_store.db")
    cache = ContextCache(cache_db_path)
    structured_store = StructuredStore(structured_db_path)
    domain_router = DefaultDomainMemoryRouter(
        storage_router=storage_router,
        context_cache=cache,
        structured_store=structured_store,
        household_config=household_config,
    )
    return domain_router, cache


def _build_llm_router(local_adapter):
    from mithra.routing.llm_router import LiteLLMRouterAdapter
    from mithra.storage.sanitiser import DataSanitiser

    base_url = os.environ.get("LITELLM_BASE_URL", "http://llm-router:4000")
    sanitiser = DataSanitiser()
    return LiteLLMRouterAdapter(
        base_url=base_url,
        sanitiser=sanitiser,
        storage_adapter=local_adapter,
    )


def _build_autonomy(storage_router, local_adapter, household_id: str = ""):
    from mithra.autonomy.default_engine import DefaultAutonomyEngine
    from mithra.autonomy.executor import ActionExecutorRegistry
    from mithra.autonomy.action_log import ActionLog
    from mithra.autonomy.dead_letter import DeadLetterQueue

    action_log = ActionLog(local_adapter, household_id=household_id)
    dead_letter_queue = DeadLetterQueue(local_adapter, household_id=household_id)
    executor = ActionExecutorRegistry()
    config_path = os.environ.get(
        "AUTONOMY_CONFIG_PATH", "services/autonomy/autonomy.yaml"
    )
    engine = DefaultAutonomyEngine(
        storage_adapter=local_adapter,
        action_log=action_log,
        config_path=config_path,
        executor=executor,
    )
    return engine, executor, action_log, dead_letter_queue


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    household_config = _load_household_config()
    _state["household_config"] = household_config
    household_id = household_config.get("household_id", "default")
    _state["household_id"] = household_id

    storage_router, local_adapter, cipher = _build_storage_router(household_config)
    _state["storage_router"] = storage_router
    _state["local_adapter"] = local_adapter

    domain_router, cache = _build_domain_router(storage_router, household_config)
    _state["domain_router"] = domain_router
    _state["context_cache"] = cache

    llm_router = _build_llm_router(local_adapter)
    _state["llm_router"] = llm_router

    engine, executor, action_log, dead_letter_queue = _build_autonomy(
        storage_router, local_adapter, household_id=household_id
    )
    _state["engine"] = engine
    _state["executor"] = executor
    _state["action_log"] = action_log
    _state["dead_letter_queue"] = dead_letter_queue

    from mithra.agent.langgraph_adapter import LangGraphAdapter
    agent = LangGraphAdapter(
        llm_router=llm_router,
        storage_router=storage_router,
    )
    _state["agent"] = agent

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if bot_token:
        from mithra.adapters.messaging.telegram_input import (
            TelegramInputAdapter,
            UserIdentityResolver,
        )
        from mithra.adapters.messaging.telegram_output import TelegramOutputAdapter

        identity_resolver = UserIdentityResolver(
            household_config,
            storage_adapter=local_adapter,
        )
        _state["identity_resolver"] = identity_resolver
        from mithra.routing.session import ConversationSessionManager
        session_mgr = ConversationSessionManager(
            local_adapter,
            known_user_ids=[m["user_id"] for m in household_config.get("members", [])],
            household_id=household_id,
        )

        telegram_input = TelegramInputAdapter(
            bot_token=bot_token,
            webhook_secret=webhook_secret,
            session_manager=session_mgr,
            identity_resolver=identity_resolver,
            on_request=_process_request,
        )
        telegram_output = TelegramOutputAdapter(bot_token=bot_token)

        executor.register("telegram_reply", telegram_output)
        executor.register_notify("telegram", telegram_output)

        _state["telegram_input"] = telegram_input
        _state["telegram_output"] = telegram_output
        _state["session_mgr"] = session_mgr

        asyncio.create_task(telegram_input.start())

    base_url = os.environ.get("MITHRA_BASE_URL", "https://assistant.mithra-app.com")

    nango_key = os.environ.get("NANGO_SECRET_KEY", "")
    if nango_key:
        from mithra.integrations.nango_client import NangoClient
        nango = NangoClient(
            nango_url=os.environ.get("NANGO_URL", "http://nango:8080"),
            secret_key=nango_key,
        )
        _state["nango"] = nango

    if nango_key:
        from mithra.adapters.output.calendar import CalendarAdapter
        calendar_adapter = CalendarAdapter(
            nango_client=_state.get("nango"),
            household_id=household_id,
        )
        _state["calendar_adapter"] = calendar_adapter
        executor.register("calendar_write", calendar_adapter)

    # Wire TelegramSignalHandler after all components are ready
    if _state.get("telegram_output"):
        from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler
        telegram_handler = TelegramSignalHandler(
            telegram_output=_state["telegram_output"],
            engine=_state.get("engine"),
            executor=_state.get("executor"),
            household_config=household_config,
            nango=_state.get("nango"),
            base_url=base_url,
            identity_resolver=_state.get("identity_resolver"),
        )
        _state["telegram_handler"] = telegram_handler

    yield

    # Shutdown
    telegram_input = _state.get("telegram_input")
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
        # Health, Telegram webhook, connect page, and all OAuth routes are unauthenticated.
        p = request.url.path
        if (
            p == "/api/v1/health"
            or p == "/api/v1/webhook/telegram"
            or p.startswith("/connect/")
            or p.startswith("/oauth/")
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
        components: dict[str, Any] = {}
        statuses = []

        agent = _state.get("agent")
        if agent:
            h = agent.health()
            components["agent"] = h
            statuses.append(h.get("status", "ok"))

        llm_router = _state.get("llm_router")
        if llm_router:
            try:
                rh = llm_router.health()
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
                h = domain_router.health()
            except Exception as exc:
                h = {"status": "error", "error": str(exc)}
            components["domain_router"] = h
            statuses.append(h.get("status", "ok"))

        context_cache = _state.get("context_cache")
        if context_cache:
            try:
                h = context_cache.health()
            except Exception as exc:
                h = {"status": "error", "error": str(exc)}
            components["context_cache"] = h
            statuses.append(h.get("status", "ok"))

        local_adapter = _state.get("local_adapter")
        if local_adapter:
            try:
                h = local_adapter.health()
            except Exception as exc:
                h = {"status": "error", "error": str(exc)}
            components.setdefault("services", {})["local_storage"] = h

        overall = "ok"
        if any(s == "error" for s in statuses):
            overall = "error"
        elif any(s == "degraded" for s in statuses):
            overall = "degraded"

        return {"status": overall, **components}

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

    @app.get("/connect/google")
    async def connect_google(token: str = ""):
        """Serve the Google OAuth connect page.

        User taps a Telegram link → lands here → Nango Frontend SDK opens a popup
        that goes to /oauth/connect/google-calendar (proxied to Nango internally).
        host must be the public gateway URL so the browser can reach it.
        """
        from fastapi.responses import HTMLResponse
        base_url = os.environ.get("MITHRA_BASE_URL", "https://assistant.mithra-app.com").rstrip("/")
        if not token:
            return HTMLResponse("<p>Invalid link — missing session token.</p>", status_code=400)
        return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Connect Google — Mithra</title>
  <style>
    body {{ font-family: sans-serif; text-align: center; padding: 60px 20px; color: #333; }}
    p {{ font-size: 1.1rem; }}
  </style>
</head>
<body>
  <p>Connecting your Google account&hellip;</p>
  <script type="module">
    import Nango from 'https://cdn.jsdelivr.net/npm/@nangohq/frontend/dist/index.js';
    const nango = new Nango({{
      connectSessionToken: {token!r},
      host: {base_url!r},
    }});
    nango.auth('google-calendar')
      .then(() => {{
        document.body.innerHTML =
          '<h2>&#x2705; Google Connected!</h2><p>You can close this tab and return to Telegram.</p>';
      }})
      .catch(err => {{
        document.body.innerHTML =
          '<p>&#x274C; Connection failed: ' + err.message + '<br>Please try again.</p>';
      }});
  </script>
</body>
</html>""")

    @app.api_route("/oauth/{path:path}", methods=["GET", "POST"])
    async def nango_oauth_proxy(request: Request, path: str):
        """Proxy all Nango OAuth routes to the internal Nango container.

        /oauth/connect/{provider} — SDK popup lands here, Nango redirects to Google.
        /oauth/callback           — Google redirects here, Nango exchanges code for tokens.
        No auth header: these are browser-facing endpoints.
        """
        from fastapi.responses import Response
        import httpx
        nango_url = os.environ.get("NANGO_URL", "http://nango:8080")
        skip = {"host", "content-length", "transfer-encoding"}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}
        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method=request.method,
                url=f"{nango_url}/oauth/{path}",
                params=dict(request.query_params),
                headers=headers,
                content=await request.body(),
                follow_redirects=False,
                timeout=30,
            )
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() != "transfer-encoding"}
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

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
