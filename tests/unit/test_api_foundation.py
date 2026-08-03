from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowflow.api.main import create_app
from knowflow.domain.common.errors import ErrorCode, KnowFlowError


@asynccontextmanager
async def _no_lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


def test_liveness_and_request_correlation_are_available_without_dependencies() -> None:
    app = create_app(lifespan=_no_lifespan)
    with TestClient(app) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "req-demo-1"})
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert response.headers["X-Request-ID"] == "req-demo-1"


def test_invalid_request_id_is_replaced_with_safe_server_value() -> None:
    app = create_app(lifespan=_no_lifespan)
    with TestClient(app) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "bad value attack"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "bad value attack"
    assert len(response.headers["X-Request-ID"]) >= 32


def test_domain_errors_use_rfc_9457_problem_json_without_context_leaks() -> None:
    app = create_app(lifespan=_no_lifespan)

    @app.get("/boom")
    async def boom() -> None:
        raise KnowFlowError(
            ErrorCode.VERSION_CONFLICT,
            "Version changed",
            status=409,
            context={"password": "never-return"},
        )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "VERSION_CONFLICT"
    assert "never-return" not in response.text


def test_readiness_distinguishes_dependency_failure_from_process_life() -> None:
    app = create_app(lifespan=_no_lifespan)

    async def healthy() -> bool:
        return True

    async def unhealthy() -> bool:
        return False

    app.state.health_probes = {"mysql": healthy, "redis": unhealthy}
    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["dependencies"] == {"mysql": "ready", "redis": "unavailable"}


def test_openapi_uses_native_contract_metadata_and_problem_schema() -> None:
    app = create_app(lifespan=_no_lifespan)
    with TestClient(app) as client:
        document = client.get("/openapi.json").json()
    assert document["info"]["title"] == "KnowFlow API"
    assert document["info"]["version"] == "0.2.0"
    assert document["openapi"].startswith("3.1")
