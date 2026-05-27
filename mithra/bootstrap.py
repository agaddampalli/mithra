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
    member_registry,
    local_adapter,
    household_id: str,
    executor,
    on_request: Callable[..., Coroutine[Any, Any, None]],
) -> dict:
    from mithra.adapters.messaging.telegram_input import TelegramInputAdapter
    from mithra.adapters.messaging.telegram_output import TelegramOutputAdapter
    from mithra.routing.session import ConversationSessionManager

    known_user_ids = [m.id for m in member_registry.get_members()] if member_registry else []
    session_mgr = ConversationSessionManager(
        local_adapter,
        known_user_ids=known_user_ids,
        household_id=household_id,
    )
    telegram_input = TelegramInputAdapter(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
        session_manager=session_mgr,
        member_registry=member_registry,
        on_request=on_request,
    )
    telegram_output = TelegramOutputAdapter(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "")
    )
    executor.register("telegram_reply", telegram_output)
    executor.register_notify("telegram", telegram_output)
    return {
        "member_registry": member_registry,
        "session_mgr": session_mgr,
        "telegram_input": telegram_input,
        "telegram_output": telegram_output,
    }


def _build_oauth_manager(state: dict) -> None:
    from mithra.integrations.oauth_providers import get_configured_providers
    from mithra.integrations.oauth_token_store import OAuthTokenStore
    from mithra.integrations.oauth_manager import OAuthManager

    providers = get_configured_providers()
    if not providers:
        _logger.info("No OAuth providers configured — skipping OAuthManager")
        return
    token_store = OAuthTokenStore(
        storage_adapter=state["local_adapter"],
        household_id=state["household_id"],
    )
    state["oauth_manager"] = OAuthManager(providers=providers, token_store=token_store)
    _logger.info("OAuthManager configured with providers: %s", list(providers.keys()))


def _build_calendar(state: dict, household_id: str, executor) -> None:
    from mithra.adapters.output.calendar import CalendarAdapter

    calendar_adapter = CalendarAdapter(
        oauth_manager=state.get("oauth_manager"),
        household_id=household_id,
        household_timezone=state.get("household_timezone", "UTC"),
        storage_adapter=state.get("local_adapter"),
    )
    state["calendar_adapter"] = calendar_adapter
    executor.register("calendar_write", calendar_adapter)


def _build_file_storage(state: dict) -> None:
    """Wire FileStorageRouter, DocumentIndexer, and FileSearchService."""
    from mithra.storage.local_file import LocalFileAdapter
    from mithra.storage.file_router import FileStorageRouter

    files_path = os.environ.get("MITHRA_FILES_PATH", "/data/mithra/files")
    db_path = state["local_adapter"].db_path
    local_file = LocalFileAdapter(base_path=files_path, db_path=db_path)

    onedrive_file = None
    oauth_manager = state.get("oauth_manager")
    if oauth_manager and oauth_manager.is_provider_configured("microsoft"):
        from mithra.integrations.microsoft_graph import MicrosoftGraphClient
        from mithra.storage.onedrive_file import OneDriveFileAdapter
        graph_client = MicrosoftGraphClient(oauth_manager)
        onedrive_file = OneDriveFileAdapter(graph_client=graph_client, db_path=db_path)

    file_router = FileStorageRouter(local_adapter=local_file, onedrive_adapter=onedrive_file)
    state["file_router"] = file_router

    try:
        import chromadb
        chroma = chromadb.HttpClient(
            host=os.environ.get("CHROMA_HOST", "vector-store"),
            port=int(os.environ.get("CHROMA_PORT", "8000")),
        )
        from mithra.storage.document_indexer import DocumentIndexer
        from mithra.storage.file_search import FileSearchService
        state["document_indexer"] = DocumentIndexer(
            chroma_client=chroma,
            file_router=file_router,
            household_id=state["household_id"],
        )
        state["file_search"] = FileSearchService(
            file_router=file_router,
            chroma_client=chroma,
            household_id=state["household_id"],
        )
    except Exception as exc:
        _logger.warning("ChromaDB not available for file indexing: %s", exc)


def _build_telegram_handler(state: dict, household_config: dict, base_url: str) -> None:
    from mithra.adapters.messaging.telegram_handler import TelegramSignalHandler

    state["telegram_handler"] = TelegramSignalHandler(
        telegram_output=state["telegram_output"],
        engine=state.get("engine"),
        executor=state.get("executor"),
        household_config=household_config,
        oauth_manager=state.get("oauth_manager"),
        base_url=base_url,
        member_registry=state.get("member_registry"),
        storage_adapter=state.get("local_adapter"),
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

    from mithra.identity.member_registry import MemberRegistry
    member_registry = MemberRegistry(local_adapter.db_path, household_id)
    state["member_registry"] = member_registry

    import sqlite3 as _sqlite3
    with _sqlite3.connect(local_adapter.db_path) as _conn:
        _row = _conn.execute(
            "SELECT value FROM household_config WHERE household_id=? AND key=?",
            (household_id, "timezone"),
        ).fetchone()
    household_tz = _row[0] if _row else "UTC"
    state["household_timezone"] = household_tz

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
    state["agent"] = LangGraphAdapter(
        llm_router=llm_router,
        storage_router=storage_router,
        household_timezone=state.get("household_timezone", "UTC"),
    )

    base_url = os.environ.get("MITHRA_BASE_URL", "https://assistant.mithra-app.com")
    state["base_url"] = base_url

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        state.update(
            _build_telegram(member_registry, local_adapter, household_id, executor, on_request)
        )

    _build_oauth_manager(state)
    _build_file_storage(state)
    _build_calendar(state, household_id, executor)

    if state.get("telegram_output"):
        _build_telegram_handler(state, household_config, base_url)

    _logger.info("Executor registered types: %s", executor.registered_types())
    return state
