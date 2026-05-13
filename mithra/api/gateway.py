"""
FastAPI gateway — lifecycle orchestrator and single HTTP entry point for Mithra.

Startup sequence:
  1. Load HouseholdConfig from household.yaml
  2. Initialise StorageRouter (adapters, encryption)
  3. Initialise ContextCache
  4. Initialise DomainMemoryRouter
  5. Initialise LLMRouter
  6. Initialise AutononomyEngine + ActionExecutorRegistry
  7. Register all MCPTools (domain plugins)
  8. Initialise AgentInterface (LangGraphAdapter)
  9. Mount input adapters (Telegram webhook, Wyoming)
 10. Mount output adapters in ActionExecutorRegistry

Request lifecycle (per CLAUDE.md contract):
  1. Receive normalised AgentRequest from input adapter
  2. Load ContextSnapshot via DomainMemoryRouter
  3. Call agent.run() or agent.run_stream()
  4. Pass AgentResult to AutononomyEngine.process()
  5. Pass ExecutionReceipt to output adapter
  6. Return response to user

Auth: Bearer token middleware validates SECRET_KEY on all /api/v1/* routes.
Exposed at api.mithra-app.com via the Cloudflare tunnel.
"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Construct and configure the FastAPI application with all routes and lifecycle hooks."""
    app = FastAPI(title="Mithra", version="0.1.0")

    # Mounted after Phase 0 implementation:
    # from mithra.api.routes import agent, health, domains, webhooks
    # app.include_router(agent.router, prefix="/api/v1/agent")
    # app.include_router(health.router, prefix="/api/v1/health")
    # app.include_router(domains.router, prefix="/api/v1/domains")
    # app.include_router(webhooks.router, prefix="/api/v1/webhook")

    return app


app = create_app()
