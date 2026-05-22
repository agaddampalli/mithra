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


def _build_autonomy(storage_router, local_adapter):
    from mithra.autonomy.default_engine import DefaultAutonomyEngine
    from mithra.autonomy.executor import ActionExecutorRegistry
    from mithra.autonomy.action_log import ActionLog

    action_log = ActionLog(local_adapter)
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
    return engine, executor, action_log


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    household_config = _load_household_config()
    _state["household_config"] = household_config

    storage_router, local_adapter, cipher = _build_storage_router(household_config)
    _state["storage_router"] = storage_router
    _state["local_adapter"] = local_adapter

    domain_router, cache = _build_domain_router(storage_router, household_config)
    _state["domain_router"] = domain_router
    _state["context_cache"] = cache

    llm_router = _build_llm_router(local_adapter)
    _state["llm_router"] = llm_router

    engine, executor, action_log = _build_autonomy(storage_router, local_adapter)
    _state["engine"] = engine
    _state["executor"] = executor
    _state["action_log"] = action_log

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

    google_client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    base_url = os.environ.get(
        "MITHRA_BASE_URL", "https://assistant.mithra-app.com"
    )
    if google_client_id:
        from mithra.integrations.google_oauth import GoogleIntegration
        google_integration = GoogleIntegration(
            client_id=google_client_id,
            client_secret=google_client_secret,
            redirect_uri=f"{base_url}/auth/google/callback",
            storage_adapter=local_adapter,
            cipher=cipher,
        )
        _state["google_integration"] = google_integration

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

    # SessionMiddleware is required by Authlib to store OAuth state between
    # the /auth/google redirect and the /auth/google/callback.
    from starlette.middleware.sessions import SessionMiddleware
    app.add_middleware(
        SessionMiddleware,
        secret_key=os.environ.get("SECRET_KEY", "dev"),
    )

    # -----------------------------------------------------------------------
    # Auth middleware
    # -----------------------------------------------------------------------

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Health, Telegram webhook, and OAuth routes are unauthenticated via Bearer.
        if request.url.path in (
            "/api/v1/health",
            "/api/v1/webhook/telegram",
            "/auth/google",
            "/auth/google/callback",
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

    @app.get("/auth/google")
    async def google_auth(request: Request, user_id: str = "household"):
        """Redirect to Google OAuth consent screen."""
        google = _state.get("google_integration")
        if not google:
            raise HTTPException(503, "Google not configured")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(google.get_auth_url(user_id))

    @app.get("/auth/google/callback")
    async def google_callback(
        request: Request,
        code: str,
        state: str = "household",
    ):
        """Handle Google callback — save tokens, notify user via Telegram."""
        google = _state.get("google_integration")
        if not google:
            raise HTTPException(503, "Google not configured")
        user_id = state
        try:
            await google.exchange_code(code, user_id)
        except Exception as exc:
            raise HTTPException(400, f"OAuth failed: {exc}")

        telegram_output = _state.get("telegram_output")
        identity = _state.get("identity_resolver")
        if telegram_output and identity:
            chat_id = identity.get_chat_id_for_user(user_id)
            if chat_id:
                from mithra.adapters.messaging import OutboundMessage
                telegram_output.register_chat(user_id, chat_id)
                await telegram_output.send(
                    user_id,
                    OutboundMessage(
                        text=(
                            "✅ Google Calendar, Gmail & Contacts connected!\n\n"
                            "You can now:\n"
                            "• Ask me to add/check calendar events\n"
                            "• I'll read receipts from Gmail\n"
                            "• I'll import your contacts"
                        )
                    ),
                )

        from fastapi.responses import HTMLResponse
        return HTMLResponse("""
            <html><body style="font-family:sans-serif;text-align:center;padding:50px">
              <h2>&#x2705; Google Connected!</h2>
              <p>You can close this tab and return to Telegram.</p>
            </body></html>
        """)

    return app


async def _process_request(agent_request) -> None:
    """Execute the full request lifecycle for a normalised AgentRequest."""
    logger = logging.getLogger(__name__)

    logger.info(f"Processing request: user={agent_request.user_id} text={agent_request.text}")

    signal = agent_request.signal or {}

    # /start — present member selection buttons so the user can self-register
    if signal.get("registration"):
        telegram_output = _state.get("telegram_output")
        household_config = _state.get("household_config", {})
        if telegram_output:
            from mithra.adapters.messaging import ActionButton, OutboundMessage
            chat_id = signal.get("chat_id")
            first_name = signal.get("first_name", "")
            members = household_config.get("members", [])
            actions = [
                ActionButton(
                    label=m["display_name"],
                    action="register",
                    payload={"user_id": m["user_id"]},
                )
                for m in members
            ]
            telegram_output.register_chat("household", chat_id)
            await telegram_output.send_with_actions(
                "household",
                OutboundMessage(text=f"Hi {first_name}! Who are you?"),
                actions,
            )
        return

    # Registration confirmed — acknowledge and update the chat mapping
    if signal.get("registered"):
        telegram_output = _state.get("telegram_output")
        if telegram_output:
            from mithra.adapters.messaging import OutboundMessage
            user_id = signal.get("user_id")
            device_id = agent_request.device_id or ""
            chat_id = int(device_id.split(":")[1]) if ":" in device_id else None
            if chat_id:
                telegram_output.register_chat(user_id, chat_id)
                await telegram_output.send(
                    user_id,
                    OutboundMessage(text="You're all set! Say hello to Mithra."),
                )
        return

    # "connect google" intent — send OAuth link without going through the LLM
    text_lower = (agent_request.text or "").lower()
    if any(phrase in text_lower for phrase in [
        "connect google", "link google", "setup google",
        "connect calendar", "link calendar",
    ]):
        google = _state.get("google_integration")
        base_url = os.environ.get(
            "MITHRA_BASE_URL", "https://assistant.mithra-app.com"
        )
        if google:
            uid = agent_request.user_id
            if google.is_connected(uid):
                result_text = "✅ Your Google account is already connected."
            else:
                url = f"{base_url}/auth/google?user_id={uid}"
                result_text = (
                    f"Tap the link to connect your Google account:\n{url}\n\n"
                    f"This gives Mithra access to your Calendar, Gmail, and Contacts."
                )
            telegram_output = _state.get("telegram_output")
            device_parts = (agent_request.device_id or "").split(":")
            if telegram_output and len(device_parts) > 1:
                from mithra.adapters.messaging import OutboundMessage
                chat_id = int(device_parts[1])
                telegram_output.register_chat(uid, chat_id)
                await telegram_output.send(uid, OutboundMessage(text=result_text))
        return

    agent = _state.get("agent")
    if not agent:
        logger.error("No agent in state")
        return

    domain_router = _state.get("domain_router")
    if domain_router:
        try:
            ctx = domain_router.assemble_context(
                agent_request.user_id,
                agent_request.device_id,
                agent_request.domain_hint,
            )
            agent_request.context = ctx
            logger.info(f"Context assembled: {len(ctx.priority_domains)} priority domains")
        except Exception as e:
            logger.error(f"Context assembly failed: {e}", exc_info=True)

    try:
        result = agent.run(agent_request)
        logger.info(
            f"Agent result: success={result.success} intent={result.intent} "
            f"response={result.response_text[:50] if result.response_text else 'empty'}"
        )
    except Exception as e:
        logger.error(f"Agent.run failed: {e}", exc_info=True)
        return

    engine = _state.get("engine")
    executor = _state.get("executor")
    if engine and executor:
        try:
            receipt = await engine.process(result, agent_request.user_id, executor)
            logger.info(f"Engine processed: executed={len(receipt.executed)} pending={len(receipt.pending)}")
        except Exception as e:
            logger.error(f"Engine.process failed: {e}", exc_info=True)
            return

    # Send response back to Telegram
    telegram_output = _state.get("telegram_output")
    if telegram_output and result.response_text:
        try:
            from mithra.adapters.messaging import OutboundMessage
            chat_id = agent_request.signal.get("chat_id") if agent_request.signal else None
            if not chat_id:
                device_id = agent_request.device_id or ""
                if ":" in device_id:
                    chat_id = int(device_id.split(":")[1])
            if chat_id:
                telegram_output.register_chat(agent_request.user_id, chat_id)
                await telegram_output.send(
                    agent_request.user_id,
                    OutboundMessage(text=result.response_text),
                )
                logger.info(f"Reply sent to {agent_request.user_id}")
            else:
                logger.error(f"No chat_id found for {agent_request.user_id}")
        except Exception as e:
            logger.error(f"Send reply failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level app instance (for uvicorn / docker-compose)
# ---------------------------------------------------------------------------

app = create_app()
