"""Offline tests: a fake iFood MCP (shapes based on a real searchOffers response) and a scripted LLM."""

import re
import time
from contextlib import asynccontextmanager

import pytest

from app import agent, auth, ifood, llm, prompts, shopping
from app.ifood import IFoodError
from app.jsonutil import as_number

OBA = {
    "merchantId": "m-oba",
    "merchantName": "Oba Hortifruti - Pamplona",
    "isOpen": True,
    "minimumOrderAmount": 60,
    "reviewRating": 4.9,
    "straightLineDistanceKm": 0.41,
    "deliveryMethod": {
        "deliveryMethodId": "FAST_DELIVERY",
        "deliveryMethodMode": "DELIVERY",
        "estimatedMinutes": {"min": 42, "max": 52},
        "fee": 12.49,
        "promotionalFee": 12.49,
    },
}
DAKI = {
    **OBA,
    "merchantId": "m-daki",
    "merchantName": "Daki Mercado Express | Paulista",
    "minimumOrderAmount": 50,
    "straightLineDistanceKm": 0.86,
    "deliveryMethod": {**OBA["deliveryMethod"], "deliveryMethodId": "DEFAULT", "fee": 16.99, "promotionalFee": 16.99},
}


def item(item_id, name, price, merchant, promo=None):
    return {
        "itemId": item_id,
        "itemName": name,
        "unitPrice": price,
        "promotionalUnitPrice": promo if promo is not None else price,
        "merchant": merchant,
    }


CATALOG = {
    "tomate": [
        item("i-tom-oba", "Tomate Italiano Kg.", 9.72, OBA),
        item("i-tom-daki", "Tomate Sweet Grape Ifrutus 250g", 10.19, DAKI),
    ],
    "cebola": [item("i-ceb-oba", "Cebola Kg.", 6.50, OBA)],
    "creme de leite": [item("i-cre-daki", "Creme de Leite Nestlé 200g", 4.99, DAKI)],
}


class FakeIFood:
    def __init__(self, require_document=False):
        self.calls = []
        self.require_document = require_document

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "listAddresses":
            return {
                "addresses": [
                    {
                        "addressId": "addr-work",
                        "alias": "TRABALHO",
                        "formattedAddress": "Rua B, 2",
                        "coordinates": {"latitude": -23.5, "longitude": -46.6},
                    },
                    {
                        "addressId": "addr-home",
                        "alias": "CASA",
                        "formattedAddress": "Av. Paulista, 1000",
                        "coordinates": {"latitude": -23.56, "longitude": -46.65},
                        "isLastUsedAddress": True,
                    },
                ]
            }
        if tool == "searchOffers":
            results = CATALOG.get(args["query"], [])
            merchant_id = args["refinements"].get("merchantId")
            if merchant_id:
                results = [r for r in results if r["merchant"]["merchantId"] == merchant_id]
            return {"results": results, "hasMore": False}
        if tool == "createCart":
            return {"cartId": "cart-1"}
        if tool == "getCartDeliveryMethods":
            return {
                "deliveryMethods": [
                    {"deliveryMethodId": "TAKEOUT", "deliveryMethodMode": "TAKEOUT"},
                    {"deliveryMethodId": "DEFAULT", "deliveryMethodMode": "DELIVERY"},
                ]
            }
        if tool == "listPaymentMethods":
            return {
                "paymentMethods": [
                    {"paymentMethodId": "pm-cash", "name": "Dinheiro"},
                    {
                        "paymentMethodId": "pm-credit",
                        "name": "Crédito",
                        "cards": [{"cardTokenId": "tok-1", "brand": "Visa", "lastDigits": "4242"}],
                    },
                ]
            }
        if tool in ("setCartDelivery", "setCartPayment"):
            return {}
        if tool == "getCart":
            return {
                "cartId": "cart-1",
                "items": [{"cartItemId": "ci-1", "itemId": "i-tom-oba", "quantity": 1, "totalPrice": 9.72}],
                "summary": {"subtotal": 9.72, "deliveryFee": 12.49, "totalAmount": 22.21},
            }
        if tool == "submitOrder":
            if self.require_document and "document" not in args:
                raise IFoodError(tool, "Missing document: CPF is required to checkout")
            return {"orderId": "order-1", "shortCode": "ABCD"}
        if tool == "trackOrder":
            return {"status": {"code": "DISPATCHED", "description": "Merchant released order for delivery."}}
        if tool == "getOrder":
            return {
                "shortCode": "ABCD",
                "merchant": "Oba Hortifruti",
                "items": ["Tomate Italiano Kg. (x1, R$ 9.72)"],
                "payment": "Credit Card - Total R$ 22.21",
                "delivery": {"address": "Av. Paulista, 1000", "verificationCode": "1234"},
            }
        raise AssertionError(f"unexpected tool {tool}")

    def tools(self):
        return [name for name, _ in self.calls]


class ScriptedLLM:
    """Agent turns come from a queue; the product picker always takes the first candidate."""

    def __init__(self):
        self.turns = []
        self.histories = []

    async def generate_json(self, system, contents, schema, temperature=0.5):
        if system == prompts.PICKER_SYSTEM:
            picks = {}
            for ref in re.findall(r"\[(M\d+-\d+-\d+)\]", contents):
                store, ingredient, _ = ref.split("-")
                picks.setdefault(store, {}).setdefault(int(ingredient), ref)
            return {
                "stores": [
                    {"store": s, "picks": [{"ingredient": i, "item_ref": r, "quantity": 1} for i, r in p.items()]}
                    for s, p in picks.items()
                ]
            }
        self.histories.append(contents)
        return self.turns.pop(0)


@pytest.fixture
def fake(monkeypatch):
    fake_ifood = FakeIFood()
    scripted = ScriptedLLM()

    @asynccontextmanager
    async def connect(token):
        yield fake_ifood

    monkeypatch.setattr(ifood, "connect", connect)
    monkeypatch.setattr(llm, "generate_json", scripted.generate_json)
    return fake_ifood, scripted


def connected_session():
    s = agent.Session("sid")
    s.tokens = auth.Tokens("access", "refresh", time.time() + 3600)
    return s


SHOPPING_LIST = [
    {"name": "Tomate", "search_term": "tomate", "quantity": 500, "unit": "g"},
    {"name": "Cebola", "search_term": "cebola", "quantity": 1, "unit": "unidade"},
    {"name": "Creme de leite", "search_term": "creme de leite", "quantity": 200, "unit": "g"},
]


async def test_full_flow_orders_only_after_button(fake):
    ifood_fake, scripted = fake
    s = connected_session()
    scripted.turns = [
        {"reply": "Boa! Pra quantas pessoas?", "action": "none"},
        {"reply": "Vou buscar nos mercados!", "action": "search_stores", "servings": 2, "shopping_list": SHOPPING_LIST},
        {"reply": "É só clicar em Confirmar pedido.", "action": "none"},
        {"reply": "Vou ver!", "action": "track_order"},
        {"reply": "Vou ver os detalhes.", "action": "order_details"},
    ]

    await agent.handle_message(s, "quero um strogonoff")
    out = await agent.handle_message(s, "2 pessoas, pode buscar")

    assert s.phase == "compare" and s.servings == 2
    assert s.address.address_id == "addr-home"  # last used address wins
    stores_msg = out[-1]
    assert stores_msg["kind"] == "stores"
    options = stores_msg["data"]["options"]
    assert [o["name"] for o in options] == ["Oba Hortifruti - Pamplona", "Daki Mercado Express | Paulista"]
    assert options[0]["missing"] == ["Creme de leite"] and options[1]["missing"] == ["Cebola"]
    assert stores_msg["data"]["not_found"] == []
    # Gap-filling: each store was searched for what it was missing.
    assert ("searchOffers", "m-oba") in [(t, a["refinements"].get("merchantId")) for t, a in ifood_fake.calls if t == "searchOffers"]

    out = await agent.choose_store(s, 0, from_button=True)
    assert s.phase == "checkout"
    summary = out[-1]["data"]
    assert summary["payment"] == "Visa final 4242"
    assert {"label": "Total", "value": "R$ 22,21"} in summary["lines"]
    assert ("setCartDelivery", {"cartId": "cart-1", "deliveryMethodId": "DEFAULT"}) in ifood_fake.calls
    assert ("setCartPayment", {"cartId": "cart-1", "paymentMethodId": "pm-credit", "cardTokenId": "tok-1"}) in ifood_fake.calls

    # Saying "confirmo" in the chat must not place the order.
    await agent.handle_message(s, "confirmo")
    assert "submitOrder" not in ifood_fake.tools()

    out = await agent.confirm_order(s)
    assert s.phase == "ordered" and s.order_id == "order-1"
    assert ("submitOrder", {"cartId": "cart-1", "userConsent": True}) in ifood_fake.calls
    assert "ABCD" in out[-1]["text"]

    out = await agent.handle_message(s, "cadê meu pedido?")
    assert "saiu para entrega" in out[-1]["text"]
    out = await agent.handle_message(s, "o que eu pedi?")
    assert "1234" in out[-1]["text"]


async def test_document_is_asked_and_kept_out_of_llm_history(fake):
    ifood_fake, scripted = fake
    ifood_fake.require_document = True
    s = connected_session()
    scripted.turns = [{"reply": "Buscando!", "action": "search_stores", "shopping_list": SHOPPING_LIST[:1]}]
    await agent.handle_message(s, "pode buscar")
    await agent.choose_store(s, 0)

    await agent.confirm_order(s)
    assert s.phase == "need_document"

    await agent.handle_message(s, "123.456.789-09")
    assert s.phase == "ordered"
    assert ifood_fake.calls[-1] == ("submitOrder", {"cartId": "cart-1", "userConsent": True, "document": "12345678909"})
    assert all("123" not in m["text"] for m in s.history)


async def test_ingredient_missing_everywhere_is_reported(fake):
    _, scripted = fake
    s = connected_session()
    shopping_list = SHOPPING_LIST[:1] + [{"name": "Açafrão", "search_term": "açafrão", "quantity": 1, "unit": "pacote"}]
    scripted.turns = [{"reply": "Buscando!", "action": "search_stores", "shopping_list": shopping_list}]
    out = await agent.handle_message(s, "pode buscar")
    assert out[-1]["data"]["not_found"] == ["Açafrão"]
    assert "Açafrão" in out[-1]["text"]


async def test_no_open_store(fake):
    _, scripted = fake
    s = connected_session()
    scripted.turns = [
        {"reply": "Buscando!", "action": "search_stores", "shopping_list": [{"name": "X", "search_term": "nada", "quantity": 1, "unit": "un"}]}
    ]
    out = await agent.handle_message(s, "pode buscar")
    assert s.phase == "chat"
    assert "nenhum mercado aberto" in out[-1]["text"]


async def test_unauthorized_asks_to_reconnect(monkeypatch, fake):
    _, scripted = fake

    @asynccontextmanager
    async def broken(token):
        raise ifood.Unauthorized
        yield

    monkeypatch.setattr(ifood, "connect", broken)
    s = connected_session()
    scripted.turns = [{"reply": "Buscando!", "action": "search_stores", "shopping_list": SHOPPING_LIST}]
    out = await agent.handle_message(s, "pode buscar")
    assert out[-1]["kind"] == "login"
    assert s.tokens is None


def test_payment_fallbacks():
    assert shopping.pick_payment(
        {"methods": [{"paymentMethodId": "cash", "name": "Dinheiro"}, {"paymentMethodId": "pix", "name": "Pix"}]}
    ) == ("pix", None, "Pix")
    with pytest.raises(IFoodError):
        shopping.pick_payment({"methods": []})


def test_as_number():
    assert as_number("R$ 1.234,56") == 1234.56
    assert as_number("12.5") == 12.5
    assert as_number({"value": 3}) == 3.0
    assert as_number(True) is None
