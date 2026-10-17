"""Conversation controller: the LLM talks and decides the next step, code drives iFood.

The LLM never gets access to submitOrder: an order is only placed by the
"Confirmar pedido" button endpoint (see confirm_order).
"""

import asyncio
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from . import auth, ifood, llm, prompts, shopping
from .ifood import IFoodError
from .jsonutil import brl, first_value

log = logging.getLogger("agent")

DOCUMENT_HINTS = ("document", "cpf", "cnpj", "passport", "documento")

# trackOrder descriptions may come in English; known codes get a Portuguese text.
ORDER_STATUS_PT = {
    "PLACED": "Pedido recebido pelo iFood.",
    "CONFIRMED": "O mercado confirmou o pedido.",
    "PREPARATION_STARTED": "O mercado está separando seus itens.",
    "READY_TO_PICKUP": "Pedido pronto, aguardando o entregador.",
    "DISPATCHED": "Pedido saiu para entrega! 🛵",
    "ORDER_LATE": "O pedido está atrasado em relação ao previsto.",
    "DELIVERY_ARRIVED_AT_DESTINATION": "O entregador chegou no seu endereço.",
    "CONCLUDED": "Pedido entregue. Bom apetite! 🍽️",
    "CANCELLED": "O pedido foi cancelado.",
}


class NotConnected(Exception):
    pass


@dataclass
class Session:
    id: str
    tokens: auth.Tokens | None = None
    oauth_state: str | None = None
    pkce_verifier: str | None = None
    phase: str = "chat"  # chat | compare | checkout | need_document | ordered
    history: list[dict[str, str]] = field(default_factory=list)  # what the LLM sees
    messages: list[dict[str, Any]] = field(default_factory=list)  # what the UI renders
    servings: int | None = None
    shopping_list: list[shopping.Ingredient] = field(default_factory=list)
    address: shopping.Address | None = None
    options: list[shopping.StoreOption] = field(default_factory=list)
    checkout: shopping.Checkout | None = None
    order_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.say(prompts.WELCOME, remember=False)

    @property
    def connected(self) -> bool:
        return self.tokens is not None

    def start_new_order(self) -> None:
        self.phase = "chat"
        self.servings = None
        self.shopping_list = []
        self.options = []
        self.checkout = None
        self.order_id = None

    def _remember(self, role: str, text: str) -> None:
        # Merge consecutive turns from the same side so the LLM history alternates.
        if self.history and self.history[-1]["role"] == role:
            self.history[-1]["text"] += "\n\n" + text
        else:
            self.history.append({"role": role, "text": text})

    def hear(self, text: str, history_text: str | None = None) -> None:
        self.messages.append({"id": secrets.token_hex(4), "role": "user", "kind": "text", "text": text})
        self._remember("user", history_text or text)

    def say(
        self,
        text: str,
        kind: str = "text",
        data: dict[str, Any] | None = None,
        history_text: str | None = None,
        remember: bool = True,
    ) -> dict[str, Any]:
        message = {"id": secrets.token_hex(4), "role": "bot", "kind": kind, "text": text, "data": data}
        self.messages.append(message)
        if remember:
            self._remember("model", history_text or text)
        return message


# ---------------------------------------------------------------- plumbing


@asynccontextmanager
async def _ifood(s: Session) -> AsyncIterator[ifood.IFoodMCP]:
    if s.tokens is None:
        raise NotConnected
    if s.tokens.expiring_soon():
        try:
            s.tokens = await auth.refresh(s.tokens)
        except auth.AuthError:
            log.warning("token refresh failed", exc_info=True)
            s.tokens = None
            raise NotConnected from None
    async with ifood.connect(s.tokens.access_token) as client:
        yield client


async def _run(s: Session, operation: Callable[[], Awaitable[list[dict]]], failure: str) -> list[dict]:
    try:
        return await operation()
    except NotConnected:
        return [_disconnected(s)]
    except ifood.Unauthorized:
        s.tokens = None
        return [_disconnected(s)]
    except IFoodError as e:
        log.warning("iFood error: %s", e)
        return [s.say(f"{failure}\n\n_Detalhe do iFood: {e.message[:300]}_", remember=False)]
    except llm.RateLimited:
        return [_rate_limited(s)]
    except Exception:
        log.exception("operation failed")
        return [s.say(failure, remember=False)]


def _disconnected(s: Session) -> dict:
    return s.say(
        "Sua conta iFood está desconectada. Clique em **Conectar iFood** para entrar de novo.",
        kind="login",
        remember=False,
    )


def _rate_limited(s: Session) -> dict:
    return s.say(
        "Opa, recebi muitas mensagens em pouco tempo (limite gratuito do Gemini). "
        "Espera uns segundos e manda de novo? 🙏",
        remember=False,
    )


def _state_block(s: Session) -> str:
    lines = ["\n## Estado atual (informação do sistema; não repita literalmente)", f"Fase: {s.phase}"]
    if s.servings:
        lines.append(f"Pessoas: {s.servings}")
    if s.shopping_list:
        lines.append("Lista de compras atual:")
        lines += [f"- {i.label()}" for i in s.shopping_list]
    if s.address:
        lines.append(f"Endereço de entrega: {s.address.label}")
    if s.phase == "compare":
        lines.append("Mercados mostrados na tela:")
        lines += [
            f"{n}. {o.name} — total {brl(o.total)} — faltando: {', '.join(o.missing) or 'nada'}"
            for n, o in enumerate(s.options, 1)
        ]
    if s.phase == "checkout" and s.checkout:
        lines.append(f"Carrinho montado no {s.checkout.store.name}; aguardando o clique em 'Confirmar pedido'.")
    if s.order_id:
        lines.append(f"Pedido feito (id {s.order_id}).")
    return "\n".join(lines)


def _parse_list(raw: Any) -> list[shopping.Ingredient]:
    items = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            items.append(
                shopping.Ingredient(
                    name=str(entry["name"]).strip(),
                    search_term=str(entry.get("search_term") or entry["name"]).strip(),
                    quantity=float(entry.get("quantity") or 1),
                    unit=str(entry.get("unit") or "unidade").strip(),
                )
            )
        except (KeyError, TypeError, ValueError):
            log.warning("ignoring malformed list entry: %r", entry)
    return items


# ---------------------------------------------------------------- entry points


async def handle_message(s: Session, text: str) -> list[dict]:
    if s.phase == "need_document":
        if re.search(r"\d", text):
            return await _submit_with_document(s, text)
        s.phase = "checkout"

    s.hear(text)
    try:
        decision = await llm.generate_json(
            prompts.AGENT_SYSTEM + _state_block(s), llm.to_contents(s.history), prompts.AGENT_SCHEMA
        )
    except llm.RateLimited:
        return [_rate_limited(s)]
    except llm.LLMError:
        log.exception("LLM failed")
        return [s.say("Tive um probleminha para pensar na resposta. Pode repetir?", remember=False)]
    log.info("decision: %s", json.dumps(decision, ensure_ascii=False))

    out = []
    if isinstance(decision.get("servings"), int) and decision["servings"] > 0:
        s.servings = decision["servings"]
    reply = str(decision.get("reply") or "").strip()
    if reply:
        out.append(s.say(reply))

    action = decision.get("action", "none")
    if action == "search_stores":
        if items := _parse_list(decision.get("shopping_list")):
            s.shopping_list = items
        if s.shopping_list:
            out += await search_stores(s)
        else:
            out.append(s.say("Antes de buscar, preciso fechar a lista de compras com você. 🙂"))
    elif action == "choose_store" and s.options and isinstance(decision.get("store_number"), int):
        out += await choose_store(s, decision["store_number"] - 1)
    elif action == "track_order" and s.order_id:
        out += await track_order(s)
    elif action == "order_details" and s.order_id:
        out += await order_details(s)
    elif action == "restart":
        s.start_new_order()
    return out


async def search_stores(s: Session) -> list[dict]:
    async def operation() -> list[dict]:
        async with _ifood(s) as client:
            s.address = shopping.pick_address(await client.call("listAddresses", {}))
            if not s.address:
                return [
                    s.say(
                        "Não encontrei um endereço de entrega salvo na sua conta iFood. "
                        "Cadastre um endereço no app do iFood e me chame de novo. 🏠"
                    )
                ]
            location = {"latitude": s.address.latitude, "longitude": s.address.longitude}
            options, not_found = await shopping.find_store_options(client, s.shopping_list, location)

        if not options:
            s.phase = "chat"
            return [
                s.say(
                    "Não encontrei nenhum mercado aberto agora com esses ingredientes perto de você. 😕 "
                    "Quer tentar mais tarde ou trocar algum ingrediente?"
                )
            ]
        s.options = options
        s.phase = "compare"
        text = f"Entrega em **{s.address.label}**. Comparei {len(options)} mercado(s) aberto(s) agora:"
        if not_found:
            text += (
                f"\n\n⚠️ Não encontrei **{', '.join(not_found)}** em nenhum mercado aberto. "
                "Se quiser seguir mesmo assim, é só escolher um mercado: o pedido vai sem esses itens."
            )
        summary = "\n".join(
            f"{n}. {o.name}: total {brl(o.total)}, faltando: {', '.join(o.missing) or 'nada'}"
            for n, o in enumerate(options, 1)
        )
        return [
            s.say(
                text,
                kind="stores",
                data=_stores_view(s, not_found),
                history_text=f"{text}\n{summary}\nQual mercado você prefere?",
            )
        ]

    return await _run(s, operation, "Não consegui buscar os mercados agora. Tenta de novo?")


async def choose_store(s: Session, index: int, from_button: bool = False) -> list[dict]:
    if not 0 <= index < len(s.options) or s.phase not in ("compare", "checkout"):
        return [s.say("Não encontrei esse mercado. Escolha um dos mercados da comparação. 🙂", remember=False)]
    store = s.options[index]
    if from_button:
        s.hear(f"Quero o mercado {store.name}")

    async def operation() -> list[dict]:
        assert s.address is not None
        async with _ifood(s) as client:
            s.checkout = await shopping.build_cart(client, store, s.address)
        s.phase = "checkout"
        text = (
            f"Montei seu carrinho no **{store.name}**. Confira o resumo e, se estiver tudo certo, "
            "clique em **Confirmar pedido**."
        )
        if store.missing:
            text += f"\n\n⚠️ Vai sem: {', '.join(store.missing)}."
        return [s.say(text, kind="summary", data=_checkout_view(s.checkout))]

    return await _run(s, operation, f"Não consegui montar o carrinho no {store.name}. Quer tentar outro mercado?")


async def confirm_order(s: Session) -> list[dict]:
    if s.phase != "checkout" or not s.checkout:
        return [s.say("Não há nenhum carrinho aguardando confirmação.", remember=False)]
    s.hear("✅ Confirmar pedido")
    return await _submit(s, document=None)


async def _submit_with_document(s: Session, text: str) -> list[dict]:
    digits = re.sub(r"\D", "", text)
    # Keep the document out of the LLM history.
    s.hear("•" * len(digits) if digits else text, history_text="[documento informado]")
    if len(digits) not in (11, 14):
        return [s.say("Hmm, isso não parece um CPF válido. Me manda só os 11 números?", remember=False)]
    s.phase = "checkout"
    return await _submit(s, document=digits)


async def _submit(s: Session, document: str | None) -> list[dict]:
    assert s.checkout is not None
    checkout = s.checkout

    async def operation() -> list[dict]:
        args: dict[str, Any] = {"cartId": checkout.cart_id, "userConsent": True}
        if document:
            args["document"] = document
        async with _ifood(s) as client:
            try:
                result = await client.call("submitOrder", args)
            except IFoodError as e:
                if not document and any(h in e.message.lower() for h in DOCUMENT_HINTS):
                    s.phase = "need_document"
                    return [s.say("O iFood pediu um documento para finalizar. Qual o seu **CPF** (só números)?")]
                raise
        s.order_id = str(first_value(result, "orderId") or first_value(result, "id") or "") or None
        s.phase = "ordered"
        short_code = first_value(result, "shortCode")
        text = f"Pedido feito no **{checkout.store.name}**! 🎉"
        if short_code:
            text += f" Código: **{short_code}**."
        text += "\n\nQuando quiser saber como está, é só perguntar: _cadê meu pedido?_"
        return [s.say(text, kind="order", data={"order_id": s.order_id, "short_code": short_code})]

    return await _run(
        s,
        operation,
        "Não consegui finalizar o pedido. Antes de tentar de novo, confira no app do iFood se ele não foi criado.",
    )


async def track_order(s: Session) -> list[dict]:
    async def operation() -> list[dict]:
        async with _ifood(s) as client:
            data = await client.call("trackOrder", {"orderId": s.order_id})
        status = data.get("status") if isinstance(data.get("status"), dict) else {}
        code = str(status.get("code") or "")
        description = ORDER_STATUS_PT.get(code) or status.get("description") or "Ainda sem atualização."
        return [s.say(f"📦 **Status do pedido:** {description}")]

    return await _run(s, operation, "Não consegui consultar o status do pedido agora.")


async def order_details(s: Session) -> list[dict]:
    async def operation() -> list[dict]:
        async with _ifood(s) as client:
            data = await client.call("getOrder", {"orderId": s.order_id})
        lines = [f"🧾 **Pedido {data.get('shortCode') or ''}** — {data.get('merchant') or ''}".strip()]
        lines += [f"- {item}" for item in data.get("items") or []]
        if data.get("payment"):
            lines.append(f"\n**Pagamento:** {data['payment']}")
        delivery = data.get("delivery") or {}
        if delivery.get("address"):
            lines.append(f"**Entrega:** {delivery['address']}")
        if delivery.get("verificationCode"):
            lines.append(f"**Código de entrega:** {delivery['verificationCode']}")
        return [s.say("\n".join(lines))]

    return await _run(s, operation, "Não consegui consultar os detalhes do pedido agora.")


# ---------------------------------------------------------------- UI payloads


def _stores_view(s: Session, not_found: list[str]) -> dict[str, Any]:
    return {
        "not_found": not_found,
        "options": [
            {
                "number": n,
                "name": o.name,
                "rating": f"{o.rating:.1f}" if o.rating else None,
                "eta": f"{o.eta[0]}–{o.eta[1]} min" if o.eta else None,
                "delivery_fee": "Grátis" if o.delivery_fee == 0 else brl(o.delivery_fee),
                "subtotal": brl(o.subtotal),
                "total": brl(o.total),
                "found": len(o.picks),
                "wanted": len(s.shopping_list),
                "missing": o.missing,
                "minimum": brl(o.minimum_order) if o.below_minimum else None,
                "items": [p.label() for p in o.picks],
            }
            for n, o in enumerate(s.options, 1)
        ],
    }


def _checkout_view(c: shopping.Checkout) -> dict[str, Any]:
    return {
        "store": c.store.name,
        "address": c.address.label,
        "payment": c.payment_label,
        "items": c.items,
        "lines": [{"label": label, "value": value} for label, value in c.lines],
        "warnings": c.warnings,
        "missing": c.store.missing,
        "raw": json.dumps(c.raw, ensure_ascii=False, indent=2)[:20000],
    }
