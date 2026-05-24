"""
Application wiring — builds and returns all components as a populated state dict.

gateway.py calls build_state() once at startup and must not import from
mithra.integrations or mithra.adapters directly. All provider-specific
instantiation lives here.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Coroutine

import yaml

_logger = logging.getLogger(__name__)


def load_household_config(path: str = "household.yaml") -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _build_storage_router(household_config: dict, tmp_dir: str | None = None):
    from mithra.storage.local import LocalStorageAdapter
    from mithra.storage.encryption import EncryptedStorageAdapter, AESGCMCipher
    from mithra.storage.router import StorageRouter

    storage_dir = tmp_dir or os.environ.get("MITHRA_STORAGE_DIR", "/data/mithra/files")
    db_path = os.path.join(
        tmp_dir or os.environ.get("MITHRA_DB_DIR", "/data/mithra"), "mithra.db"
    )

    local = LocalStorageAdapter(storage_dir, db_path)

    enc_key = os.environ.get("MITHRA_ENCRYPTION_KEY", "")
    cipher = None
    if enc_key:
        cipher = AESGCMCipher(enc_key)
        encrypted_local = EncryptedStorageAdapter(local, cipher)
    else:
        encrypted_local = local

    onedrive_client_id = os.environ.get("ONEDRIVE_CLIENT_ID", "")
    if onedrive_client_id:
        from mithra.storage.onedrive import OneDriveAdapter
        onedrive = OneDriveAdapter(
            client_id=onedrive_client_id,
            client_secret=os.environ.get("ONEDRIVE_CLIENT_SECRET", ""),
            tenant_id=os.environ.get("ONEDRIVE_TENANT_ID", ""),
        )
        encrypted_onedrive = onedrive
    else:
        onedrive = local
        encrypted_onedrive = encrypted_local

    return (
        StorageRouter(
            local_adapter=local,
            onedrive_adapter=onedrive,
            encrypted_local_adapter=encrypted_local,
            encrypted_onedrive_adapter=encrypted_onedrive,
        ),
        local,
        cipher,
    )


def _build_domain_router(storage_router, household_config: dict):
    from mithra.routing.domain_router import DefaultDomainMemoryRouter
    from mithra.memory.context_cache import ContextCache
    from mithra.memory.structured_store import StructuredStore

    db_dir = os.environ.get("MITHRA_DB_DIR", "/data/mithra")
    cache = ContextCache(os.path.join(db_dir, "context_cache.db"))
    structured_store = StructuredStore(os.path.join(db_dir, "structured_store.db"))
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

    return LiteLLMRouterAdapter(
        base_url=os.environ.get("LITELLM_BASE_URL", "http://llm-router:4000"),
        sanitiser=DataSanitiser(),
        storage_adapter=local_adapter,
    )


def _build_autonomy(local_adapter, household_id: str = ""):
    from mithra.autonomy.default_engine import DefaultAutonomyEngine
    from mithra.autonomy.executor import ActionExecutorRegistry
    from mithra.autonomy.action_log import ActionLog
    from mithra.autonomy.dead_letter import DeadLetterQueue

    action_log = ActionLog(local_adapter, household_id=household_id)
    dead_letter_queue = DeadLetterQueue(local_adapter, household_id=household_id)
    executor = ActionExecutorRegistry()
    engine = DefaultAutonomyEngine(
        storage_adapter=local_adapter,
        action_log=action_log,
        config_path=os.environ.get("AUTONOMY_CONFIG_PATH", "services/autonomy/autonomy.yaml"),
        executor=executor,
    )
    return engine, executor, action_log, dead_letter_queue


def _build_telegram(
    household_config: dict,
    local_adapter,
    household_id: str,
    executor,
    on_request: Callable[..., Coroutine[Any, Any, None]],
) -> dict:
    from mithra.adapters.messaging.telegram_input import TelegramInputAdapter, UserIdentityResolver
    from mithra.adapters.messaging.telegram_output import TelegramOutputAdapter
    from mithra.routing.session import ConversationSessionManager

    identity_resolver = UserIdentityResolver(household_config, storage_adapter=local_adapter)
    session_mgr = ConversationSessionManager(
        local_adapter,
        known_user_ids=[m["user_id"] for m in household_config.get("members", [])],
        household_id=household_id,
    )
    telegram_input = TelegramInputAdapter(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
        session_manager=session_mgr,
        identity_resolver=identity_resolver,
        on_request=on_request,
    )
    telegram_output = TelegramOutputAdapter(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "")
    )
    executor.register("telegram_reply", telegram_output)
    executor.register_notify("telegram", telegram_output)
    return {
        "identity_resolver": identity_resolver,
        "session_mgr": session_mgr,
        "telegram_input": telegram_input,
        "telegram_output": telegram_output,
    }


def _build_calendar(state: dict, household_id: str, executor) -> None:
    from mithra.adapters.output.calendar import CalendarAdapter

    calendar_adapter = CalendarAdapter(
        nango_client=state.get("nango"),
        household_id=household_id,
        google_integration=state.get("google_integration"),
    )
    state["calendar_adapter"] = calendar_adapter
    executor.register("calendar_write", calendar_adapter)


async def _setup_nango(state: dict) -> None:
    from mithra.integrations.nango_client import NangoClient

    nango = NangoClient(
        nango_url=os.environ.get("NANGO_URL", "http://nango:8080"),
        secret_key=os.environ.get("NANGO_SECRET_KEY", ""),
    )
    state["nango"] = nango
    await nango.setup_configured_integrations()


def _setup_google(state: dict, local_adapter, cipher, base_url: str) -> None:
    from mithra.integrations.google_oauth import GoogleIntegration

    state["google_integration"] = GoogleIntegration(
        client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        redirect_uri=f"{base_url}/auth/google/callback",
        storage_adapter=local_adapter,
        cipher=cipher,
    )


def _build_telegram_handler(state: dict, household_config: dict, base_url: str) -> None:
    from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler

    state["telegram_handler"] = TelegramSignalHandler(
        telegram_output=state["telegram_output"],
        engine=state.get("engine"),
        executor=state.get("executor"),
        household_config=household_config,
        nango=state.get("nango"),
        base_url=base_url,
        identity_resolver=state.get("identity_resolver"),
    )


async def build_state(
    household_config: dict,
    on_request: Callable[..., Coroutine[Any, Any, None]],
) -> dict[str, Any]:
    """Instantiate and wire all application components.

    on_request is the gateway's request-lifecycle callback — it references
    _state so it must be defined in gateway and passed in here.
    Returns a fully populated state dict ready for the gateway to use.
    """
    state: dict[str, Any] = {}
    household_id = household_config.get("household_id", "default")
    state["household_config"] = household_config
    state["household_id"] = household_id

    storage_router, local_adapter, cipher = _build_storage_router(household_config)
    state["storage_router"] = storage_router
    state["local_adapter"] = local_adapter

    domain_router, cache = _build_domain_router(storage_router, household_config)
    state["domain_router"] = domain_router
    state["context_cache"] = cache

    llm_router = _build_llm_router(local_adapter)
    state["llm_router"] = llm_router

    engine, executor, action_log, dead_letter_queue = _build_autonomy(
        local_adapter, household_id=household_id
    )
    state["engine"] = engine
    state["executor"] = executor
    state["action_log"] = action_log
    state["dead_letter_queue"] = dead_letter_queue

    from mithra.agent.langgraph_adapter import LangGraphAdapter
    state["agent"] = LangGraphAdapter(llm_router=llm_router, storage_router=storage_router)

    base_url = os.environ.get("MITHRA_BASE_URL", "https://assistant.mithra-app.com")
    state["base_url"] = base_url

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        state.update(
            _build_telegram(household_config, local_adapter, household_id, executor, on_request)
        )

    if os.environ.get("NANGO_SECRET_KEY"):
        await _setup_nango(state)

    if os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"):
        _setup_google(state, local_adapter, cipher, base_url)

    _build_calendar(state, household_id, executor)

    if state.get("telegram_output"):
        _build_telegram_handler(state, household_config, base_url)

    _logger.info("Executor registered types: %s", executor.registered_types())
    return state
