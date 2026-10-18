"""Order tracker card: trackOrder status → progress step and polling cadence (simulated in DRY_RUN)."""

import re
from typing import Any

# Progress steps of the tracker card. The status text itself is shown verbatim from trackOrder.
ORDER_STEPS = ["Recebido", "Confirmado", "Em preparo", "A caminho", "Entregue"]
STEP_OF_CODE = {
    "PLACED": 0,
    "CONFIRMED": 1,
    "PREPARATION_STARTED": 2,
    "READY_TO_PICKUP": 2,
    "DISPATCHED": 3,
    "ORDER_LATE": 3,
    "DELIVERY_ARRIVED_AT_DESTINATION": 3,
    "CONCLUDED": 4,
}
FINAL_CODES = ("CONCLUDED", "CANCELLED")
DEFAULT_POLL_SECONDS = 30
MIN_POLL_SECONDS = 10
MAX_POLL_SECONDS = 600
DRY_RUN_STATUSES = [
    ("PLACED", "[Simulação] Pedido recebido pelo iFood."),
    ("CONFIRMED", "[Simulação] O mercado confirmou o pedido."),
    ("PREPARATION_STARTED", "[Simulação] O mercado está separando seus itens."),
    ("DISPATCHED", "[Simulação] O pedido saiu para entrega."),
    ("CONCLUDED", "[Simulação] Pedido entregue."),
]


def poll_seconds(hints: dict[str, Any]) -> int:
    """agentHints.retryMessage says when to poll again (e.g. "retry in 30 seconds")."""
    message = str(hints.get("retryMessage") or "")
    match = re.search(r"(\d+)\s*(s\b|sec|seg|min)", message, re.IGNORECASE)
    if not match:
        return DEFAULT_POLL_SECONDS
    seconds = int(match.group(1)) * (60 if match.group(2).lower().startswith("min") else 1)
    return max(MIN_POLL_SECONDS, min(seconds, MAX_POLL_SECONDS))


def tracker_view(order_id: str | None, short_code: str | None, data: dict[str, Any]) -> dict[str, Any]:
    status = data.get("status") if isinstance(data.get("status"), dict) else {}
    hints = data.get("agentHints") if isinstance(data.get("agentHints"), dict) else {}
    code = str(status.get("code") or "")
    stop = hints.get("pollingStop") in (True, "true", "TRUE", "True") or code in FINAL_CODES
    return {
        "order_id": order_id,
        "short_code": short_code,
        "code": code,
        "description": status.get("description") or "Aguardando a primeira atualização do iFood.",
        "steps": ORDER_STEPS,
        "step": STEP_OF_CODE.get(code, 0),
        "cancelled": code == "CANCELLED",
        "poll_after": None if stop else poll_seconds(hints),
        "dry_run": False,
    }


def simulated_view(order_id: str | None, short_code: str | None, step: int) -> dict[str, Any]:
    """DRY_RUN: the `step`-th fake status (the last one repeats)."""
    code, description = DRY_RUN_STATUSES[min(step, len(DRY_RUN_STATUSES) - 1)]
    data = {
        "status": {"code": code, "description": description},
        "agentHints": {"retryMessage": "Poll again in 10 seconds"},
    }
    return {**tracker_view(order_id, short_code, data), "dry_run": True}
