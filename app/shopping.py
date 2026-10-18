"""Grocery search, store comparison and cart building.

All iFood calls here are made by code, not by the LLM: one search per ingredient,
gap-filling searches inside the best stores, and a single LLM call to match
search results to ingredients and round quantities up.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import config, llm, prompts
from .ifood import Caller, IFoodError
from .jsonutil import as_number, brl, cart_id_of, dicts_with, first_value, last_digits

log = logging.getLogger("shopping")

SEARCH_CONCURRENCY = 4
CANDIDATES_PER_INGREDIENT = 4
# Results per searchOffers call: the wide search across stores, then gap-filling inside one store.
SEARCH_PAGE_SIZE = 25
GAP_SEARCH_PAGE_SIZE = 5
# Ranking tie-breaker for stores that don't report a distance (sorts them last).
UNKNOWN_DISTANCE_KM = 99.0
# Product matching sees every store at once, so it gets longer than config.LLM_TIMEOUT.
PICKER_LLM_TIMEOUT = 45
PICKUP_WORDS = ("PICKUP", "TAKEOUT", "RETIRADA")
OFFLINE_PAYMENT_WORDS = ("CASH", "DINHEIRO", "OFFLINE", "NA ENTREGA")
CARD_WORDS = ("CARD", "CREDIT", "DEBIT", "CRÉDITO", "CREDITO", "DÉBITO", "DEBITO", "CARTÃO", "CARTAO")
SUMMARY_SECTIONS = ("summary", "totals", "totalizers", "prices", "values", "amounts")
ALLERGEN_LABELS = ("compatible", "uncertain", "avoid")
# Per-address order analytics from listAddresses (on the address or under usagePattern).
ADDRESS_HABIT_KEYS = ("topMerchantTypes", "topWeekDayShift", "ordersByMerchantTypeWeekdayAndShift", "ordersByDay")


@dataclass
class Ingredient:
    name: str
    search_term: str
    quantity: float
    unit: str

    def label(self) -> str:
        return f"{self.name} ({self.quantity:g} {self.unit})"


@dataclass
class Product:
    item_id: str
    name: str
    price: float


@dataclass
class Pick:
    index: int  # position in the shopping list
    ingredient: str
    product: Product
    quantity: int
    allergen: str | None = None  # compatible | uncertain | avoid, only when the user has restrictions

    @property
    def total(self) -> float:
        return self.product.price * self.quantity

    def label(self) -> str:
        return f"{self.quantity}x {self.product.name} — {brl(self.total)}"

    def view(self) -> dict[str, Any]:
        return {"label": self.label(), "allergen": self.allergen}


@dataclass
class StoreOption:
    merchant_id: str
    name: str
    rating: float | None
    distance_km: float | None
    eta: tuple[int, int] | None
    delivery_fee: float | None
    minimum_order: float | None
    picks: list[Pick] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return sum(p.total for p in self.picks)

    @property
    def total(self) -> float:
        return self.subtotal + (self.delivery_fee or 0)

    @property
    def below_minimum(self) -> bool:
        return self.minimum_order is not None and self.subtotal < self.minimum_order

    @property
    def short_of_minimum(self) -> float:
        """How much the subtotal lacks to reach the store's minimum order (0 = nothing)."""
        return max((self.minimum_order or 0) - self.subtotal, 0)


@dataclass
class Address:
    address_id: str
    label: str
    latitude: float
    longitude: float
    habits: dict[str, Any] = field(default_factory=dict)  # ADDRESS_HABIT_KEYS present on this address


@dataclass
class PaymentOption:
    method_id: str
    card_token: str | None  # only for saved cards
    label: str
    offline: bool  # paid on delivery


@dataclass
class Voucher:
    code: str
    campaign_id: str
    label: str


@dataclass
class Checkout:
    cart_id: str
    store: StoreOption
    address: Address
    payment_label: str
    voucher_label: str | None
    items: list[dict[str, Any]]  # {quantity, name, price, allergen}
    lines: list[tuple[str, str]]
    warnings: list[str]


# ---------------------------------------------------------------- address


def parse_addresses(data: dict[str, Any]) -> list[Address]:
    """Saved addresses with coordinates: last used first, then most used in 30 days."""
    raw = sorted(
        dicts_with(data, "addressId"),
        key=lambda a: (first_value(a, "isLastUsedAddress") is True, first_value(a, "isMostUsedAddress30d") is True),
        reverse=True,
    )
    addresses = []
    for entry in raw:
        coords = first_value(entry, "coordinates") or entry
        latitude = as_number(first_value(coords, "latitude", "lat"))
        longitude = as_number(first_value(coords, "longitude", "lng", "lon"))
        if latitude is None or longitude is None:
            log.warning("address %s has no coordinates, skipping", entry.get("addressId"))
            continue
        alias = entry.get("alias") if isinstance(entry.get("alias"), str) else None
        street = next(
            (v for k in ("formattedAddress", "location", "streetName", "address") if isinstance(v := entry.get(k), str)),
            None,
        )
        if alias and street:
            label = f"{alias.title()} ({street})"
        else:
            label = alias or street or "endereço salvo"
        usage = entry.get("usagePattern") if isinstance(entry.get("usagePattern"), dict) else {}
        habits = {k: v for k in ADDRESS_HABIT_KEYS if (v := entry.get(k, usage.get(k))) not in (None, "", [], {})}
        addresses.append(Address(str(entry["addressId"]), label, latitude, longitude, habits))
    return addresses


# ---------------------------------------------------------------- search & compare


def _price(item: dict[str, Any]) -> float | None:
    prices = [p for k in ("promotionalUnitPrice", "unitPrice") if (p := as_number(item.get(k)))]
    return min(prices) if prices else None


def _store_from(merchant: dict[str, Any]) -> StoreOption:
    delivery = merchant.get("deliveryMethod") or {}
    minutes = delivery.get("estimatedMinutes") or {}
    fee = as_number(delivery.get("promotionalFee"))
    if fee is None:
        fee = as_number(delivery.get("fee"))
    eta = (int(minutes["min"]), int(minutes["max"])) if "min" in minutes and "max" in minutes else None
    return StoreOption(
        merchant_id=merchant["merchantId"],
        name=merchant.get("merchantName") or "Mercado",
        rating=as_number(merchant.get("reviewRating")),
        distance_km=as_number(merchant.get("straightLineDistanceKm")),
        eta=eta,
        delivery_fee=fee,
        minimum_order=as_number(merchant.get("minimumOrderAmount")),
    )


async def find_store_options(
    ifood: Caller,
    ingredients: list[Ingredient],
    location: dict[str, float],
    restrictions: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[StoreOption], list[str]]:
    """Returns (store options sorted best-first, ingredients no compared store has)."""
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def search(term: str, merchant_id: str | None = None, page_size: int = SEARCH_PAGE_SIZE) -> list[dict[str, Any]]:
        refinements: dict[str, Any] = {"onlyOpen": True}
        if merchant_id:
            refinements["merchantId"] = merchant_id
        args = {
            "query": term,
            "merchantType": "GROCERIES",
            "resultKind": "ITEM",
            "pageSize": page_size,
            "userLocation": location,
            "refinements": refinements,
        }
        async with semaphore:
            try:
                data = await ifood.call("searchOffers", args)
            except IFoodError as e:
                log.warning("search failed for %r: %s", term, e)
                return []
        return [r for r in data.get("results") or [] if isinstance(r, dict) and r.get("itemId")]

    stores: dict[str, StoreOption] = {}
    candidates: dict[str, dict[int, list[Product]]] = {}

    def collect(index: int, results: list[dict[str, Any]]) -> None:
        for item in results:
            merchant = item.get("merchant") or {}
            merchant_id = merchant.get("merchantId")
            price = _price(item)
            if not merchant_id or price is None or merchant.get("isOpen") is False:
                continue
            stores.setdefault(merchant_id, _store_from(merchant))
            bucket = candidates.setdefault(merchant_id, {}).setdefault(index, [])
            if len(bucket) < CANDIDATES_PER_INGREDIENT and all(p.item_id != item["itemId"] for p in bucket):
                bucket.append(Product(item["itemId"], item.get("itemName") or "?", price))

    results = await asyncio.gather(*(search(i.search_term) for i in ingredients))
    for index, items in enumerate(results):
        collect(index, items)
    if not stores:
        return [], [i.name for i in ingredients]

    # Best coverage first, closest as tie-breaker; then look for what's missing inside each of them.
    ranked = sorted(
        stores.values(), key=lambda s: (-len(candidates.get(s.merchant_id, {})), s.distance_km or UNKNOWN_DISTANCE_KM)
    )
    top = ranked[: config.MAX_STORES]
    gaps = [
        (s.merchant_id, i)
        for s in top
        for i in range(len(ingredients))
        if i not in candidates.get(s.merchant_id, {})
    ]
    gap_results = await asyncio.gather(
        *(search(ingredients[i].search_term, merchant_id, page_size=GAP_SEARCH_PAGE_SIZE) for merchant_id, i in gaps)
    )
    for (merchant_id, i), items in zip(gaps, gap_results, strict=True):
        collect(i, [r for r in items if (r.get("merchant") or {}).get("merchantId") == merchant_id])

    if progress:
        progress("Comparando produtos e preços…")
    picks = await _match_products(ingredients, top, candidates, restrictions)
    for store in top:
        store.picks = picks.get(store.merchant_id, [])
        found = {p.index for p in store.picks}
        store.missing = [ing.name for i, ing in enumerate(ingredients) if i not in found]

    top = [s for s in top if s.picks]
    not_found = [ing.name for ing in ingredients if all(ing.name in s.missing for s in top)]
    top.sort(key=lambda s: (len(s.missing), s.total))
    return top, not_found


async def _match_products(
    ingredients: list[Ingredient],
    stores: list[StoreOption],
    candidates: dict[str, dict[int, list[Product]]],
    restrictions: str | None = None,
) -> dict[str, list[Pick]]:
    """One LLM call: pick the right product per ingredient per store and round quantities up."""
    refs: dict[str, tuple[str, int, Product]] = {}
    lines = [f"Restrições alimentares da pessoa: {restrictions or 'nenhuma'}", "", "Ingredientes:"]
    lines += [
        f"{n}. {ing.name} — precisa de {ing.quantity:g} {ing.unit} (busca: {ing.search_term})"
        for n, ing in enumerate(ingredients, 1)
    ]
    for store_number, store in enumerate(stores, 1):
        code = f"M{store_number}"
        lines.append(f"\nMercado {code} ({store.name}):")
        for index in range(len(ingredients)):
            products = candidates.get(store.merchant_id, {}).get(index, [])
            if not products:
                lines.append(f"  Ingrediente {index + 1}: sem candidatos")
                continue
            lines.append(f"  Ingrediente {index + 1}:")
            for product_number, product in enumerate(products, 1):
                ref = f"{code}-{index + 1}-{product_number}"
                refs[ref] = (store.merchant_id, index, product)
                lines.append(f"    [{ref}] {product.name} — {brl(product.price)}")

    picks: dict[str, list[Pick]] = {s.merchant_id: [] for s in stores}
    if not refs:
        return picks
    data = await llm.generate_json(
        prompts.PICKER_SYSTEM, "\n".join(lines), prompts.PICKER_SCHEMA, temperature=0.1, timeout=PICKER_LLM_TIMEOUT
    )
    for store_entry in data.get("stores") or []:
        for entry in store_entry.get("picks") or []:
            ref = str(entry.get("item_ref") or "").strip().strip("[]")
            if ref not in refs:
                continue
            merchant_id, index, product = refs[ref]
            if any(p.index == index for p in picks[merchant_id]):
                continue
            quantity = max(1, int(entry.get("quantity") or 1))
            allergen = entry.get("allergen") if restrictions else None
            if restrictions and allergen not in ALLERGEN_LABELS:
                allergen = "uncertain"  # never imply a product is safe without a clear signal
            picks[merchant_id].append(Pick(index, ingredients[index].name, product, quantity, allergen))
    for store_picks in picks.values():
        store_picks.sort(key=lambda p: p.index)
    return picks


# ---------------------------------------------------------------- cart


def pick_delivery(data: dict[str, Any]) -> str:
    """ASAP home delivery: skip pickup methods, prefer DEFAULT."""
    methods = dicts_with(data, "deliveryMethodId")
    if not methods:
        raise IFoodError("getCartDeliveryMethods", "nenhum método de entrega disponível")

    def kind(m: dict[str, Any]) -> str:
        return f"{m.get('deliveryMethodMode', '')} {m.get('mode', '')} {m['deliveryMethodId']}".upper()

    delivery = [m for m in methods if not any(w in kind(m) for w in PICKUP_WORDS)] or methods
    best = next((m for m in delivery if str(m["deliveryMethodId"]).upper() == "DEFAULT"), delivery[0])
    return str(best["deliveryMethodId"])


def _text(d: dict[str, Any]) -> str:
    return " ".join(str(d.get(k, "")) for k in ("name", "type", "description", "method", "paymentMethodId")).upper()


def _payment_label(method: dict[str, Any], parent: dict[str, Any] | None) -> str:
    name = next(
        (v for v in (method.get("brand"), method.get("name"), (parent or {}).get("name"), method.get("type"))
         if isinstance(v, str) and v),
        "forma de pagamento",
    )
    digits = last_digits(method)
    label = f"{name} final {digits}" if digits else name
    kind = (parent or {}).get("name")
    return f"{label} ({kind})" if parent and isinstance(kind, str) and kind and kind not in label else label


def parse_payment_options(data: dict[str, Any]) -> list[PaymentOption]:
    """Every payable option from listPaymentMethods: saved cards (with their token), PIX, on delivery..."""
    options: list[PaymentOption] = []
    seen: set[tuple[str, str | None]] = set()

    def add(method_id: Any, token: Any, label: str, offline: bool) -> None:
        key = (str(method_id), str(token) if token else None)
        if key not in seen:
            seen.add(key)
            options.append(PaymentOption(key[0], key[1], label, offline))

    for method in dicts_with(data, "paymentMethodId"):
        offline = any(w in _text(method) for w in OFFLINE_PAYMENT_WORDS)
        if method.get("cardTokenId"):
            add(method["paymentMethodId"], method["cardTokenId"], _payment_label(method, None), offline)
            continue
        cards = [c for c in dicts_with(method, "cardTokenId") if c is not method and not c.get("paymentMethodId")]
        for card in cards:
            if card.get("cardTokenId"):
                add(method["paymentMethodId"], card["cardTokenId"], _payment_label(card, method), offline)
        # A card method with no saved card can't be paid here (setCartPayment needs a cardTokenId).
        if not cards and not any(w in _text(method) for w in CARD_WORDS):
            add(method["paymentMethodId"], None, _payment_label(method, None), offline)
    # Saved cards first, then PIX and other online methods, pay-on-delivery last.
    options.sort(key=lambda o: (o.offline, o.card_token is None, "PIX" not in o.label.upper()))
    return options


def parse_vouchers(data: dict[str, Any]) -> list[Voucher]:
    vouchers = []
    for d in dicts_with(data, "campaignId"):
        code = d.get("code") or d.get("voucherCode")
        if not code or not d.get("campaignId"):
            continue
        description = next(
            (v for k in ("title", "description", "name", "discountDescription") if isinstance(v := d.get(k), str) and v),
            None,
        )
        label = description or "Cupom iFood"
        vouchers.append(Voucher(str(code), str(d["campaignId"]), label))
    return vouchers


async def _sync_items(ifood: Caller, cart_id: str, wanted: dict[str, int], cart: dict[str, Any]) -> None:
    """Brings an existing cart to the wanted {itemId: quantity} instead of creating a new one."""
    current = {str(line.get("itemId")): line for line in dicts_with(cart, "cartItemId")}
    for item_id, quantity in wanted.items():
        line = current.get(item_id)
        if line is None:
            await ifood.call(
                "addItemToCart", {"cartId": cart_id, "itemId": item_id, "quantity": quantity, "options": []}
            )
        elif int(as_number(line.get("quantity")) or 0) != quantity:  # counts may come as strings
            await ifood.call(
                "updateCartItem",
                {"cartId": cart_id, "cartItemId": str(line["cartItemId"]), "itemId": item_id, "quantity": quantity,
                 "options": []},
            )
    for item_id, line in current.items():
        if item_id not in wanted:
            await ifood.call("removeItemFromCart", {"cartId": cart_id, "cartItemId": str(line["cartItemId"])})


async def prepare_cart(ifood: Caller, store: StoreOption, address: Address, existing_cart_id: str | None) -> str:
    """Creates the cart (or resumes the one this store already has) and sets ASAP delivery."""
    wanted = {p.product.item_id: p.quantity for p in store.picks}
    if existing_cart_id:
        cart_id = existing_cart_id
        await _sync_items(ifood, cart_id, wanted, await ifood.call("getCart", {"cartId": cart_id}))
    else:
        # Grocery items have no options; the field is still required ([] = none).
        items = [{"itemId": item_id, "quantity": q, "options": []} for item_id, q in wanted.items()]
        cart = await ifood.call(
            "createCart", {"merchantId": store.merchant_id, "addressId": address.address_id, "items": items}
        )
        cart_id = cart_id_of(cart)
        if not cart_id:
            raise IFoodError("createCart", "a resposta não trouxe o cartId")

    methods = await ifood.call("getCartDeliveryMethods", {"cartId": cart_id})
    await ifood.call("setCartDelivery", {"cartId": cart_id, "deliveryMethodId": pick_delivery(methods)})
    return cart_id


async def list_payments(ifood: Caller, merchant_id: str) -> list[PaymentOption]:
    return parse_payment_options(await ifood.call("listPaymentMethods", {"merchantId": merchant_id}))


async def set_payment(ifood: Caller, cart_id: str, option: PaymentOption) -> None:
    args = {"cartId": cart_id, "paymentMethodId": option.method_id}
    if option.card_token:
        args["cardTokenId"] = option.card_token
    await ifood.call("setCartPayment", args)


async def list_vouchers(ifood: Caller, cart_id: str) -> list[Voucher]:
    """Optional step: a failure here just means no coupon is offered."""
    try:
        return parse_vouchers(await ifood.call("listVouchers", {"cartId": cart_id}))
    except IFoodError as e:
        log.warning("listVouchers failed: %s", e)
        return []


async def set_voucher(ifood: Caller, cart_id: str, voucher: Voucher) -> None:
    await ifood.call("setVoucher", {"cartId": cart_id, "code": voucher.code, "campaignId": voucher.campaign_id})


async def review(
    ifood: Caller, cart_id: str, store: StoreOption, address: Address, payment_label: str, voucher_label: str | None
) -> Checkout:
    cart = await ifood.call("getCart", {"cartId": cart_id})
    # The raw payload stays in the server log; the UI only gets the clean summary.
    log.info("getCart payload for %s: %s", cart_id, json.dumps(cart, ensure_ascii=False))
    return _summarize(cart_id, store, address, payment_label, voucher_label, cart)


def _summarize(
    cart_id: str,
    store: StoreOption,
    address: Address,
    payment_label: str,
    voucher_label: str | None,
    cart: dict[str, Any],
) -> Checkout:
    section = next((cart[k] for k in SUMMARY_SECTIONS if isinstance(cart.get(k), dict)), cart)

    def amount(*keys: str) -> float | None:
        value = as_number(first_value(section, *keys))
        if value is None and section is not cart:
            value = as_number(first_value(cart, *keys))
        return value

    picks = {p.product.item_id: p for p in store.picks}
    items = []
    for line in dicts_with(cart, "cartItemId"):
        pick = picks.get(str(line.get("itemId")))
        name = first_value(line, "name", "itemName", "description") or (pick.product.name if pick else "Item")
        price = as_number(first_value(line, "totalPrice", "total", "price", "unitPrice"))
        quantity = int(as_number(line.get("quantity")) or 1)
        items.append(
            {"quantity": quantity, "name": str(name), "price": brl(price), "allergen": pick.allergen if pick else None}
        )
    if not items:
        items = [
            {"quantity": p.quantity, "name": p.product.name, "price": brl(p.total), "allergen": p.allergen}
            for p in store.picks
        ]

    lines = []
    for label, keys in (
        ("Subtotal", ("subtotal", "subTotal", "itemsTotal", "itemsAmount")),
        ("Entrega", ("deliveryFee", "deliveryAmount", "deliveryPrice")),
        ("Descontos", ("discount", "discountAmount", "totalDiscount")),
        ("Total", ("totalAmount", "total", "totalPrice", "totalValue", "orderTotal")),
    ):
        value = amount(*keys)
        if value is not None:
            lines.append((label, brl(value)))
    if not any(label == "Total" for label, _ in lines):
        lines.append(("Total estimado", brl(store.total)))

    warnings = []
    raw_warnings = first_value(cart, "warnings", "validations", "alerts")
    for w in raw_warnings if isinstance(raw_warnings, list) else []:
        warnings.append(w if isinstance(w, str) else str(first_value(w, "message", "description") or json.dumps(w)))

    return Checkout(cart_id, store, address, payment_label, voucher_label, items, lines, warnings)
