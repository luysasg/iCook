"""Conversation controller: the LLM talks and decides the next step, code drives iFood.

The LLM never gets access to submitOrder: an order is only placed by the
"Confirmar pedido" button endpoint (see confirm_order), and every MCP call goes
through app.guard, which enforces the documented checkout order.

Phases: address → chat → pantry (recipe shown) → compare → payment → voucher → checkout → ordered
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

from . import auth, config, ifood, llm, prompts, shopping, tracking, user, views
from .guard import Guard, GuardedClient
from .ifood import IFoodError
from .jsonutil import brl, first_value
from .recipe import (
    Recipe,
    parse_ingredients,
    parse_pantry_answer,
    parse_recipe,
    valid_document,
)

log = logging.getLogger("agent")

DOCUMENT_HINTS = ("document", "cpf", "cnpj", "passport", "documento")
NO_RESTRICTIONS = ("nenhuma", "nenhum", "não", "nao", "none", "sem restrições", "sem restrição")

# Status line shown under the typing indicator while a tool runs.
TOOL_STATUS = {
    "listAddresses": "Carregando seus endereços…",
    "searchOffers": "Buscando ingredientes nos mercados…",
    "createCart": "Montando seu carrinho…",
    "getCart": "Conferindo o carrinho…",
    "addItemToCart": "Atualizando o carrinho…",
    "updateCartItem": "Atualizando o carrinho…",
    "removeItemFromCart": "Atualizando o carrinho…",
    "getCartDeliveryMethods": "Verificando a entrega…",
    "setCartDelivery": "Verificando a entrega…",
    "listPaymentMethods": "Carregando formas de pagamento…",
    "setCartPayment": "Aplicando a forma de pagamento…",
    "listVouchers": "Procurando cupons…",
    "setVoucher": "Aplicando o cupom…",
    "getUser": "Carregando seu perfil…",
    "submitOrder": "Enviando o pedido ao iFood…",
    "trackOrder": "Consultando o status do pedido…",
    "getOrder": "Consultando seu pedido…",
}
THINKING = "Pensando…"
# getUser fails → no personalization now, one more try in the background after this delay.
PROFILE_RETRY_SECONDS = 3.0
# Craving chips (behavior profile or time of day), started in the background when the addresses show up.
# The reply waits for them however long it takes; failures are retried with this backoff.
CHIPS_LLM_TIMEOUT = 20.0
CHIPS_RETRY_SECONDS = 1.0
CHIPS_RETRY_MAX_SECONDS = 10.0
MAX_CHIPS = 4
MAX_CHIP_CHARS = 30
# iFood error text shown to the user is cut to this length.
ERROR_DETAIL_MAX_CHARS = 300

# Actions whose result the system shows itself: their reply text is filler and is dropped.
SYSTEM_ACTIONS = {
    "choose_address",
    "change_address",
    "show_recipe",
    "set_pantry",
    "search_stores",
    "choose_store",
    "choose_payment",
    "choose_voucher",
    "track_order",
    "order_details",
}


class NotConnected(Exception):
    pass


@dataclass
class Session:
    id: str
    tokens: auth.Tokens | None = None
    oauth_state: str | None = None
    pkce_verifier: str | None = None
    phase: str = "chat"  # see the module docstring; plus need_document
    history: list[dict[str, str]] = field(default_factory=list)  # what the LLM sees
    messages: list[dict[str, Any]] = field(default_factory=list)  # what the UI renders
    addresses: list[shopping.Address] = field(default_factory=list)  # saved on the iFood account
    servings: int | None = None
    restrictions: str | None = None
    recipe: Recipe | None = None
    recipe_message: str | None = None  # id of the recipe card, updated with what the user has
    shopping_list: list[shopping.Ingredient] = field(default_factory=list)
    address: shopping.Address | None = None
    options: list[shopping.StoreOption] = field(default_factory=list)
    store: shopping.StoreOption | None = None
    cart_id: str | None = None
    payment_options: list[shopping.PaymentOption] = field(default_factory=list)
    payment: shopping.PaymentOption | None = None
    vouchers: list[shopping.Voucher] = field(default_factory=list)
    voucher: shopping.Voucher | None = None
    checkout: shopping.Checkout | None = None
    order_id: str | None = None
    short_code: str | None = None
    dry_run_step: int = 0
    guard: Guard = field(default_factory=Guard)
    # From getUser, once per session (kept on "Nova conversa"); None = not loaded or failed.
    profile: user.UserProfile | None = None
    profile_retry: asyncio.Task | None = None
    # Craving chips suggested from the behavior profile, by its behavior text (getUser + address habits).
    chips_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    payment_preselected: int | None = None  # index into payment_options
    activity: str = ""  # what is running right now, for the UI status line
    # Interrupted by a disconnection: replayed after the next login.
    pending: tuple[Callable[[], Awaitable[list[dict]]], str] | None = None
    pending_message: str | None = None
    needs_resume: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if not self.connected:
            self.say(prompts.WELCOME, kind="login", remember=False)

    @property
    def connected(self) -> bool:
        return self.tokens is not None

    def start_new_order(self) -> None:
        self.phase = "chat"
        self.servings = None
        self.restrictions = None
        self.recipe = None
        self.recipe_message = None
        self.shopping_list = []
        self.options = []
        self.clear_cart()
        self.order_id = None
        self.short_code = None
        self.dry_run_step = 0

    def clear_cart(self) -> None:
        self.store = None
        self.cart_id = None
        self.payment_options = []
        self.payment_preselected = None
        self.payment = None
        self.vouchers = []
        self.voucher = None
        self.checkout = None

    def remember(self, role: str, text: str) -> None:
        # Merge consecutive turns from the same side so the LLM history alternates.
        if self.history and self.history[-1]["role"] == role:
            self.history[-1]["text"] += "\n\n" + text
        else:
            self.history.append({"role": role, "text": text})

    def hear(self, text: str, history_text: str | None = None, remember: bool = True) -> None:
        self.messages.append({"id": secrets.token_hex(4), "role": "user", "kind": "text", "text": text})
        if remember:
            self.remember("user", history_text or text)

    def say(
        self,
        text: str,
        kind: str = "text",
        data: dict[str, Any] | None = None,
        history_text: str | None = None,
        remember: bool = True,
    ) -> dict[str, Any]:
        message = {"id": secrets.token_hex(4), "role": "bot", "kind": kind, "text": text, "data": data, "rev": 0}
        self.messages.append(message)
        if remember:
            self.remember("model", history_text or text)
        return message

    def redact(self, text: str) -> str:
        """For logs: the user's name never goes to the server console."""
        name = self.profile.first_name if self.profile else None
        return re.sub(rf"\b{re.escape(name)}\b", "***", text, flags=re.IGNORECASE) if name else text

    def update(self, message_id: str | None, data: dict[str, Any]) -> dict[str, Any] | None:
        """Changes a card already on screen (tracker refresh, ticked ingredients)."""
        message = next((m for m in self.messages if m["id"] == message_id), None)
        if message is not None:
            message["data"] = data
            message["rev"] += 1
        return message


# ---------------------------------------------------------------- plumbing


@asynccontextmanager
async def _ifood(s: Session) -> AsyncIterator[GuardedClient]:
    if s.tokens is None:
        raise NotConnected
    if s.tokens.expiring_soon():
        try:
            s.tokens = await auth.refresh(s.tokens)
        except auth.AuthError:
            log.warning("token refresh failed", exc_info=True)
            s.tokens = None
            raise NotConnected from None

    def on_call(tool: str) -> None:
        s.activity = TOOL_STATUS.get(tool, "Falando com o iFood…")

    async with ifood.connect(s.tokens.access_token) as client:
        yield GuardedClient(client, s.guard, on_call)


async def _run(s: Session, operation: Callable[[], Awaitable[list[dict]]], failure: str) -> list[dict]:
    try:
        return await operation()
    except NotConnected:
        s.pending = (operation, failure)
        return [_disconnected(s)]
    except ifood.Unauthorized:
        s.tokens = None
        s.pending = (operation, failure)
        return [_disconnected(s)]
    except IFoodError as e:
        log.warning("iFood error: %s", e)
        return [s.say(f"{failure}\n\n_Detalhe do iFood: {e.message[:ERROR_DETAIL_MAX_CHARS]}_", remember=False)]
    except llm.RateLimited:
        return [_rate_limited(s)]
    except Exception:
        log.exception("operation failed")
        return [s.say(failure, remember=False)]


def _disconnected(
    s: Session,
    text: str = "Sua conta iFood desconectou. Conecte de novo e eu continuo exatamente de onde paramos.",
) -> dict:
    return s.say(text, kind="login", remember=False)


def _rate_limited(s: Session) -> dict:
    return s.say(
        "Recebi muitas mensagens em pouco tempo (limite gratuito do Gemini). Manda de novo em alguns segundos? 🙏",
        remember=False,
    )


def _forget_tokens(s: Session) -> list[dict]:
    """iFood rejected the token: show the connect card."""
    s.tokens = None
    return [_disconnected(s)]


def _numbered(options: list[str]) -> str:
    return "\n".join(f"{n}. {label}" for n, label in enumerate(options, 1))


def _system_prompt(s: Session) -> str:
    profile = _profile_for(s)
    return prompts.AGENT_SYSTEM + (profile.prompt_block() if profile else "") + _state_block(s)


def _state_block(s: Session) -> str:
    lines = [
        "\n## Estado atual (informação do sistema; não repita literalmente)",
        user.now_line(user.now_sp()),
        f"Fase: {s.phase}",
    ]
    if s.servings:
        lines.append(f"Pessoas: {s.servings}")
    if s.restrictions:
        lines.append(f"Restrições: {s.restrictions}")
    if s.recipe:
        lines.append(f"Receita mostrada: {s.recipe.title}. Ingredientes numerados:")
        lines.append(_numbered([i.label() for i in s.recipe.ingredients]))
        if s.recipe.have:
            lines.append(f"A pessoa já tem: {', '.join(map(str, s.recipe.have))}")
    if s.shopping_list:
        lines.append("Lista de compras atual:")
        lines += [f"- {i.label()}" for i in s.shopping_list]
    if s.phase == "address":
        lines.append("Endereços salvos na conta iFood (a pessoa precisa escolher um):")
        lines.append(_numbered([a.label for a in s.addresses]))
    elif s.address:
        lines.append(f"Endereço de entrega: {s.address.label}")
    if s.phase == "compare":
        lines.append("Mercados mostrados na tela:")
        lines += [
            f"{n}. {o.name} — total {brl(o.total)} — faltando: {', '.join(o.missing) or 'nada'}"
            for n, o in enumerate(s.options, 1)
        ]
    if s.phase == "payment":
        lines.append("Formas de pagamento na tela:")
        lines.append(_numbered([o.label for o in s.payment_options]))
        if s.payment_preselected is not None:
            lines.append(f"Pré-selecionada (salva na conta, a pessoa ainda confirma): {s.payment_preselected + 1}")
    if s.phase == "voucher":
        lines.append("Cupons na tela (0 = sem cupom):")
        lines.append(_numbered([v.label for v in s.vouchers]))
    if s.phase == "checkout" and s.checkout:
        lines.append(f"Resumo do carrinho no {s.checkout.store.name} na tela; aguardando o botão 'Confirmar pedido'.")
    if s.order_id:
        lines.append(f"Pedido feito (id {s.order_id}).")
    return "\n".join(lines)


# ---------------------------------------------------------------- entry points


async def handle_message(s: Session, text: str, heard: bool = False) -> list[dict]:
    """`heard`: the message is already on screen (typed before login, replayed now)."""
    # Nothing happens before the iFood account is connected and an address is chosen.
    if not s.connected:
        s.hear(text, remember=False)
        s.pending_message = text
        return [_disconnected(s, prompts.LOGIN_REQUIRED)]
    if s.phase == "address" and not s.addresses:
        if not heard:
            s.hear(text, remember=False)
        return await ask_address(s)

    if s.phase == "need_document":
        if re.search(r"\d", text):
            return await _submit_with_document(s, text)
        s.phase = "checkout"

    if heard:
        s.remember("user", text)
    else:
        s.hear(text)

    if s.phase == "pantry" and s.recipe:
        have = parse_pantry_answer(text, len(s.recipe.ingredients))
        if have is not None:
            return await apply_pantry(s, have)

    s.activity = THINKING
    try:
        decision = await llm.generate_json(_system_prompt(s), llm.to_contents(s.history), prompts.AGENT_SCHEMA)
    except llm.RateLimited:
        return [_rate_limited(s)]
    except llm.LLMError:
        log.exception("LLM failed")
        return [s.say("Tive um probleminha para pensar na resposta. Pode repetir?", remember=False)]
    log.info("decision: %s", s.redact(json.dumps(decision, ensure_ascii=False)))

    _apply_preferences(s, decision)
    action = decision.get("action", "none")
    out = _reply(s, decision, action)

    if s.phase == "address":
        if action == "choose_address" and isinstance(decision.get("address_number"), int):
            out += await choose_address(s, decision["address_number"] - 1)
        return out or [s.say("Antes de tudo, escolha o endereço de entrega na lista acima. 📍", remember=False)]

    out += await _run_action(s, action, decision, replied=bool(out))
    if not out:
        out.append(s.say("Não entendi bem. Pode dizer de outro jeito? 🙂", remember=False))
    return out


def _apply_preferences(s: Session, decision: dict[str, Any]) -> None:
    """Servings and restrictions the LLM picked up from the conversation."""
    if isinstance(decision.get("servings"), int) and decision["servings"] > 0:
        s.servings = decision["servings"]
    if isinstance(decision.get("restrictions"), str) and decision["restrictions"].strip():
        restrictions = decision["restrictions"].strip()
        s.restrictions = None if restrictions.lower().rstrip(".") in NO_RESTRICTIONS else restrictions


def _reply(s: Session, decision: dict[str, Any], action: str) -> list[dict]:
    reply = str(decision.get("reply") or "").strip()
    # The system shows the result of an action itself; a reply there is just filler.
    if not reply or action in SYSTEM_ACTIONS:
        return []
    chips = [str(c).strip() for c in decision.get("quick_replies") or [] if str(c).strip()][:MAX_CHIPS]
    return [s.say(reply, data={"chips": chips} if chips else None)]


async def _run_action(s: Session, action: str, decision: dict[str, Any], replied: bool) -> list[dict]:
    """The step the LLM chose, when the current state allows it ([] = nothing to do)."""
    number_key = {
        "choose_store": "store_number",
        "choose_payment": "payment_number",
        "choose_voucher": "voucher_number",
    }.get(action, "")
    number = decision.get(number_key) if isinstance(decision.get(number_key), int) else None
    if action == "change_address" and s.phase != "ordered":
        return await ask_address(s)
    if action == "show_recipe" and (recipe := parse_recipe(decision.get("recipe"))):
        return show_recipe(s, recipe)
    if action == "set_pantry" and s.phase == "pantry" and s.recipe:
        have = [n for n in decision.get("have_ingredients") or [] if isinstance(n, int)]
        return await apply_pantry(s, have)
    if action == "search_stores":
        if items := parse_ingredients(decision.get("shopping_list")):
            s.shopping_list = items
        if not s.shopping_list:
            return [s.say("Antes de buscar, preciso fechar a receita com você. 🙂")]
        if s.address is None:
            return await ask_address(s)
        return await search_stores(s)
    if action == "choose_store" and s.options and number is not None:
        return await choose_store(s, number - 1)
    if action == "choose_payment" and s.payment_options and number is not None:
        return await choose_payment(s, number - 1)
    if action == "choose_voucher" and s.phase == "voucher" and number is not None:
        return await choose_voucher(s, number - 1 if number > 0 else None)
    if action == "track_order" and s.order_id:
        return await track_order(s)
    if action == "order_details" and s.order_id:
        return await order_details(s)
    if action == "restart":
        s.start_new_order()
        if not replied:
            return [s.say(prompts.ASK_CRAVING, data={"chips": await _craving_chips(s)})]
    return []


async def resume(s: Session) -> list[dict]:
    """Right after a login: getUser (once), greet, pick the address if needed, continue what was interrupted."""
    try:
        await load_profile(s)
    except ifood.Unauthorized:
        return _forget_tokens(s)
    name = s.profile.first_name if s.profile else None
    if s.address is None:
        return await ask_address(s, intro=f"{prompts.greeting(name)} Sua conta iFood está conectada. ")
    again = f"Pronto, {name}!" if name else "Pronto!"
    return [s.say(f"{again} Continuando de onde paramos.", remember=False)] + await _replay_pending(s)


async def new_conversation(s: Session) -> list[dict]:
    """ "Nova conversa" while connected: same profile (getUser only if it never loaded), new address question."""
    try:
        await load_profile(s)
    except ifood.Unauthorized:
        return _forget_tokens(s)
    return await ask_address(s, intro=f"{prompts.greeting(s.profile.first_name if s.profile else None)} ")


async def load_profile(s: Session) -> None:
    """getUser once per session. On failure: no personalization now and one retry in the background."""
    if s.profile is not None or (s.profile_retry and not s.profile_retry.done()):
        return
    try:
        s.profile = await _fetch_profile(s)
    except NotConnected, ifood.Unauthorized:
        raise ifood.Unauthorized from None
    except Exception as e:  # noqa: BLE001 - any failure only costs the personalization
        log.warning("getUser failed (%s); retrying once in the background", _error_code(e))
        s.profile_retry = asyncio.create_task(_retry_profile(s))


async def _fetch_profile(s: Session) -> user.UserProfile:
    async with _ifood(s) as client:
        return user.parse_user(await client.call("getUser", {"includeProfileData": True}))


async def _retry_profile(s: Session) -> None:
    await asyncio.sleep(PROFILE_RETRY_SECONDS)
    try:
        s.profile = await _fetch_profile(s)
        log.info("getUser retry succeeded")
    except Exception as e:  # noqa: BLE001 - a background task must not crash
        log.warning("getUser retry failed (%s); continuing without personalization", _error_code(e))


def _error_code(e: Exception) -> str:
    # Never the message: it may echo personal data.
    return getattr(e, "code", None) or type(e).__name__


def _profile_for(s: Session, address: shopping.Address | None = None) -> user.UserProfile | None:
    """getUser profile + the delivery address's habits (listAddresses); None when there is neither."""
    address = address or s.address
    if address is None or not address.habits:
        return s.profile
    return user.with_address_habits(s.profile, address.habits)


def _chips_task(s: Session, profile: user.UserProfile | None) -> tuple[str, asyncio.Task]:
    """One background suggestion per source, started on first use: the behavior profile (per address),
    or, with profile=None, the day and time only (per weekday/weekend + shift, nothing about the person)."""
    now = user.now_sp()
    if profile and profile.has_behavior:
        key = "\n".join(profile.behavior_lines())
        system, context = prompts.CHIPS_SYSTEM, "\n".join(profile.behavior_lines() + [user.now_line(now)])
    else:
        key = f"time:{now.weekday() >= 5}:{user.shift_of(now)}"
        system, context = prompts.CHIPS_TIME_SYSTEM, user.now_line(now)
    if key not in s.chips_tasks:
        s.chips_tasks[key] = asyncio.create_task(_suggest_chips(system, context))
    return key, s.chips_tasks[key]


async def _suggest_chips(system: str, context: str) -> list[str] | None:
    """Dish ideas from behavior lines or the time only (never the name or payments).
    [] = the profile says nothing about food; None = the LLM failed (try again)."""
    try:
        answer = await llm.generate_json(system, context, prompts.CHIPS_SCHEMA, timeout=CHIPS_LLM_TIMEOUT)
    except llm.LLMError as e:
        log.warning("craving chips suggestion failed (%s); trying again", type(e).__name__)
        return None
    chips: list[str] = []
    for raw in answer.get("chips") or []:
        chip = re.sub(r"\s+", " ", str(raw)).strip(" .")
        if chip and len(chip) <= MAX_CHIP_CHARS and chip.lower() not in (c.lower() for c in chips):
            chips.append(chip)
    return chips[:MAX_CHIPS]


async def _craving_chips(s: Session) -> list[str]:
    """Chips from the behavior profile, else from the time (no data, or nothing about food in it).

    No fixed fallback and no time limit: a failed suggestion is retried until the LLM answers.
    """
    profile = _profile_for(s)
    sources = [profile, None] if profile and profile.has_behavior else [None]
    delay = CHIPS_RETRY_SECONDS
    while True:
        for source in sources:
            s.activity = "Preparando sugestões…"
            key, task = _chips_task(s, source)
            chips = await asyncio.shield(task)
            if chips:
                return chips
            if chips is None or source is None:
                s.chips_tasks.pop(key, None)  # failed (or an empty time answer): ask this source again
                break  # only "nothing about food" in the profile moves on to the time chips
        await asyncio.sleep(delay)
        delay = min(delay * 2, CHIPS_RETRY_MAX_SECONDS)


async def _replay_pending(s: Session) -> list[dict]:
    if s.pending is not None:
        operation, failure = s.pending
        s.pending = None
        return await _run(s, operation, failure)
    if s.pending_message is not None:
        text, s.pending_message = s.pending_message, None
        return await handle_message(s, text, heard=True)
    return []


async def ask_address(s: Session, intro: str = "") -> list[dict]:
    """List the account's saved addresses so the user picks where they are now."""
    s.phase = "address"
    s.addresses = []
    s.options = []
    s.clear_cart()

    async def operation() -> list[dict]:
        async with _ifood(s) as client:
            s.addresses = shopping.parse_addresses(await client.call("listAddresses", {}))
        if not s.addresses:
            return [
                s.say(
                    f"{intro}Não encontrei nenhum endereço salvo na sua conta iFood. 🏠 "
                    "Cadastre um no app do iFood e me mande qualquer mensagem aqui que eu procuro de novo.",
                    remember=False,
                )
            ]
        # The first address is the most likely pick: its chips get ready while the user chooses.
        _chips_task(s, _profile_for(s, s.addresses[0]))
        return [
            s.say(
                f"{intro}Onde você está agora? Escolha o endereço de entrega:",
                kind="addresses",
                data={"options": views.options_view([a.label for a in s.addresses])},
                remember=False,
            )
        ]

    return await _run(
        s, operation, "Não consegui carregar seus endereços do iFood. Me mande qualquer mensagem que eu tento de novo."
    )


async def choose_address(s: Session, index: int, from_button: bool = False) -> list[dict]:
    if s.phase != "address" or not 0 <= index < len(s.addresses):
        return [s.say("Não encontrei esse endereço. Escolha um da lista. 🙂", remember=False)]
    s.address = s.addresses[index]
    if from_button:
        s.hear(f"Estou em {s.address.label}")
    s.phase = "pantry" if s.recipe and not s.shopping_list else "chat"
    if s.pending or s.pending_message:
        # Something was interrupted by the login: continue it now.
        return [s.say(f"Entrega em **{s.address.label}**. 📍", remember=False)] + await _replay_pending(s)
    if s.shopping_list:
        # The address changed after a search: stores and fees depend on it.
        return [s.say(f"Endereço atualizado para **{s.address.label}**. 📍")] + await search_stores(s)
    return [
        s.say(
            f"Combinado, entrega em **{s.address.label}**. 📍\n\n{prompts.ASK_CRAVING}",
            data={"chips": await _craving_chips(s)},
        )
    ]


def show_recipe(s: Session, recipe: Recipe) -> list[dict]:
    """Recipe first (card with the numbered ingredients), then the pantry question."""
    if recipe.servings:
        s.servings = recipe.servings
    s.recipe = recipe
    s.phase = "pantry"
    s.shopping_list = []
    s.options = []
    s.clear_cart()
    message = s.say(
        prompts.ASK_PANTRY,
        kind="recipe",
        data={**recipe.view(), "chips": prompts.PANTRY_CHIPS},
        history_text=f"{recipe.as_text()}\n\n{prompts.ASK_PANTRY}",
    )
    s.recipe_message = message["id"]
    return [message]


async def apply_pantry(s: Session, have: list[int]) -> list[dict]:
    """Only the ingredients the user doesn't have go to the store search."""
    assert s.recipe is not None
    s.recipe.have = sorted({n for n in have if 1 <= n <= len(s.recipe.ingredients)})
    s.update(s.recipe_message, {**s.recipe.view(), "chips": prompts.PANTRY_CHIPS})
    s.shopping_list = [i for n, i in enumerate(s.recipe.ingredients, 1) if n not in s.recipe.have]
    if not s.shopping_list:
        s.phase = "chat"
        return [s.say("Você já tem tudo! Nada para comprar desta vez. Bom apetite! 🍽️")]
    out = [s.say(f"Anotado. Vou comprar só o que falta: **{', '.join(i.name for i in s.shopping_list)}**.")]
    if s.address is None:
        return out + await ask_address(s)
    return out + await search_stores(s)


async def search_stores(s: Session) -> list[dict]:
    async def operation() -> list[dict]:
        assert s.address is not None
        s.clear_cart()

        def progress(text: str) -> None:
            s.activity = text

        async with _ifood(s) as client:
            location = {"latitude": s.address.latitude, "longitude": s.address.longitude}
            options, not_found = await shopping.find_store_options(
                client, s.shopping_list, location, s.restrictions, progress
            )

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
        text = f"Comparei {len(options)} mercado(s) aberto(s) que entregam em **{s.address.label}**:"
        if not_found:
            text += (
                f"\n\n⚠️ Não encontrei **{', '.join(not_found)}** em nenhum mercado aberto. "
                "Se escolher um mercado, o pedido vai sem esses itens."
            )
        summary = _numbered(
            [f"{o.name}: total {brl(o.total)}, faltando: {', '.join(o.missing) or 'nada'}" for o in options]
        )
        return [
            s.say(
                text,
                kind="stores",
                data=views.stores_view(s.options, s.shopping_list, s.restrictions, not_found),
                history_text=f"{text}\n{summary}\nQual mercado você prefere?",
            )
        ]

    return await _run(s, operation, "Não consegui buscar os mercados agora. Tenta de novo?")


async def choose_store(s: Session, index: int, from_button: bool = False) -> list[dict]:
    if not 0 <= index < len(s.options) or s.phase not in ("compare", "payment", "voucher", "checkout"):
        return [s.say("Não encontrei esse mercado. Escolha um dos mercados da comparação. 🙂", remember=False)]
    store = s.options[index]
    if from_button:
        s.hear(f"Quero o mercado {store.name}")
    # Below the minimum the order can't go through: no cart yet, the user adds items (search_stores) or picks another.
    if store.below_minimum:
        return [
            s.say(
                f"O **{store.name}** tem pedido mínimo de **{brl(store.minimum_order)}** e sua lista lá dá "
                f"{brl(store.subtotal)}: faltam **{brl(store.short_of_minimum)}**. Quer adicionar mais algum item? "
                "É só me dizer o quê. Se preferir, escolha outro mercado da comparação."
            )
        ]

    async def operation() -> list[dict]:
        assert s.address is not None
        s.clear_cart()
        async with _ifood(s) as client:
            existing = s.guard.cart_for(store.merchant_id)
            try:
                cart_id = await shopping.prepare_cart(client, store, s.address, existing)
            except IFoodError:
                if not existing:
                    raise
                log.warning("cart %s could not be resumed, creating a new one", existing)
                s.guard.drop(existing)
                cart_id = await shopping.prepare_cart(client, store, s.address, None)
            payment_options = await shopping.list_payments(client, store.merchant_id)
        s.store, s.cart_id, s.payment_options = store, cart_id, payment_options
        if not payment_options:
            s.phase = "compare"
            return [
                s.say(f"O {store.name} não tem nenhuma forma de pagamento disponível pra você. Quer outro mercado?")
            ]
        s.phase = "payment"
        s.payment_preselected = user.preferred_payment(s.profile, payment_options)
        text = f"Carrinho pronto no **{store.name}**. Como você quer pagar?"
        if s.payment_preselected is not None:
            saved = payment_options[s.payment_preselected].label
            text = (
                f"Carrinho pronto no **{store.name}**. Deixei marcado **{saved}**, que está salvo na sua conta. "
                "Confirme ou escolha outra forma:"
            )
        if store.missing:
            text = f"⚠️ Vai sem: {', '.join(store.missing)}.\n\n" + text
        selected = s.payment_preselected + 1 if s.payment_preselected is not None else None
        return [
            s.say(
                text,
                kind="payment",
                data={
                    "options": [
                        {"number": n, "label": o.label, "offline": o.offline} for n, o in enumerate(payment_options, 1)
                    ],
                    "selected": selected,
                },
                history_text=f"{text}\n{_numbered([o.label for o in payment_options])}",
            )
        ]

    return await _run(s, operation, f"Não consegui montar o carrinho no {store.name}. Quer tentar outro mercado?")


async def choose_payment(s: Session, index: int, from_button: bool = False) -> list[dict]:
    if s.phase not in ("payment", "voucher", "checkout") or not 0 <= index < len(s.payment_options):
        return [s.say("Escolha uma das formas de pagamento da lista. 🙂", remember=False)]
    option = s.payment_options[index]
    if from_button:
        s.hear(f"Pagar com {option.label}")

    async def operation() -> list[dict]:
        assert s.cart_id is not None
        async with _ifood(s) as client:
            await shopping.set_payment(client, s.cart_id, option)
            s.payment, s.voucher = option, None
            s.vouchers = await shopping.list_vouchers(client, s.cart_id)
            if not s.vouchers:
                return await _review(s, client)
        s.phase = "voucher"
        text = "Você tem cupom disponível para este pedido. Quer usar algum?"
        return [
            s.say(
                text,
                kind="vouchers",
                data={"options": views.options_view([v.label for v in s.vouchers])},
                history_text=f"{text}\n{_numbered([v.label for v in s.vouchers])}\n0. Sem cupom",
            )
        ]

    return await _run(s, operation, "Não consegui aplicar essa forma de pagamento. Quer escolher outra?")


async def choose_voucher(s: Session, index: int | None, from_button: bool = False) -> list[dict]:
    """index None = no coupon."""
    if s.phase != "voucher" or (index is not None and not 0 <= index < len(s.vouchers)):
        return [s.say("Escolha um dos cupons da lista ou siga sem cupom. 🙂", remember=False)]
    voucher = s.vouchers[index] if index is not None else None
    if from_button:
        s.hear(f"Usar o cupom {voucher.label}" if voucher else "Seguir sem cupom")

    async def operation() -> list[dict]:
        assert s.cart_id is not None
        async with _ifood(s) as client:
            if voucher:
                await shopping.set_voucher(client, s.cart_id, voucher)
            s.voucher = voucher
            return await _review(s, client)

    return await _run(s, operation, "Não consegui aplicar o cupom. Quer seguir sem ele?")


async def _review(s: Session, client: GuardedClient) -> list[dict]:
    """getCart → summary card. The only step that leads to the confirm button."""
    assert s.store and s.address and s.cart_id and s.payment
    s.checkout = await shopping.review(
        client, s.cart_id, s.store, s.address, s.payment.label, s.voucher.label if s.voucher else None
    )
    s.phase = "checkout"
    text = "Confira o resumo. Se estiver tudo certo, toque em **Confirmar pedido**."
    return [s.say(text, kind="summary", data=views.checkout_view(s.checkout))]


async def confirm_order(s: Session, document: str | None = None) -> list[dict]:
    if s.phase != "checkout" or not s.checkout:
        return [s.say("Não há nenhum carrinho aguardando confirmação.", remember=False)]
    if document and document.strip():
        valid = valid_document(document)
        if not valid:
            return [s.say("Esse documento não parece válido. Confira o CPF/CNPJ ou passaporte. 🙂", remember=False)]
        document = valid
    else:
        document = None
    s.hear("✅ Confirmar pedido")
    s.guard.grant_consent(s.checkout.cart_id)
    return await _submit(s, document)


async def _submit_with_document(s: Session, text: str) -> list[dict]:
    # Keep the document out of the LLM history and off the screen.
    s.hear("•" * len(re.sub(r"\s", "", text)), history_text="[documento informado]")
    document = valid_document(text)
    if not document:
        return [s.say("Hmm, isso não parece um CPF/CNPJ ou passaporte válido. Pode conferir?", remember=False)]
    s.phase = "checkout"
    assert s.checkout is not None
    s.guard.grant_consent(s.checkout.cart_id)
    return await _submit(s, document)


async def _submit(s: Session, document: str | None) -> list[dict]:
    assert s.checkout is not None
    checkout = s.checkout

    if config.DRY_RUN:
        # Everything up to here ran against iFood; only submitOrder is skipped.
        log.info("DRY_RUN: submitOrder skipped for cart %s (document %s)", checkout.cart_id, bool(document))
        s.order_id, s.short_code, s.dry_run_step = f"demo-{secrets.token_hex(3)}", "DEMO", 0
        s.phase = "ordered"
        out = [
            s.say(
                f"Pedido simulado no **{checkout.store.name}** (modo de teste: nada foi enviado ao iFood). 🧪",
                kind="order",
                data={"order_id": s.order_id, "short_code": s.short_code, "dry_run": True},
            )
        ]
        return out + [s.say("", kind="tracker", data=_simulated_track(s), remember=False)]

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
                    return [
                        s.say(
                            "O iFood pediu um documento para finalizar. Qual o seu **CPF** (só números)? "
                            "Para contas estrangeiras, o número do passaporte."
                        )
                    ]
                raise
            order_id = first_value(result, "orderId") or first_value(result, "id")
            if not order_id:
                # Never announce an order iFood didn't confirm with an id.
                raise IFoodError("submitOrder", "resposta sem orderId")
            s.order_id = str(order_id)
            s.short_code = first_value(result, "shortCode")
            s.phase = "ordered"
            text = f"Pedido feito no **{checkout.store.name}**! 🎉"
            if s.short_code:
                text += f" Código: **{s.short_code}**."
            out = [s.say(text, kind="order", data={"order_id": s.order_id, "short_code": s.short_code})]
            tracker = tracking.tracker_view(
                s.order_id, s.short_code, await client.call("trackOrder", {"orderId": s.order_id})
            )
            out.append(s.say("", kind="tracker", data=tracker, remember=False))
            return out

    return await _run(
        s,
        operation,
        "Não consegui finalizar o pedido. Antes de tentar de novo, confira no app do iFood se ele não foi criado.",
    )


def _simulated_track(s: Session) -> dict[str, Any]:
    view = tracking.simulated_view(s.order_id, s.short_code, s.dry_run_step)
    s.dry_run_step += 1
    return view


async def track_order(s: Session, refresh: bool = False) -> list[dict]:
    """refresh=True (automatic polling) updates the tracker card on screen instead of adding one."""
    tracker_message = next((m for m in reversed(s.messages) if m["kind"] == "tracker"), None)

    def show(view: dict[str, Any]) -> list[dict]:
        if refresh and tracker_message:
            return [m for m in [s.update(tracker_message["id"], view)] if m]
        return [s.say("", kind="tracker", data=view, remember=False)]

    if config.DRY_RUN and s.order_id and s.order_id.startswith("demo-"):
        return show(_simulated_track(s))

    async def operation() -> list[dict]:
        async with _ifood(s) as client:
            data = await client.call("trackOrder", {"orderId": s.order_id})
        return show(tracking.tracker_view(s.order_id, s.short_code, data))

    return await _run(s, operation, "Não consegui consultar o status do pedido agora.")


async def order_details(s: Session) -> list[dict]:
    if s.order_id and s.order_id.startswith("demo-") and s.checkout:
        c = s.checkout
        lines = [f"🧾 **Pedido simulado** — {c.store.name}"]
        lines += [f"- {i['quantity']}x {i['name']} — {i['price']}" for i in c.items]
        lines += [f"\n**Pagamento:** {c.payment_label}", f"**Entrega:** {c.address.label}"]
        lines += [f"**{label}:** {value}" for label, value in c.lines if label.startswith("Total")]
        return [s.say("\n".join(lines))]

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
