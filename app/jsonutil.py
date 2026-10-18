"""Lenient lookups over MCP responses.

Only the Discovery tools' response shapes were verified live; the Cart/Order/User
shapes are undocumented, so we look fields up by name anywhere in the payload.
"""

import re
from collections.abc import Iterator
from typing import Any


def walk(obj: Any, parent: dict | None = None) -> Iterator[tuple[dict, dict | None]]:
    """Yields every dict in `obj` (breadth-first) together with its closest parent dict."""
    queue: list[tuple[Any, dict | None]] = [(obj, parent)]
    while queue:
        node, node_parent = queue.pop(0)
        if isinstance(node, dict):
            yield node, node_parent
            queue.extend((v, node) for v in node.values())
        elif isinstance(node, list):
            queue.extend((v, node_parent) for v in node)


def first_value(obj: Any, *keys: str) -> Any:
    for d, _ in walk(obj):
        for key in keys:
            if d.get(key) not in (None, "", []):
                return d[key]
    return None


def dicts_with(obj: Any, key: str) -> list[dict]:
    return [d for d, _ in walk(obj) if key in d]


def cart_id_of(data: Any) -> str | None:
    """The cart id in a createCart/getCart response."""
    value = first_value(data, "cartId") or first_value(data, "id")
    return str(value) if value else None


def last_digits(card: dict) -> Any:
    """A saved card's last digits, under whichever name the response uses."""
    return card.get("lastDigits") or card.get("last4") or card.get("lastFourDigits")


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return as_number(first_value(value, "value", "amount", "total"))
    if isinstance(value, str):
        # Accepts "12.34", "R$ 12,34" and "R$ 1.234,56".
        cleaned = re.sub(r"[^\d,.-]", "", value)
        if "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def brl(value: float | None) -> str:
    if value is None:
        return "—"
    return "R$ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
