"""OpenRouter wrapper: every call returns JSON validated against a schema.

Models in config.LLM_MODELS are tried in order: a slow, overloaded or rate-limited
model hands the request to the next one instead of making the user wait. Only
providers that support JSON-schema output are used (provider.require_parameters).
"""

import json
import logging
import re
import time
from typing import Any

import httpx

from . import config

log = logging.getLogger("llm")

_client = httpx.AsyncClient(
    base_url=config.OPENROUTER_URL,
    headers={
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "HTTP-Referer": f"http://localhost:{config.APP_PORT}",
        "X-Title": "Bateu a Vontade",
    },
)


class LLMError(Exception):
    pass


class RateLimited(LLMError):
    pass


def to_contents(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Session history (roles user/model) as chat messages."""
    return [{"role": "assistant" if m["role"] == "model" else "user", "content": m["text"]} for m in history]


def _parse(content: str) -> dict[str, Any]:
    # Some models wrap the JSON in a ```json fence even in JSON mode.
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("not an object", text, 0)
    return data


async def generate_json(
    system: str,
    contents: list[dict[str, str]] | str,
    schema: dict[str, Any],
    temperature: float = 0.5,
    timeout: float | None = None,
) -> dict[str, Any]:
    messages = [{"role": "system", "content": system}]
    messages += [{"role": "user", "content": contents}] if isinstance(contents, str) else contents
    timeout = timeout or config.LLM_TIMEOUT
    failures: list[Any] = []
    for model in config.LLM_MODELS:
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
            "provider": {"require_parameters": True},
        }
        started = time.monotonic()
        try:
            response = await _client.post("/chat/completions", json=body, timeout=timeout)
        except httpx.TimeoutException:
            log.warning("%s: no answer after %.0fs, trying the next model", model, timeout)
            failures.append("timeout")
            continue
        except httpx.HTTPError as e:
            log.warning("%s: %s, trying the next model", model, e)
            failures.append("network")
            continue
        if response.status_code != 200:
            log.warning("%s: HTTP %s (%s), trying the next model", model, response.status_code, response.text[:200])
            failures.append(response.status_code)
            continue
        try:
            choice = response.json()["choices"][0]
            if choice.get("error"):  # a provider failure can come inside a 200
                raise LLMError(str(choice["error"])[:200])
            data = _parse(choice["message"]["content"] or "")
        except (LLMError, KeyError, IndexError, TypeError, ValueError) as e:
            log.warning("%s: unusable answer (%s): %r", model, e, response.text[:300])
            failures.append("json")
            continue
        log.info("%s answered in %.1fs", model, time.monotonic() - started)
        return data
    if failures and all(f == 429 for f in failures):
        raise RateLimited("every model is rate limited")
    raise LLMError(f"every model failed: {failures}")
