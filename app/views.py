"""Payloads of the chat cards the UI renders (store comparison, checkout summary, choice buttons)."""

from typing import Any

from . import config
from .jsonutil import brl
from .shopping import Checkout, Ingredient, StoreOption


def eta_label(eta: tuple[int, int] | None) -> str | None:
    return f"{eta[0]}–{eta[1]} min" if eta else None


def options_view(labels: list[str]) -> list[dict[str, Any]]:
    """Numbered buttons of a choice card (addresses, vouchers)."""
    return [{"number": n, "label": label} for n, label in enumerate(labels, 1)]


def stores_view(
    options: list[StoreOption], shopping_list: list[Ingredient], restrictions: str | None, not_found: list[str]
) -> dict[str, Any]:
    return {
        "not_found": not_found,
        "restrictions": restrictions,
        "options": [
            {
                "number": n,
                "name": o.name,
                "rating": f"{o.rating:.1f}" if o.rating else None,
                "eta": eta_label(o.eta),
                "delivery_fee": "Grátis" if o.delivery_fee == 0 else brl(o.delivery_fee),
                "subtotal": brl(o.subtotal),
                "total": brl(o.total),
                "found": len(o.picks),
                "wanted": len(shopping_list),
                "missing": o.missing,
                "minimum": brl(o.minimum_order) if o.below_minimum else None,
                "items": [p.view() for p in o.picks],
            }
            for n, o in enumerate(options, 1)
        ],
    }


def checkout_view(c: Checkout) -> dict[str, Any]:
    return {
        "store": c.store.name,
        "address": c.address.label,
        "eta": eta_label(c.store.eta),
        "payment": c.payment_label,
        "voucher": c.voucher_label,
        "items": c.items,
        "lines": [{"label": label, "value": value} for label, value in c.lines],
        "warnings": c.warnings,
        "missing": c.store.missing,
        "dry_run": config.DRY_RUN,
    }
