"""Optional stdio MCP server.

Run with:

    python -m merchant_agent.mcp.server

This exposes the same tool registry as the HTTP MCP endpoint to MCP-native
clients (for example Claude Desktop). It requires the optional `mcp` package:

    pip install "mcp>=1.2.0"

The HTTP endpoint at `POST /mcp` works without that dependency, which is why it
is what the buyer agent uses.
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.services.ai_provider import get_ai_provider
from app.services.catalog_service import CatalogService
from app.services.embedding_service import EmbeddingService, JsonFileEmbeddingCache
from app.services.inventory_service import InventoryService
from merchant_agent.mcp.tools import MCPToolRegistry

logger = get_logger(__name__)


def build_registry() -> MCPToolRegistry:
    settings = get_settings()
    embeddings = EmbeddingService(
        get_ai_provider(settings), JsonFileEmbeddingCache(settings.embedding_cache_path)
    )
    catalog = CatalogService.from_file(settings.catalog_path, embedding_service=embeddings)
    return MCPToolRegistry(catalog, InventoryService(catalog))


async def _run() -> None:  # pragma: no cover - requires the optional mcp package
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import TextContent, Tool
    except ImportError:
        print(
            "The 'mcp' package is not installed. Install it with:\n"
            '    pip install "mcp>=1.2.0"\n'
            "The HTTP MCP endpoint (POST /mcp on the merchant agent) works without it.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    registry = build_registry()
    server: Server = Server("merchant-catalog")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=definition["name"],
                description=definition["description"],
                inputSchema=definition["inputSchema"],
            )
            for definition in registry.definitions()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        result = registry.call(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:  # pragma: no cover
    configure_logging()
    logger.info("Starting merchant MCP stdio server")
    asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    main()
