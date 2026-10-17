"""Gemini wrapper: every call returns JSON validated against a schema."""

import asyncio
import json
import logging
from typing import Any

from google import genai
from google.genai import errors, types

from . import config

log = logging.getLogger("llm")

_client = genai.Client(api_key=config.GEMINI_API_KEY)


class LLMError(Exception):
    pass


class RateLimited(LLMError):
    pass


def to_contents(history: list[dict[str, str]]) -> list[types.Content]:
    return [types.Content(role=m["role"], parts=[types.Part(text=m["text"])]) for m in history]


async def generate_json(
    system: str, contents: list[types.Content] | str, schema: dict[str, Any], temperature: float = 0.5
) -> dict[str, Any]:
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=temperature,
    )
    for attempt in range(3):
        try:
            response = await _client.aio.models.generate_content(
                model=config.GEMINI_MODEL, contents=contents, config=cfg
            )
        except errors.APIError as e:
            # Free tier is ~10 requests/minute: back off a little before giving up.
            if e.code == 429:
                if attempt < 2:
                    await asyncio.sleep(10 * (attempt + 1))
                    continue
                raise RateLimited(str(e)) from e
            if e.code >= 500 and attempt < 2:
                await asyncio.sleep(2)
                continue
            raise LLMError(str(e)) from e
        try:
            return json.loads(response.text or "")
        except json.JSONDecodeError:
            log.warning("invalid JSON from model: %r", (response.text or "")[:500])
    raise LLMError("model did not return valid JSON")
