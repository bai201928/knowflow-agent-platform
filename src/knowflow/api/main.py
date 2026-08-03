from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from knowflow.api.dependencies import HealthProbe, RequestContextMiddleware
from knowflow.api.error_handlers import install_error_handlers
from knowflow.application.auth.service import AuthService
from knowflow.config import Settings, get_settings
from knowflow.infrastructure.db.session import Database

Lifespan = Callable[[FastAPI], Any]


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    database = Database(settings)
    app.state.settings = settings
    app.state.database = database
    app.state.auth_service = AuthService(settings)
    app.state.health_probes = {"mysql": database.healthy}
    try:
        yield
    finally:
        await database.close()


def _health_router() -> APIRouter:
    router = APIRouter(prefix="/health", tags=["Health"])

    @router.get("/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @router.get("/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        configured: Mapping[str, HealthProbe] = getattr(request.app.state, "health_probes", {})
        dependencies: dict[str, str] = {}
        for name, probe in configured.items():
            try:
                dependencies[name] = "ready" if await probe() else "unavailable"
            except Exception:
                dependencies[name] = "unavailable"
        is_ready = bool(dependencies) and all(value == "ready" for value in dependencies.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "unavailable",
                "dependencies": dependencies,
            },
        )

    return router


def _register_routers(app: FastAPI) -> None:
    app.include_router(_health_router())


def create_app(
    *,
    settings: Settings | None = None,
    lifespan: Lifespan | None = None,
) -> FastAPI:
    configured_settings = settings or get_settings()
    app = FastAPI(
        title="KnowFlow API",
        version="0.2.0",
        summary="Reliable and auditable enterprise knowledge-workflow agent",
        description=(
            "Native KnowFlow HTTP contract. Business mutations are accepted durably and "
            "authorization is re-evaluated from current server state."
        ),
        openapi_version="3.1.0",
        lifespan=lifespan or application_lifespan,
    )
    app.state.settings = configured_settings
    app.add_middleware(RequestContextMiddleware, settings=configured_settings)
    install_error_handlers(app)
    _register_routers(app)
    return app


app = create_app()
