"""Offline tests: a fake iFood MCP (shapes based on a real searchOffers response) and a scripted LLM."""

import json
import re
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from app import agent, auth, config, ifood, llm, prompts, shopping
from app.guard import Guard, GuardError
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
        self.cart_items = []

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
            self.cart_items = [
                {"cartItemId": f"ci-{n}", "itemId": i["itemId"], "quantity": str(i["quantity"]), "totalPrice": 9.72}
                for n, i in enumerate(args["items"], 1)
            ]
            return {"cartId": "cart-1", "items": self.cart_items}
        if tool == "addItemToCart":
            line = {"cartItemId": f"ci-new-{args['itemId']}", "itemId": args["itemId"], "quantity": args["quantity"]}
            self.cart_items.append(line)
            return {"cartId": "cart-1", "items": self.cart_items}
        if tool == "removeItemFromCart":
            self.cart_items = [i for i in self.cart_items if i["cartItemId"] != args["cartItemId"]]
            return {}
        if tool == "listVouchers":
            return {"vouchers": [{"code": "BEMVINDO", "campaignId": "camp-1", "title": "R$ 10 de desconto"}]}
        if tool == "setVoucher":
            return {}
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
                    {"paymentMethodId": "pm-debit", "name": "Débito"},  # no saved card: not payable here
                    {"paymentMethodId": "pm-pix", "name": "Pix"},
                ]
            }
        if tool in ("setCartDelivery", "setCartPayment"):
            return {}
        if tool == "getCart":
            return {
                "cartId": "cart-1",
                "items": self.cart_items,
                "summary": {"subtotal": 9.72, "deliveryFee": 12.49, "totalAmount": 22.21},
            }
        if tool == "submitOrder":
            if self.require_document and "document" not in args:
                raise IFoodError(tool, "Missing document: CPF is required to checkout")
            return {"orderId": "order-1", "shortCode": "ABCD"}
        if tool == "trackOrder":
            return {
                "status": {"code": "DISPATCHED", "description": "Merchant released order for delivery."},
                "agentHints": {"retryMessage": "Poll again in 45 seconds.", "pollingStop": False},
            }
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

    async def generate_json(self, system, contents, schema, temperature=0.5, timeout=None):
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


HOME = shopping.Address("addr-home", "Casa (Av. Paulista, 1000)", -23.56, -46.65)


def connected_session(address=HOME):
    """A logged-in session that already picked its delivery address."""
    s = agent.Session("sid")
    s.tokens = auth.Tokens("access", "refresh", time.time() + 3600)
    s.address = address
    return s


SHOPPING_LIST = [
    {"name": "Tomate", "search_term": "tomate", "quantity": 500, "unit": "g"},
    {"name": "Cebola", "search_term": "cebola", "quantity": 1, "unit": "unidade"},
    {"name": "Creme de leite", "search_term": "creme de leite", "quantity": 200, "unit": "g"},
]


RECIPE = {
    "title": "Strogonoff de frango",
    "description": "Clássico cremoso.",
    "servings": 2,
    "ingredients": SHOPPING_LIST + [{"name": "Sal", "search_term": "sal", "quantity": 1, "unit": "pitada"}],
    "steps": ["Refogue a cebola.", "Junte o tomate e o creme de leite."],
}


async def test_full_flow_orders_only_after_button(fake):
    ifood_fake, scripted = fake
    s = connected_session()
    scripted.turns = [
        {"reply": "Boa! Pra quantas pessoas?", "action": "none", "quick_replies": ["2", "4"]},
        {"reply": "Vou te mostrar a receita, só um instante!", "action": "show_recipe", "servings": 2,
         "restrictions": "nenhuma", "recipe": RECIPE},
        {"reply": "É só clicar em Confirmar pedido.", "action": "none"},
        {"reply": "Vou ver!", "action": "track_order"},
        {"reply": "Vou ver os detalhes.", "action": "order_details"},
    ]

    out = await agent.handle_message(s, "quero um strogonoff")
    assert out[-1]["data"] == {"chips": ["2", "4"]}

    # Recipe first, then the pantry question; the filler reply is dropped.
    out = await agent.handle_message(s, "2 pessoas, sem restrições")
    assert [m["kind"] for m in out] == ["recipe"]
    assert s.phase == "pantry" and s.servings == 2
    assert [i["number"] for i in out[0]["data"]["ingredients"]] == [1, 2, 3, 4]
    assert "Não tenho nenhum" in out[0]["data"]["chips"]

    # "4" (sal) is answered by number, without the LLM; only the rest is searched.
    out = await agent.handle_message(s, "4")
    assert [i.name for i in s.shopping_list] == ["Tomate", "Cebola", "Creme de leite"]
    assert next(m for m in s.messages if m["kind"] == "recipe")["data"]["have"] == [4]
    assert "sal" not in [a["query"] for t, a in ifood_fake.calls if t == "searchOffers"]

    assert s.phase == "compare"
    assert "listAddresses" not in ifood_fake.tools()  # the address was chosen before the chat
    stores_msg = out[-1]
    assert stores_msg["kind"] == "stores"
    options = stores_msg["data"]["options"]
    assert [o["name"] for o in options] == ["Oba Hortifruti - Pamplona", "Daki Mercado Express | Paulista"]
    assert options[0]["missing"] == ["Creme de leite"] and options[1]["missing"] == ["Cebola"]
    assert stores_msg["data"]["not_found"] == []
    # Gap-filling: each store was searched for what it was missing.
    assert ("searchOffers", "m-oba") in [(t, a["refinements"].get("merchantId")) for t, a in ifood_fake.calls if t == "searchOffers"]

    # Store → the user picks the payment method (nothing is chosen for them).
    out = await agent.choose_store(s, 0, from_button=True)
    assert s.phase == "payment" and out[-1]["kind"] == "payment"
    labels = [o["label"] for o in out[-1]["data"]["options"]]
    assert labels == ["Visa final 4242 (Crédito)", "Pix", "Dinheiro"]  # debit has no saved card
    assert ("setCartDelivery", {"cartId": "cart-1", "deliveryMethodId": "DEFAULT"}) in ifood_fake.calls
    assert "setCartPayment" not in ifood_fake.tools()
    create = next(a for t, a in ifood_fake.calls if t == "createCart")
    assert all(i["options"] == [] for i in create["items"])

    out = await agent.choose_payment(s, 0, from_button=True)
    assert ("setCartPayment", {"cartId": "cart-1", "paymentMethodId": "pm-credit", "cardTokenId": "tok-1"}) in ifood_fake.calls
    assert s.phase == "voucher" and out[-1]["kind"] == "vouchers"

    out = await agent.choose_voucher(s, 0, from_button=True)
    assert ("setVoucher", {"cartId": "cart-1", "code": "BEMVINDO", "campaignId": "camp-1"}) in ifood_fake.calls
    assert ifood_fake.tools()[-1] == "getCart"  # the summary always comes from a fresh getCart
    assert s.phase == "checkout"
    summary = out[-1]["data"]
    assert summary["payment"] == "Visa final 4242 (Crédito)" and summary["voucher"].startswith("BEMVINDO")
    assert {"label": "Total", "value": "R$ 22,21"} in summary["lines"]
    assert "raw" not in summary  # raw MCP payloads stay in the server log

    # Saying "confirmo" in the chat must not place the order.
    await agent.handle_message(s, "confirmo")
    assert "submitOrder" not in ifood_fake.tools()

    out = await agent.confirm_order(s)
    assert s.phase == "ordered" and s.order_id == "order-1"
    assert ("submitOrder", {"cartId": "cart-1", "userConsent": True}) in ifood_fake.calls
    assert "ABCD" in out[0]["text"]
    # Tracking starts right away; status text verbatim, cadence from agentHints.
    tracker = out[-1]
    assert tracker["kind"] == "tracker"
    assert tracker["data"]["description"] == "Merchant released order for delivery."
    assert tracker["data"]["poll_after"] == 45 and tracker["data"]["step"] == 3

    out = await agent.track_order(s, refresh=True)
    assert out[0]["id"] == tracker["id"] and tracker["rev"] == 1  # polling updates the same card

    out = await agent.handle_message(s, "cadê meu pedido?")
    assert out[-1]["data"]["description"] == "Merchant released order for delivery."
    out = await agent.handle_message(s, "o que eu pedi?")
    assert "1234" in out[-1]["text"]


async def to_summary(s, scripted):
    scripted.turns.append({"reply": "", "action": "search_stores", "shopping_list": SHOPPING_LIST[:1]})
    await agent.handle_message(s, "pode buscar")
    await agent.choose_store(s, 0)
    await agent.choose_payment(s, 1)  # Pix
    return await agent.choose_voucher(s, None)


async def test_document_is_asked_and_kept_out_of_llm_history(fake):
    ifood_fake, scripted = fake
    ifood_fake.require_document = True
    s = connected_session()
    await to_summary(s, scripted)
    assert ("setCartPayment", {"cartId": "cart-1", "paymentMethodId": "pm-pix"}) in ifood_fake.calls  # no token for Pix
    assert "setVoucher" not in ifood_fake.tools()

    await agent.confirm_order(s)
    assert s.phase == "need_document"

    await agent.handle_message(s, "123.456.789-09")
    assert s.phase == "ordered"
    submit = [a for t, a in ifood_fake.calls if t == "submitOrder"][-1]
    assert submit == {"cartId": "cart-1", "userConsent": True, "document": "12345678909"}
    assert all("123" not in m["text"] for m in s.history)


async def test_optional_document_on_the_summary_card(fake):
    ifood_fake, scripted = fake
    s = connected_session()
    await to_summary(s, scripted)
    out = await agent.confirm_order(s, document="12")
    assert s.phase == "checkout" and "submitOrder" not in ifood_fake.tools() and "válido" in out[-1]["text"]
    await agent.confirm_order(s, document="AB123456")  # passport
    assert ifood_fake.calls[-2] == ("submitOrder", {"cartId": "cart-1", "userConsent": True, "document": "AB123456"})


async def test_dry_run_never_submits(fake, monkeypatch):
    ifood_fake, scripted = fake
    monkeypatch.setattr(config, "DRY_RUN", True)
    s = connected_session()
    out = await to_summary(s, scripted)
    assert out[-1]["data"]["dry_run"] is True

    out = await agent.confirm_order(s)
    assert s.phase == "ordered" and "submitOrder" not in ifood_fake.tools()
    assert out[-1]["kind"] == "tracker" and out[-1]["data"]["dry_run"] is True
    first = out[-1]["data"]["code"]
    out = await agent.track_order(s, refresh=True)
    assert out[0]["data"]["code"] != first and "trackOrder" not in ifood_fake.tools()
    # Even a direct call is blocked by the guard.
    with pytest.raises(GuardError):
        s.guard.check("submitOrder", {"cartId": "cart-1", "userConsent": True})


async def test_same_store_resumes_the_cart(fake):
    ifood_fake, scripted = fake
    s = connected_session()
    scripted.turns = [{"reply": "", "action": "search_stores", "shopping_list": SHOPPING_LIST}]
    await agent.handle_message(s, "pode buscar")
    await agent.choose_store(s, 0)
    s.options[0].picks = s.options[0].picks[:1]  # e.g. the list changed

    await agent.choose_store(s, 0)
    assert ifood_fake.tools().count("createCart") == 1
    removed = [a for t, a in ifood_fake.calls if t == "removeItemFromCart"]
    assert removed == [{"cartId": "cart-1", "cartItemId": "ci-2"}]  # cartItemId, not itemId


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


async def test_messages_before_login_only_ask_to_connect(fake):
    ifood_fake, scripted = fake
    s = agent.Session("sid")
    assert s.messages[0]["kind"] == "login"

    out = await agent.handle_message(s, "quero fazer lasanha")
    assert out[-1]["kind"] == "login"
    assert scripted.histories == [] and ifood_fake.calls == []
    assert s.history == []


async def test_address_is_chosen_first_by_button(fake):
    s = connected_session(address=None)

    out = await agent.ask_address(s, intro="Conta iFood conectada! ✅ ")
    assert s.phase == "address"
    assert out[-1]["kind"] == "addresses"
    # Last used address comes first.
    assert [a["label"] for a in out[-1]["data"]["options"]] == ["Casa (Av. Paulista, 1000)", "Trabalho (Rua B, 2)"]

    out = await agent.choose_address(s, 1, from_button=True)
    assert s.phase == "chat" and s.address.address_id == "addr-work"
    assert prompts.ASK_CRAVING in out[-1]["text"]
    assert s.history[0] == {"role": "user", "text": "Estou em Trabalho (Rua B, 2)"}


async def test_address_by_text_and_nothing_else_until_chosen(fake):
    ifood_fake, scripted = fake
    s = connected_session(address=None)
    await agent.ask_address(s)
    scripted.turns = [
        {"reply": "Busco já!", "action": "search_stores", "shopping_list": SHOPPING_LIST},
        {"reply": "Fechado!", "action": "choose_address", "address_number": 1},
    ]

    await agent.handle_message(s, "quero strogonoff, pode buscar")
    assert s.phase == "address" and "searchOffers" not in ifood_fake.tools()
    assert "1. Casa (Av. Paulista, 1000)" in agent._state_block(s)  # the LLM sees the numbered list

    await agent.handle_message(s, "tô em casa")
    assert s.phase == "chat" and s.address.address_id == "addr-home"


async def test_changing_address_redoes_the_comparison(fake):
    ifood_fake, scripted = fake
    s = connected_session()
    scripted.turns = [
        {"reply": "Buscando!", "action": "search_stores", "shopping_list": SHOPPING_LIST[:1]},
        {"reply": "Claro!", "action": "change_address"},
    ]
    await agent.handle_message(s, "pode buscar")
    assert s.phase == "compare"

    out = await agent.handle_message(s, "na verdade estou no trabalho")
    assert s.phase == "address" and s.options == [] and out[-1]["kind"] == "addresses"

    searches = ifood_fake.tools().count("searchOffers")
    out = await agent.choose_address(s, 1, from_button=True)
    assert s.phase == "compare" and s.address.address_id == "addr-work"
    assert out[-1]["kind"] == "stores"
    assert ifood_fake.tools().count("searchOffers") > searches


async def test_no_saved_address(fake, monkeypatch):
    ifood_fake, scripted = fake
    original = ifood_fake.call

    async def call(tool, args):
        if tool == "listAddresses":
            return {"addresses": []}
        return await original(tool, args)

    monkeypatch.setattr(ifood_fake, "call", call)
    s = connected_session(address=None)
    out = await agent.ask_address(s)
    assert s.phase == "address" and "app do iFood" in out[-1]["text"]
    # Any message looks the addresses up again, without calling the LLM.
    out = await agent.handle_message(s, "pronto, cadastrei")
    assert scripted.histories == [] and "app do iFood" in out[-1]["text"]


def openrouter_with(monkeypatch, behaviour):
    """Fake OpenRouter: each model name maps to an HTTP status, "slow", or the message content."""
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        result = behaviour[body["model"]]
        if result == "slow":
            raise httpx.ReadTimeout("no answer", request=request)
        if isinstance(result, int):
            return httpx.Response(result, json={"error": {"code": result, "message": "nope"}})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": result}}]})

    monkeypatch.setattr(llm, "_client", httpx.AsyncClient(base_url="http://fake", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(config, "LLM_MODELS", list(behaviour))
    return requests


async def test_llm_falls_back_to_the_next_model(monkeypatch):
    requests = openrouter_with(
        monkeypatch,
        {"overloaded": 503, "slow": "slow", "chatty": "not json", "ok": '```json\n{"reply": "oi", "action": "none"}\n```'},
    )
    assert await llm.generate_json("sys", "oi", {"type": "object"}, timeout=0.05) == {"reply": "oi", "action": "none"}
    assert [r["model"] for r in requests] == ["overloaded", "slow", "chatty", "ok"]
    # JSON-schema output, only from providers that support it.
    assert requests[-1]["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert requests[-1]["provider"] == {"require_parameters": True}
    assert requests[-1]["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "oi"}]


async def test_llm_rate_limited_everywhere(monkeypatch):
    openrouter_with(monkeypatch, {"a": 429, "b": 429})
    with pytest.raises(llm.RateLimited):
        await llm.generate_json("sys", "oi", {})


def test_history_roles_map_to_chat_roles():
    history = [{"role": "user", "text": "oi"}, {"role": "model", "text": "olá"}]
    assert llm.to_contents(history) == [{"role": "user", "content": "oi"}, {"role": "assistant", "content": "olá"}]


def test_payment_options_come_from_the_list():
    options = shopping.parse_payment_options(
        {"methods": [{"paymentMethodId": "cash", "name": "Dinheiro"}, {"paymentMethodId": "pix", "name": "Pix"}]}
    )
    assert [(o.method_id, o.card_token, o.offline) for o in options] == [("pix", None, False), ("cash", None, True)]
    assert shopping.parse_payment_options({"methods": []}) == []


def test_guard_enforces_the_documented_rules():
    g = Guard()
    g.record("createCart", {"merchantId": "m1"}, {"cartId": "c1", "items": [{"cartItemId": "ci-1", "itemId": "i-1"}]})

    def blocked(tool, args, why):
        with pytest.raises(GuardError, match=why):
            g.check(tool, args)

    blocked("createCart", {"merchantId": "m1", "items": []}, "getCart")  # resume, don't recreate
    blocked("addItemToCart", {"cartId": "c1", "itemId": "i-2", "quantity": 1}, "options")
    blocked("addItemToCart", {"cartId": "c1", "itemId": "i-2", "quantity": 1, "options": [{"optionId": "o"}]}, "getItem")
    blocked("updateCartItem", {"cartId": "c1", "cartItemId": "i-1", "itemId": "i-1", "quantity": 2, "options": []},
            "cartItemId")  # an itemId where a cartItemId is expected
    blocked("setCartDelivery", {"cartId": "c1", "deliveryMethodId": "DEFAULT"}, "getCartDeliveryMethods")
    blocked("setCartPayment", {"cartId": "c1", "paymentMethodId": "PIX"}, "listPaymentMethods")
    blocked("setVoucher", {"cartId": "c1", "code": "X", "campaignId": "y"}, "listVouchers")
    blocked("getCart", {"cartId": "other"}, "cartId")

    g.record("getItem", {"itemId": "i-2"}, {"options": [{"optionId": "o"}]})
    g.check("addItemToCart", {"cartId": "c1", "itemId": "i-2", "quantity": 1, "options": [{"optionId": "o"}]})
    g.record("getCartDeliveryMethods", {"cartId": "c1"}, {"methods": [{"deliveryMethodId": "DEFAULT"}]})
    g.record("setCartDelivery", {"cartId": "c1", "deliveryMethodId": "DEFAULT"}, {})
    g.record("listPaymentMethods", {"merchantId": "m1"},
             {"methods": [{"paymentMethodId": "CC", "name": "Crédito", "cards": [{"cardTokenId": "t1"}]}]})
    blocked("setCartPayment", {"cartId": "c1", "paymentMethodId": "CC"}, "listPaymentMethods")  # token required
    g.check("setCartPayment", {"cartId": "c1", "paymentMethodId": "CC", "cardTokenId": "t1"})
    g.record("setCartPayment", {"cartId": "c1", "paymentMethodId": "CC", "cardTokenId": "t1"}, {})

    blocked("submitOrder", {"cartId": "c1", "userConsent": True}, "getCart")
    g.record("getCart", {"cartId": "c1"}, {"items": []})
    blocked("submitOrder", {"cartId": "c1", "userConsent": True}, "confirmação")
    g.grant_consent("c1")
    blocked("submitOrder", {"cartId": "c1"}, "userConsent")
    g.check("submitOrder", {"cartId": "c1", "userConsent": True})
    # Any change after the review needs a new getCart and a new confirmation.
    g.record("setCartPayment", {"cartId": "c1", "paymentMethodId": "CC", "cardTokenId": "t1"}, {})
    blocked("submitOrder", {"cartId": "c1", "userConsent": True}, "getCart")


async def test_pantry_by_names_goes_through_the_llm(fake):
    _, scripted = fake
    s = connected_session()
    scripted.turns = [
        {"reply": "", "action": "show_recipe", "recipe": RECIPE},
        {"reply": "", "action": "set_pantry", "have_ingredients": [2, 4]},
    ]
    await agent.handle_message(s, "strogonoff pra 2, sem restrição")
    await agent.handle_message(s, "tenho cebola e sal")
    assert [i.name for i in s.shopping_list] == ["Tomate", "Creme de leite"]
    assert s.phase == "compare"


def test_pantry_answers_parsed_in_code():
    assert agent.parse_pantry_answer("1, 3 e 5", 5) == [1, 3, 5]
    assert agent.parse_pantry_answer("Já tenho: 2;9", 5) == [2]
    assert agent.parse_pantry_answer("Não tenho nenhum", 5) == []
    assert agent.parse_pantry_answer("tenho todos", 3) == [1, 2, 3]
    assert agent.parse_pantry_answer("tenho arroz", 5) is None


async def test_login_resumes_the_interrupted_task(fake):
    ifood_fake, _ = fake
    s = connected_session()
    s.tokens = None  # disconnected mid-conversation
    s.shopping_list = shopping_list_items = agent._parse_ingredients(SHOPPING_LIST[:1])
    out = await agent.search_stores(s)
    assert out[-1]["kind"] == "login" and s.pending is not None
    assert "searchOffers" not in ifood_fake.tools()

    s.tokens = auth.Tokens("access", "refresh", time.time() + 3600)
    out = await agent.resume(s)
    assert out[-1]["kind"] == "stores" and s.shopping_list == shopping_list_items and s.pending is None


async def test_message_before_login_is_replayed_after_address(fake):
    _, scripted = fake
    s = agent.Session("sid")
    await agent.handle_message(s, "quero lasanha")
    s.tokens = auth.Tokens("access", "refresh", time.time() + 3600)
    await agent.resume(s)
    assert s.phase == "address"
    scripted.turns = [{"reply": "Lasanha! Pra quantas pessoas?", "action": "none"}]
    out = await agent.choose_address(s, 0, from_button=True)
    assert out[-1]["text"] == "Lasanha! Pra quantas pessoas?"
    assert [m["text"] for m in s.messages if m["role"] == "user"].count("quero lasanha") == 1


def test_as_number():
    assert as_number("R$ 1.234,56") == 1234.56
    assert as_number("12.5") == 12.5
    assert as_number({"value": 3}) == 3.0
    assert as_number(True) is None
