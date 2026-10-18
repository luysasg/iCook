"""Grocery search, store comparison and cart building.

All iFood calls here are made by code, not by the LLM: one search per ingredient,
gap-filling searches inside the best stores, and a single LLM call to match
search results to ingredients and round quantities up.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from . import config, llm, prompts
from .ifood import IFoodError, IFoodMCP
from .jsonutil import as_number, brl, dicts_with, first_value, walk

log = logging.getLogger("shopping")

SEARCH_CONCURRENCY = 4
CANDIDATES_PER_INGREDIENT = 4
PICKUP_WORDS = ("PICKUP", "TAKEOUT", "RETIRADA")
OFFLINE_PAYMENT_WORDS = ("CASH", "DINHEIRO", "OFFLINE", "NA ENTREGA", "VALE")
SUMMARY_SECTIONS = ("summary", "totals", "totalizers", "prices", "values", "amounts")


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

    @property
    def total(self) -> float:
        return self.product.price * self.quantity

    def label(self) -> str:
        return f"{self.quantity}x {self.product.name} — {brl(self.total)}"


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


@dataclass
class Address:
    address_id: str
    label: str
    latitude: float
    longitude: float


@dataclass
class Checkout:
    cart_id: str
    store: StoreOption
    address: Address
    payment_label: str
    items: list[str]
    lines: list[tuple[str, str]]
    warnings: list[str]
    raw: dict[str, Any]


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
        addresses.append(Address(str(entry["addressId"]), label, latitude, longitude))
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
    ifood: IFoodMCP, ingredients: list[Ingredient], location: dict[str, float]
) -> tuple[list[StoreOption], list[str]]:
    """Returns (store options sorted best-first, ingredients no compared store has)."""
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def search(term: str, merchant_id: str | None = None, page_size: int = 25) -> list[dict[str, Any]]:
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
        stores.values(), key=lambda s: (-len(candidates.get(s.merchant_id, {})), s.distance_km or 99.0)
    )
    top = ranked[: config.MAX_STORES]
    gaps = [
        (s.merchant_id, i)
        for s in top
        for i in range(len(ingredients))
        if i not in candidates.get(s.merchant_id, {})
    ]
    gap_results = await asyncio.gather(
        *(search(ingredients[i].search_term, merchant_id, page_size=5) for merchant_id, i in gaps)
    )
    for (merchant_id, i), items in zip(gaps, gap_results, strict=True):
        collect(i, [r for r in items if (r.get("merchant") or {}).get("merchantId") == merchant_id])

    picks = await _match_products(ingredients, top, candidates)
    for store in top:
        store.picks = picks.get(store.merchant_id, [])
        found = {p.index for p in store.picks}
        store.missing = [ing.name for i, ing in enumerate(ingredients) if i not in found]

    top = [s for s in top if s.picks]
    not_found = [ing.name for ing in ingredients if all(ing.name in s.missing for s in top)]
    top.sort(key=lambda s: (len(s.missing), s.total))
    return top, not_found


async def _match_products(
    ingredients: list[Ingredient], stores: list[StoreOption], candidates: dict[str, dict[int, list[Product]]]
) -> dict[str, list[Pick]]:
    """One LLM call: pick the right product per ingredient per store and round quantities up."""
    refs: dict[str, tuple[str, int, Product]] = {}
    lines = ["Ingredientes:"]
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
        prompts.PICKER_SYSTEM, "\n".join(lines), prompts.PICKER_SCHEMA, temperature=0.1, timeout=45
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
            picks[merchant_id].append(Pick(index, ingredients[index].name, product, quantity))
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


def _payment_label(method: dict[str, Any], parent: dict[str, Any] | None) -> str:
    name = next(
        (v for v in (method.get("name"), method.get("brand"), (parent or {}).get("name"), method.get("type"))
         if isinstance(v, str) and v),
        "forma de pagamento padrão",
    )
    digits = method.get("lastDigits") or method.get("last4") or method.get("lastFourDigits")
    return f"{name} final {digits}" if digits else name


def pick_payment(data: dict[str, Any]) -> tuple[str, str | None, str]:
    """Returns (paymentMethodId, cardTokenId, label): saved card, then PIX, then any online method."""
    for d, parent in walk(data):
        if d.get("cardTokenId"):
            method_id = d.get("paymentMethodId") or (parent or {}).get("paymentMethodId")
            if method_id:
                return str(method_id), str(d["cardTokenId"]), _payment_label(d, parent)

    methods = dicts_with(data, "paymentMethodId")

    def text(m: dict[str, Any]) -> str:
        return " ".join(str(m.get(k, "")) for k in ("name", "type", "description", "method")).upper()

    online = [m for m in methods if not any(w in text(m) for w in OFFLINE_PAYMENT_WORDS)]
    pix = [m for m in online if "PIX" in text(m)]
    choices = pix or online or methods
    if not choices:
        raise IFoodError("listPaymentMethods", "nenhuma forma de pagamento disponível")
    return str(choices[0]["paymentMethodId"]), None, _payment_label(choices[0], None)


async def build_cart(ifood: IFoodMCP, store: StoreOption, address: Address) -> Checkout:
    items = [{"itemId": p.product.item_id, "quantity": p.quantity} for p in store.picks]
    cart = await ifood.call(
        "createCart", {"merchantId": store.merchant_id, "addressId": address.address_id, "items": items}
    )
    cart_id = first_value(cart, "cartId") or first_value(cart, "id")
    if not cart_id:
        raise IFoodError("createCart", "a resposta não trouxe o cartId")
    cart_id = str(cart_id)

    methods = await ifood.call("getCartDeliveryMethods", {"cartId": cart_id})
    await ifood.call("setCartDelivery", {"cartId": cart_id, "deliveryMethodId": pick_delivery(methods)})

    payments = await ifood.call("listPaymentMethods", {"merchantId": store.merchant_id})
    method_id, card_token, payment_label = pick_payment(payments)
    payment_args = {"cartId": cart_id, "paymentMethodId": method_id}
    if card_token:
        payment_args["cardTokenId"] = card_token
    await ifood.call("setCartPayment", payment_args)

    final = await ifood.call("getCart", {"cartId": cart_id})
    return _summarize(cart_id, store, address, payment_label, final)


def _summarize(
    cart_id: str, store: StoreOption, address: Address, payment_label: str, cart: dict[str, Any]
) -> Checkout:
    section = next((cart[k] for k in SUMMARY_SECTIONS if isinstance(cart.get(k), dict)), cart)

    def amount(*keys: str) -> float | None:
        value = as_number(first_value(section, *keys))
        if value is None and section is not cart:
            value = as_number(first_value(cart, *keys))
        return value

    names = {p.product.item_id: p.product.name for p in store.picks}
    items = []
    for line in dicts_with(cart, "cartItemId"):
        name = first_value(line, "name", "itemName", "description") or names.get(line.get("itemId"), "Item")
        price = as_number(first_value(line, "totalPrice", "total", "price", "unitPrice"))
        items.append(f"{line.get('quantity', 1)}x {name} — {brl(price)}")
    if not items:
        items = [p.label() for p in store.picks]

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

    return Checkout(cart_id, store, address, payment_label, items, lines, warnings, cart)
