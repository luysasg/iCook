"""The iFood MCP checkout rules, enforced in code on every tool call.

The LLM never calls MCP tools, but every code path that does goes through this layer,
so a bug can't skip a step, invent an ID or place an order without the user's consent:

- IDs (delivery, payment, card token, voucher, option, cartItemId) must come from the
  matching list/get tool response, never be hardcoded.
- addItemToCart / updateCartItem / createCart items always carry an `options` array.
- A merchant with an open cart is resumed with getCart, not recreated.
- submitOrder needs delivery + payment set, a getCart after the last change, the user's
  explicit confirmation (`grant_consent`) and userConsent=true; never in DRY_RUN.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import config
from .ifood import Caller, IFoodError
from .jsonutil import cart_id_of, dicts_with, first_value
from .shopping import parse_payment_options, parse_vouchers

log = logging.getLogger("guard")

MUTATIONS = {
    "addItemToCart", "updateCartItem", "removeItemFromCart", "setCartDelivery", "setCartPayment", "setVoucher",
    "removeVoucher",
}


class GuardError(IFoodError):
    """A call that breaks the documented flow; it is never sent to iFood."""


@dataclass
class CartState:
    cart_id: str
    merchant_id: str
    cart_items: dict[str, str] = field(default_factory=dict)  # cartItemId -> itemId
    delivery_ids: set[str] = field(default_factory=set)
    vouchers: set[tuple[str, str]] = field(default_factory=set)  # (code, campaignId)
    delivery_set: bool = False
    payment_set: bool = False
    reviewed: bool = False  # getCart ran after the last change
    consent: bool = False  # the user confirmed this exact cart state
    submitted: bool = False


class Guard:
    def __init__(self) -> None:
        self.carts: dict[str, CartState] = {}
        self.payments: dict[str, set[tuple[str, str | None]]] = {}  # merchantId -> (paymentMethodId, cardTokenId)
        self.item_options: dict[str, set[str]] = {}  # itemId -> optionId/customizationId from getItem

    # ------------------------------------------------------------ used by the agent

    def cart_for(self, merchant_id: str) -> str | None:
        """The open cart for this merchant, if any (to resume instead of recreating)."""
        return next(
            (c.cart_id for c in self.carts.values() if c.merchant_id == merchant_id and not c.submitted), None
        )

    def drop(self, cart_id: str) -> None:
        """Forget a cart iFood no longer knows (expired)."""
        self.carts.pop(cart_id, None)

    def grant_consent(self, cart_id: str) -> None:
        self._cart(cart_id, "submitOrder").consent = True

    # ------------------------------------------------------------ checks

    def check(self, tool: str, args: dict[str, Any]) -> None:
        if tool == "createCart":
            self._check_create(args)
            return
        if tool in ("addItemToCart", "updateCartItem"):
            self._check_options(tool, str(args.get("itemId")), args.get("options"))
        if tool in ("updateCartItem", "removeItemFromCart"):
            cart = self._cart(args.get("cartId"), tool)
            item_id = cart.cart_items.get(str(args.get("cartItemId")))
            if item_id is None:
                raise GuardError(tool, "cartItemId desconhecido: use o que veio no getCart/addItemToCart")
            if tool == "updateCartItem" and item_id != str(args.get("itemId")):
                raise GuardError(tool, "itemId não corresponde ao cartItemId")
        if tool == "setCartDelivery":
            cart = self._cart(args.get("cartId"), tool)
            if str(args.get("deliveryMethodId")) not in cart.delivery_ids:
                raise GuardError(tool, "deliveryMethodId não veio do getCartDeliveryMethods")
        if tool == "setCartPayment":
            cart = self._cart(args.get("cartId"), tool)
            choice = (str(args.get("paymentMethodId")), args.get("cardTokenId") or None)
            if choice not in self.payments.get(cart.merchant_id, set()):
                raise GuardError(tool, "forma de pagamento/cardTokenId não veio do listPaymentMethods deste mercado")
        if tool == "setVoucher":
            cart = self._cart(args.get("cartId"), tool)
            if (str(args.get("code")), str(args.get("campaignId"))) not in cart.vouchers:
                raise GuardError(tool, "cupom (code + campaignId) não veio do listVouchers")
        if tool == "submitOrder":
            self._check_submit(args)
        elif "cartId" in args:
            self._cart(args["cartId"], tool)

    def _check_create(self, args: dict[str, Any]) -> None:
        if args.get("orderId"):
            return
        merchant_id = str(args.get("merchantId"))
        if self.cart_for(merchant_id):
            raise GuardError("createCart", "este mercado já tem um carrinho aberto: retome com getCart")
        for item in args.get("items") or []:
            self._check_options("createCart", str(item.get("itemId")), item.get("options"))

    def _check_options(self, tool: str, item_id: str, options: Any) -> None:
        if not isinstance(options, list):
            raise GuardError(tool, "options é obrigatório (use [] quando o item não tem opções)")
        known = self.item_options.get(item_id, set())
        for option in options:
            ids = [option.get("optionId")] + [c.get("customizationId") for c in option.get("customizations") or []]
            if any(str(i) not in known for i in ids):
                raise GuardError(tool, "optionId/customizationId não veio do getItem deste item")

    def _check_submit(self, args: dict[str, Any]) -> None:
        if config.DRY_RUN:
            raise GuardError("submitOrder", "modo demonstração (DRY_RUN): o pedido não é enviado")
        cart = self._cart(args.get("cartId"), "submitOrder")
        missing = [
            label
            for label, ok in (
                ("entrega", cart.delivery_set),
                ("pagamento", cart.payment_set),
                ("getCart após a última mudança", cart.reviewed),
                ("confirmação explícita da pessoa", cart.consent),
            )
            if not ok
        ]
        if args.get("userConsent") is not True:
            missing.append("userConsent=true")
        if missing:
            raise GuardError("submitOrder", "faltando: " + ", ".join(missing))

    def _cart(self, cart_id: Any, tool: str) -> CartState:
        cart = self.carts.get(str(cart_id))
        if cart is None:
            raise GuardError(tool, "cartId desconhecido: use o do createCart/getCart")
        return cart

    # ------------------------------------------------------------ bookkeeping from responses

    def record(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        if tool == "createCart":
            cart_id = cart_id_of(result)
            if cart_id:
                cart = CartState(cart_id, str(args.get("merchantId") or first_value(result, "merchantId")))
                self.carts[cart.cart_id] = cart
                self._record_items(cart, result)
            return
        if tool == "listPaymentMethods":
            self.payments[str(args.get("merchantId"))] = {
                (o.method_id, o.card_token) for o in parse_payment_options(result)
            }
            return
        if tool == "getItem":
            self.item_options[str(args.get("itemId"))] = {
                str(d[key]) for key in ("id", "optionId", "customizationId") for d in dicts_with(result, key)
            }
            return
        cart = self.carts.get(str(args.get("cartId")))
        if cart is None:
            return
        if tool == "getCart":
            cart.cart_items = {}
            self._record_items(cart, result)
            cart.reviewed = True
        elif tool == "getCartDeliveryMethods":
            cart.delivery_ids = {str(d["deliveryMethodId"]) for d in dicts_with(result, "deliveryMethodId")}
        elif tool == "listVouchers":
            cart.vouchers = {(v.code, v.campaign_id) for v in parse_vouchers(result)}
        elif tool == "submitOrder":
            cart.submitted = True
        if tool in MUTATIONS:
            cart.reviewed = cart.consent = False
            if tool == "setCartDelivery":
                cart.delivery_set = True
            elif tool == "setCartPayment":
                cart.payment_set = True
            elif tool == "addItemToCart":
                # Keep the new cartItemId (itemId, cartItemId and cartId are never mixed up).
                for line in dicts_with(result, "cartItemId"):
                    if str(line.get("itemId", args.get("itemId"))) == str(args.get("itemId")):
                        cart.cart_items[str(line["cartItemId"])] = str(args.get("itemId"))
            elif tool == "removeItemFromCart":
                cart.cart_items.pop(str(args.get("cartItemId")), None)

    @staticmethod
    def _record_items(cart: CartState, result: dict[str, Any]) -> None:
        for line in dicts_with(result, "cartItemId"):
            if line.get("itemId"):
                cart.cart_items[str(line["cartItemId"])] = str(line["itemId"])


class GuardedClient:
    """Wraps an MCP client: check → call → record. Also reports which tool is running."""

    def __init__(self, client: Caller, guard: Guard, on_call: Callable[[str], None] | None = None):
        self._client = client
        self._guard = guard
        self._on_call = on_call

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self._guard.check(tool, arguments)
        except GuardError as e:
            log.warning("blocked %s: %s", tool, e.message)
            raise
        if self._on_call:
            self._on_call(tool)
        result = await self._client.call(tool, arguments)
        self._guard.record(tool, arguments, result)
        return result
