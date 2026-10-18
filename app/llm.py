"""Gemini wrapper: every call returns JSON validated against a schema.

Models in config.GEMINI_MODELS are tried in order: a slow, overloaded or rate-limited
model hands the request to the next one instead of making the user wait.
"""

import asyncio
import json
import logging
import time
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
    system: str,
    contents: list[types.Content] | str,
    schema: dict[str, Any],
    temperature: float = 0.5,
    timeout: float | None = None,
) -> dict[str, Any]:
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=temperature,
    )
    timeout = timeout or config.GEMINI_TIMEOUT
    failures = []
    for model in config.GEMINI_MODELS:
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                _client.aio.models.generate_content(model=model, contents=contents, config=cfg), timeout
            )
        except TimeoutError:
            log.warning("%s: no answer after %.0fs, trying the next model", model, timeout)
            failures.append("timeout")
            continue
        except errors.APIError as e:
            log.warning("%s: error %s (%s), trying the next model", model, e.code, str(e)[:200])
            failures.append(e.code)
            continue
        try:
            data = json.loads(response.text or "")
        except json.JSONDecodeError:
            log.warning("%s: invalid JSON: %r", model, (response.text or "")[:500])
            failures.append("json")
            continue
        log.info("%s answered in %.1fs", model, time.monotonic() - started)
        return data
    if failures and all(f == 429 for f in failures):
        raise RateLimited("every model is rate limited")
    raise LLMError(f"every model failed: {failures}")
