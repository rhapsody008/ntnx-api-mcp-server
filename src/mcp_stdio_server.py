"""MCP stdio runtime server entrypoint."""

from __future__ import annotations

import asyncio

from mcp.server.stdio import stdio_server

from src.config import Settings
from src.mcp_app import _build_instructions, _to_mcp_tool, build_mcp_server, initialization_options

__all__ = ["_build_instructions", "_to_mcp_tool", "serve_stdio"]


async def _serve_stdio(settings: Settings) -> None:
    server, _ = build_mcp_server(settings)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, initialization_options(server))


def serve_stdio(settings: Settings) -> None:
    """Run the MCP stdio server until terminated."""
    asyncio.run(_serve_stdio(settings))
