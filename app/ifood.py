"""Thin client for the iFood MCP server (Streamable HTTP + Bearer token)."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from . import config

log = logging.getLogger("ifood")

# Personal data never reaches the server log (getUser, payment methods, documents).
PRIVATE_KEYS = {
    "fullname",
    "firstname",
    "lastname",
    "email",
    "phone",
    "phonenumber",
    "taxpayeridentificationnumber",
    "document",
    "cpf",
    "cnpj",
    "userid",
    "customerid",
    "cardtokenid",
    "lastdigits",
    "last4",
    "lastfourdigits",
    "userbehaviordescription",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "***" if k.lower() in PRIVATE_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Unauthorized(Exception):
    """The access token was rejected (HTTP 401 or an inline AUTHENTICATION_REQUIRED error)."""


class IFoodError(Exception):
    def __init__(self, tool: str, message: str):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.message = message


class Caller(Protocol):
    """Anything that calls MCP tools: the real client or the guarded wrapper (app.guard)."""

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class IFoodMCP:
    def __init__(self, client: Client):
        self._client = client

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        log.info("→ %s %s", tool, json.dumps(redact(arguments), ensure_ascii=False))
        result = await self._client.call_tool(tool, arguments)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        data = result.structured_content
        if data is None:
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                data = {"text": text}
        log.info("← %s %s", tool, json.dumps(redact(data), ensure_ascii=False)[:3000])
        error = data.get("error") if isinstance(data, dict) else None
        if "AUTHENTICATION_REQUIRED" in text or (
            isinstance(error, dict) and error.get("code") == "AUTHENTICATION_REQUIRED"
        ):
            raise Unauthorized
        if result.is_error:
            raise IFoodError(tool, text or json.dumps(data, ensure_ascii=False))
        # Failures can also come inline, without isError: {"success": false, "error": {code, message}}.
        if isinstance(error, dict) or (isinstance(data, dict) and data.get("success") is False):
            details = error if isinstance(error, dict) else {}
            summary = ": ".join(str(v) for v in (details.get("code"), details.get("message")) if v)
            raise IFoodError(tool, summary or text or "erro sem detalhes")
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
