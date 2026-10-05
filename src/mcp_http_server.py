"""MCP Streamable HTTP server entrypoint (Starlette + uvicorn)."""

from __future__ import annotations

import contextlib
import hmac
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config import Settings
from src.mcp_app import build_mcp_server
from src.tools import RuntimeToolDispatcher

LOGGER = logging.getLogger(__name__)


@dataclass
class _RuntimeState:
    """Spec loading happens after the HTTP listener is up; this tracks its progress."""

    dispatcher: RuntimeToolDispatcher | None = None
    manager: StreamableHTTPSessionManager | None = None
    error: str | None = None


class _BearerAuth:
    """Require ``Authorization: Bearer <token>`` before passing to the wrapped app."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self._expected):
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _MCPEndpoint:
    """ASGI endpoint that forwards to the session manager once specs are loaded."""

    def __init__(self, state: _RuntimeState) -> None:
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.state.manager is None:
            detail = self.state.error or "Server is still loading API specs."
            response = JSONResponse({"error": "not_ready", "detail": detail}, status_code=503)
            await response(scope, receive, send)
            return
        await self.state.manager.handle_request(scope, receive, send)


def build_http_app(settings: Settings) -> Starlette:
    """Build the Starlette app serving MCP at ``settings.http_path`` plus health probes."""
    state = _RuntimeState()

    async def _load_and_serve() -> None:
        try:
            server, dispatcher = await anyio.to_thread.run_sync(build_mcp_server, settings)
        except Exception as exc:  # surfaced via /readyz; the pod stays unready
            LOGGER.exception("event=http_startup_failed error=%s", exc)
            state.error = f"Startup failed: {exc}"
            return
        manager = StreamableHTTPSessionManager(
            app=server,
            json_response=settings.mcp_stateless,
            stateless=settings.mcp_stateless,
        )
        async with manager.run():
            state.dispatcher = dispatcher
            state.manager = manager
            LOGGER.info(
                "event=http_ready operation_count=%s path=%s stateless=%s auth=%s",
                len(dispatcher.load_result.operations),
                settings.http_path,
                settings.mcp_stateless,
                settings.mcp_auth_token is not None,
            )
            await anyio.sleep_forever()

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(_load_and_serve)
            yield
            task_group.cancel_scope.cancel()

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def readyz(_: Request) -> JSONResponse:
        if state.dispatcher is None:
            body = {"status": "failed" if state.error else "loading"}
            if state.error:
                body["error"] = state.error
            return JSONResponse(body, status_code=503)
        return JSONResponse(
            {"status": "ready", "operation_count": len(state.dispatcher.load_result.operations)}
        )

    mcp_endpoint: ASGIApp = _MCPEndpoint(state)
    if settings.mcp_auth_token is not None:
        mcp_endpoint = _BearerAuth(mcp_endpoint, settings.mcp_auth_token.get_secret_value())

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/readyz", readyz, methods=["GET"]),
            # An ASGI-app endpoint on Route (not Mount) serves the exact path without a
            # trailing-slash redirect, which MCP clients do not follow for POST.
            Route(settings.http_path, mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
    app.state.runtime = state
    return app


def serve_http(settings: Settings) -> None:
    """Run the MCP Streamable HTTP server until terminated."""
    import uvicorn

    if settings.mcp_auth_token is None:
        LOGGER.warning(
            "event=http_auth_disabled detail=MCP_AUTH_TOKEN is not set; the MCP endpoint is unauthenticated"
        )
    uvicorn.run(
        build_http_app(settings),
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,  # keep the CLI's logging configuration
        proxy_headers=True,
    )
