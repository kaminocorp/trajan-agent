"""Regression test for CORS-on-500 behavior.

Without ``unhandled_exception_handler`` in ``app.main``, Starlette's
``ServerErrorMiddleware`` emits 500 responses outside the user middleware
stack (including ``CORSMiddleware``), so browsers see the 500 as a CORS
failure and the real exception is hidden. This test pins the behavior so a
future middleware refactor that drops the handler fails loud.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.main import unhandled_exception_handler


@pytest.fixture
def app_with_handler(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A minimal FastAPI app wired exactly like prod's CORS + handler setup."""
    from app.config import settings

    monkeypatch.setattr(settings, "cors_origins", ["https://app.example.com"])

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("simulated RLS denial")

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_500_carries_cors_header_for_allowed_origin(app_with_handler: FastAPI) -> None:
    client = TestClient(app_with_handler, raise_server_exceptions=False)
    resp = client.get("/boom", headers={"Origin": "https://app.example.com"})

    assert resp.status_code == 500
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.com"
    assert resp.headers.get("access-control-allow-credentials") == "true"
    assert resp.headers.get("vary") == "Origin"


def test_500_omits_cors_header_for_disallowed_origin(app_with_handler: FastAPI) -> None:
    client = TestClient(app_with_handler, raise_server_exceptions=False)
    resp = client.get("/boom", headers={"Origin": "https://evil.example.com"})

    assert resp.status_code == 500
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_500_omits_cors_header_when_no_origin(app_with_handler: FastAPI) -> None:
    """Non-browser clients (curl, server-to-server) shouldn't get CORS headers."""
    client = TestClient(app_with_handler, raise_server_exceptions=False)
    resp = client.get("/boom")

    assert resp.status_code == 500
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_2xx_path_unaffected(app_with_handler: FastAPI) -> None:
    """The handler only fires on exceptions; happy path keeps CORSMiddleware in charge."""
    client = TestClient(app_with_handler)
    resp = client.get("/ok", headers={"Origin": "https://app.example.com"})

    assert resp.status_code == 200
    # CORSMiddleware (not our handler) attaches this on the success path
    assert resp.headers.get("access-control-allow-origin") == "https://app.example.com"
