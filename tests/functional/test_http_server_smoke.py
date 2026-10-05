"""Functional smoke test: serve-http end to end with the MCP Python client."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import ClientSession

try:  # mcp >= 1.2x
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # pragma: no cover - older 1.x SDKs
    streamable_http_client = None
from mcp.client.streamable_http import streamablehttp_client

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "smoke-test-token"

_SPEC = """\
openapi: 3.0.0
info:
  title: Nutanix VMM APIs
  version: v4.2
paths:
  /vmm/v4.2/ahv/config/vms:
    get:
      operationId: listVms
      summary: List VMs
      parameters:
        - name: $filter
          in: query
          schema: {type: string}
        - name: $limit
          in: query
          schema: {type: integer}
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(params=["false", "true"], ids=["stateful", "stateless"])
def http_server(request, tmp_path):  # type: ignore[no-untyped-def]
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "vmm-v4.2-all-documentation.yaml").write_text(_SPEC, encoding="utf-8")
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PC_", "MCP_", "NAMESPACE_"))
    }
    env.update(
        ARTIFACTS_DIR=str(artifacts_dir),
        LOG_DIR=str(tmp_path / "logs"),
        MCP_AUTH_TOKEN=TOKEN,
        MCP_STATELESS=request.param,
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "src.cli", "serve-http", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                pytest.fail(f"serve-http exited early: {process.stderr.read().decode()[-2000:]}")
            try:
                if httpx.get(f"{base_url}/readyz", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("serve-http did not become ready within 30s")
            time.sleep(0.2)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


async def _run_session(url: str) -> tuple[Any, Any, Any]:
    """initialize, tools/list, and a tools/call with a misspelled query parameter."""
    headers = {"Authorization": f"Bearer {TOKEN}"}

    async def _exercise(session: ClientSession) -> tuple[Any, Any, Any]:
        init = await session.initialize()
        tools = await session.list_tools()
        bogus = await session.call_tool(
            "vmm_execute", {"operation": "listVms", "query_params": {"$fliter": "x"}}
        )
        return init, tools, bogus

    if streamable_http_client is not None:
        async with httpx.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    return await _exercise(session)
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            return await _exercise(session)


def test_serve_http_probes_and_auth(http_server: str) -> None:
    assert httpx.get(f"{http_server}/healthz").json() == {"status": "ok"}
    assert httpx.get(f"{http_server}/readyz").json() == {"status": "ready", "operation_count": 1}
    unauthorized = httpx.post(f"{http_server}/mcp", json={})
    assert unauthorized.status_code == 401


def test_serve_http_initialize_list_and_call(http_server: str) -> None:
    init, tools, bogus = anyio.run(_run_session, f"{http_server}/mcp")

    assert init.serverInfo.name == "nutanix-v4-mcp-server"
    assert "PARAMETER PLACEMENT" in (init.instructions or "")
    execute_tool = next(tool for tool in tools.tools if tool.name == "vmm_execute")
    properties = execute_tool.inputSchema["properties"]
    for bucket in ("path_params", "query_params", "headers"):
        assert properties[bucket]["type"] == "object"

    assert bogus.isError is True
    assert bogus.structuredContent["error"]["code"] == "unknown_parameter"
