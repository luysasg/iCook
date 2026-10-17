"""Thin client for the iFood MCP server (Streamable HTTP + Bearer token)."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from . import config

log = logging.getLogger("ifood")


class Unauthorized(Exception):
    """The access token was rejected (HTTP 401)."""


class IFoodError(Exception):
    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class IFoodMCP:
    def __init__(self, client: Client):
        self._client = client

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        logged = {k: ("***" if k == "document" else v) for k, v in arguments.items()}
        log.info("→ %s %s", tool, json.dumps(logged, ensure_ascii=False))
        result = await self._client.call_tool(tool, arguments)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        data = result.structured_content
        if data is None:
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"text": text}
        log.info("← %s %s", tool, json.dumps(data, ensure_ascii=False)[:3000])
        if result.is_error:
            raise IFoodError(tool, text or json.dumps(data, ensure_ascii=False))
        return data


@asynccontextmanager
async def connect(access_token: str) -> AsyncIterator[IFoodMCP]:
    # The MCP SDK reports HTTP failures as a generic internal error, so watch for 401 ourselves.
    rejected = False

    async def on_response(response: Any) -> None:
        nonlocal rejected
        if response.status_code == 401:
            rejected = True

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {access_token}"})
    http.event_hooks["response"].append(on_response)
    try:
        async with http:
            transport = streamable_http_client(config.IFOOD_MCP_URL, http_client=http)
            async with Client(transport, mode="legacy") as client:
                yield IFoodMCP(client)
    except Exception:
        if rejected:
            raise Unauthorized from None
        raise
