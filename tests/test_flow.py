"""Offline tests: a fake iFood MCP (shapes based on a real searchOffers response) and a scripted LLM."""

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app import agent, auth, config, ifood, llm, prompts, recipe, shopping, user
from app.guard import Guard, GuardError
from app.ifood import IFoodError
from app.jsonutil import as_number

OBA = {
    "merchantId": "m-oba",
    "merchantName": "Oba Hortifruti - Pamplona",
    "isOpen": True,
    "minimumOrderAmount": 5,
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


# Shape of the real getUser answer for the test account (BASIC tier only).
BASIC_USER = {
    "success": True,
    "data": {"fullName": "MARIA SOUZA LIMA", "email": "maria@test.ifood.com.br", "phone": "5511999998888"},
    "agentHints": {"suggestedNextAction": "Use the user's name for personalization."},
}


class FakeIFood:
    def __init__(self, require_document=False):
        self.calls = []
        self.require_document = require_document
        self.cart_items = []
        self.user = BASIC_USER
        self.submit_result = {"orderId": "order-1", "shortCode": "ABCD"}

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "getUser":
            if isinstance(self.user, Exception):
                raise self.user
            return self.user
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
            return self.submit_result
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


TIME_CHIPS = ["Almoço rápido", "Salada completa", "Me dá ideias"]


class ScriptedLLM:
    """Agent turns come from a queue; the product picker always takes the first candidate."""

    def __init__(self):
        self.turns = []
        self.histories = []
        # Answers to the chips prompts: one answer, or a list played in order (the last one repeats);
        # an exception in it is raised.
        self.chips = {"chips": []}  # profile-based prompt
        self.chip_requests = []
        self.time_chips = {"chips": TIME_CHIPS}  # time-only prompt (no user data)
        self.time_chip_requests = []

    @staticmethod
    def _play(answers):
        answer = (answers.pop(0) if len(answers) > 1 else answers[0]) if isinstance(answers, list) else answers
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def generate_json(self, system, contents, schema, temperature=0.5, timeout=None):
        if system == prompts.CHIPS_TIME_SYSTEM:
            self.time_chip_requests.append(contents)
            return self._play(self.time_chips)
        if system == prompts.CHIPS_SYSTEM:
            self.chip_requests.append(contents)
            return self._play(self.chips)
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
    monkeypatch.setattr(agent, "CHIPS_RETRY_SECONDS", 0)  # chip retries don't wait in tests
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
        {
            "reply": "Vou te mostrar a receita, só um instante!",
            "action": "show_recipe",
            "servings": 2,
            "restrictions": "nenhuma",
            "recipe": RECIPE,
        },
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
    assert ("searchOffers", "m-oba") in [
        (t, a["refinements"].get("merchantId")) for t, a in ifood_fake.calls if t == "searchOffers"
    ]

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
    assert (
        "setCartPayment",
        {"cartId": "cart-1", "paymentMethodId": "pm-credit", "cardTokenId": "tok-1"},
    ) in ifood_fake.calls
    assert s.phase == "voucher" and out[-1]["kind"] == "vouchers"

    out = await agent.choose_voucher(s, 0, from_button=True)
    assert ("setVoucher", {"cartId": "cart-1", "code": "BEMVINDO", "campaignId": "camp-1"}) in ifood_fake.calls
    assert ifood_fake.tools()[-1] == "getCart"  # the summary always comes from a fresh getCart
    assert s.phase == "checkout"
    summary = out[-1]["data"]
    assert summary["payment"] == "Visa final 4242 (Crédito)" and summary["voucher"] == "R$ 10 de desconto"
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


async def test_store_below_minimum_asks_to_add_items(fake):
    ifood_fake, scripted = fake
    s = connected_session()
    scripted.turns = [
        {"reply": "", "action": "search_stores", "shopping_list": SHOPPING_LIST},
        {
            "reply": "",
            "action": "search_stores",
            "shopping_list": SHOPPING_LIST + [{"name": "Cebola", "search_term": "cebola", "quantity": 1, "unit": "kg"}],
        },
    ]
    out = await agent.handle_message(s, "pode buscar")
    daki = out[-1]["data"]["options"][1]
    assert daki["name"].startswith("Daki") and daki["minimum"] == "R$ 50,00"  # subtotal R$ 15,18

    out = await agent.choose_store(s, 1, from_button=True)
    assert "faltam **R$ 34,82**" in out[-1]["text"] and "R$ 50,00" in out[-1]["text"]
    assert s.phase == "compare" and s.cart_id is None
    assert "createCart" not in ifood_fake.tools() and "listPaymentMethods" not in ifood_fake.tools()
    assert "faltam" in s.history[-1]["text"]  # the LLM sees it to help pick what to add

    await agent.handle_message(s, "adiciona mais cebola")
    assert s.phase == "compare" and len(s.shopping_list) == 4

    out = await agent.choose_store(s, 0)  # Oba is above its minimum: the cart is built
    assert s.phase == "payment" and "createCart" in ifood_fake.tools()


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
        {
            "reply": "Buscando!",
            "action": "search_stores",
            "shopping_list": [{"name": "X", "search_term": "nada", "quantity": 1, "unit": "un"}],
        }
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

    monkeypatch.setattr(
        llm, "_client", httpx.AsyncClient(base_url="http://fake", transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(config, "LLM_MODELS", list(behaviour))
    return requests


async def test_llm_falls_back_to_the_next_model(monkeypatch):
    requests = openrouter_with(
        monkeypatch,
        {
            "overloaded": 503,
            "slow": "slow",
            "chatty": "not json",
            "ok": '```json\n{"reply": "oi", "action": "none"}\n```',
        },
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
    blocked(
        "addItemToCart", {"cartId": "c1", "itemId": "i-2", "quantity": 1, "options": [{"optionId": "o"}]}, "getItem"
    )
    blocked(
        "updateCartItem",
        {"cartId": "c1", "cartItemId": "i-1", "itemId": "i-1", "quantity": 2, "options": []},
        "cartItemId",
    )  # an itemId where a cartItemId is expected
    blocked("setCartDelivery", {"cartId": "c1", "deliveryMethodId": "DEFAULT"}, "getCartDeliveryMethods")
    blocked("setCartPayment", {"cartId": "c1", "paymentMethodId": "PIX"}, "listPaymentMethods")
    blocked("setVoucher", {"cartId": "c1", "code": "X", "campaignId": "y"}, "listVouchers")
    blocked("getCart", {"cartId": "other"}, "cartId")

    g.record("getItem", {"itemId": "i-2"}, {"options": [{"optionId": "o"}]})
    g.check("addItemToCart", {"cartId": "c1", "itemId": "i-2", "quantity": 1, "options": [{"optionId": "o"}]})
    g.record("getCartDeliveryMethods", {"cartId": "c1"}, {"methods": [{"deliveryMethodId": "DEFAULT"}]})
    g.record("setCartDelivery", {"cartId": "c1", "deliveryMethodId": "DEFAULT"}, {})
    g.record(
        "listPaymentMethods",
        {"merchantId": "m1"},
        {"methods": [{"paymentMethodId": "CC", "name": "Crédito", "cards": [{"cardTokenId": "t1"}]}]},
    )
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
    s.shopping_list = shopping_list_items = recipe.parse_ingredients(SHOPPING_LIST[:1])
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


# ---------------------------------------------------------------- header, connect card, input

STATIC = Path(__file__).parent.parent / "app" / "static"


def test_header_keeps_only_the_brand():
    html = (STATIC / "index.html").read_text()
    header = re.search(r"<header.*?</header>", html, re.DOTALL).group(0)
    assert "Bateu a Vontade" in header
    for removed in ('id="demo"', 'id="connect"', 'id="connected"', "Demonstração", "Conectado", "Conectar iFood"):
        assert removed not in header
    script = (STATIC / "app.js").read_text()
    for removed in ('getElementById("connect")', 'getElementById("connected")', 'getElementById("demo")'):
        assert removed not in script


def test_chat_input_has_no_placeholder_but_is_labelled():
    tag = re.search(r'<input id="input"[^>]*>', (STATIC / "index.html").read_text()).group(0)
    assert "placeholder" not in tag and 'aria-label="Mensagem"' in tag
    assert "input.placeholder" not in (STATIC / "app.js").read_text()
    assert ".composer input:focus-visible" in (STATIC / "style.css").read_text()


def test_test_mode_notice_only_in_the_confirmation_card():
    script = (STATIC / "app.js").read_text()
    summary = re.search(r"function summaryCard.*?\n}\n", script, re.DOTALL).group(0)
    assert "Modo de teste: seu pedido não será feito." in summary
    assert script.count("Modo de teste") == 1


def test_disconnected_page_starts_with_welcome_and_connect_card():
    from fastapi.testclient import TestClient

    from app.main import app

    state = TestClient(app).get("/api/state").json()
    assert state["connected"] is False
    assert [(m["kind"], m["text"]) for m in state["messages"]] == [("login", prompts.WELCOME)]


class _ToolResult:
    def __init__(self, payload, is_error=False):
        self.content = [type("Text", (), {"type": "text", "text": json.dumps(payload)})()]
        self.structured_content = None
        self.is_error = is_error


class _MCPClient:
    def __init__(self, payload, is_error=False):
        self.result = _ToolResult(payload, is_error)

    async def call_tool(self, tool, arguments):
        return self.result


async def test_inline_authentication_error_is_unauthorized():
    payload = {"success": False, "error": {"code": "AUTHENTICATION_REQUIRED", "message": "login"}}
    with pytest.raises(ifood.Unauthorized):
        await ifood.IFoodMCP(_MCPClient(payload)).call("listAddresses", {})


async def test_auth_error_during_getuser_shows_the_connect_card(fake):
    ifood_fake, _ = fake
    ifood_fake.user = {"success": False, "error": {"code": "AUTHENTICATION_REQUIRED"}}
    s = connected_session(address=None)
    out = await agent.resume(s)
    assert out[-1]["kind"] == "login" and not s.connected


# ---------------------------------------------------------------- getUser personalization


async def test_greets_by_first_name_and_keeps_contact_data_out(fake):
    ifood_fake, scripted = fake
    s = connected_session(address=None)
    out = await agent.resume(s)
    assert ifood_fake.calls[0] == ("getUser", {"includeProfileData": True})
    assert out[-1]["kind"] == "addresses" and out[-1]["text"].startswith("Oi, Maria! 👋")

    scripted.turns = [{"reply": "Oi, Maria!", "action": "none"}]
    await agent.choose_address(s, 0, from_button=True)
    await agent.handle_message(s, "oi")
    system = agent._system_prompt(s)
    assert "Primeiro nome: Maria" in system
    for private in ("maria@test", "5511999998888", "SOUZA", "Souza"):
        assert private not in system and all(private not in m["text"] for m in s.messages)

    # Once per session: a reconnect or a new conversation doesn't call getUser again.
    await agent.resume(s)
    await agent.new_conversation(s)
    assert ifood_fake.tools().count("getUser") == 1


async def test_personal_data_is_redacted_in_logs(fake, caplog):
    _, scripted = fake
    caplog.set_level("INFO")
    await ifood.IFoodMCP(_MCPClient(BASIC_USER)).call("getUser", {"includeProfileData": True})
    cards = {"paymentMethods": [{"paymentMethodId": "pm", "cards": [{"cardTokenId": "tok-9", "lastDigits": "4242"}]}]}
    await ifood.IFoodMCP(_MCPClient(cards)).call("listPaymentMethods", {"merchantId": "m"})

    s = connected_session()
    s.profile = user.UserProfile(first_name="Maria")
    scripted.turns = [{"reply": "Oi, Maria! Bora cozinhar?", "action": "none"}]
    await agent.handle_message(s, "oi")
    logged = caplog.text
    assert "decision" in logged and "getUser" in logged
    for private in ("MARIA", "Maria", "maria@test", "5511999998888", "tok-9", "4242"):
        assert private not in logged


async def test_getuser_failure_falls_back_and_retries_once(fake, monkeypatch):
    ifood_fake, _ = fake
    monkeypatch.setattr(agent, "PROFILE_RETRY_SECONDS", 0)
    ifood_fake.user = {
        "success": False,
        "error": {"code": "SERVICE_UNAVAILABLE", "message": "down"},
        "agentHints": {"suggestedNextAction": "retry"},
    }
    s = connected_session(address=None)
    out = await agent.resume(s)
    assert out[-1]["text"].startswith("Oi! 👋") and s.profile is None  # generic greeting, no personalization

    ifood_fake.user = BASIC_USER
    await s.profile_retry
    assert s.profile.first_name == "Maria" and ifood_fake.tools().count("getUser") == 2


async def test_getuser_retry_happens_only_once(fake, monkeypatch):
    ifood_fake, _ = fake
    monkeypatch.setattr(agent, "PROFILE_RETRY_SECONDS", 0)
    ifood_fake.user = IFoodError("getUser", "timeout")
    s = connected_session(address=None)
    await agent.resume(s)
    await s.profile_retry
    assert s.profile is None and ifood_fake.tools().count("getUser") == 2
    assert "Primeiro nome" not in agent._system_prompt(s)


def test_missing_user_fields_mean_no_personalization():
    profile = user.parse_user({"success": True, "data": {"email": "x@y.z"}})
    assert profile.first_name is None and profile.prompt_block() == ""
    assert prompts.greeting(profile.first_name) == "Oi! 👋"
    assert user.parse_user({"success": True, "data": {"fullName": "  "}}).first_name is None


BEHAVIOR_USER = {
    "success": True,
    "data": {
        "fullName": "Maria Souza",
        "email": "maria@test.ifood.com.br",
        "phone": "5511999998888",
        "taxPayerIdentificationNumber": "123.456.789-10",
        "paymentMethods": [{"id": "pm2", "type": "pix"}, {"id": "pm1", "type": "credit_card", "lastDigits": "9999"}],
        "profileData": {
            "userBehaviorDescription": "Pede mercado no almoço\n\nem dias úteis.",
            "topMerchantTypes": [
                {"merchantType": "RESTAURANT", "count": "3"},
                {"merchantType": "GROCERIES", "count": "12"},
            ],
            "topWeekDayShift": {"weekDay": "WEEKDAY", "shift": "LUNCH"},
            "ordersByMerchantTypeWeekdayAndShift": {"GROCERIES": {"MONDAY": {"LUNCH": "4", "DINNER": "0"}}},
        },
    },
}


def test_behavior_data_is_parsed_leniently_and_counts_converted():
    profile = user.parse_user(BEHAVIOR_USER)
    assert profile.categories == ["Mercado (12 pedidos)", "Restaurante (3 pedidos)"]  # "12" > "3" as numbers
    assert profile.patterns == ["dias úteis · almoço", "Mercado · segunda · almoço (4 pedidos)"]
    assert profile.persona == "Pede mercado no almoço em dias úteis."
    block = profile.prompt_block()
    assert "Pagamentos salvos: Pix; Credit card final 9999" in block
    for private in ("maria@", "5511", "123.456", "Souza", "pm1", "pm2"):
        assert private not in block

    lunch = user.datetime(2026, 10, 19, 12, 30)  # a Monday
    assert user.now_line(lunch) == "Agora: segunda, 12:30 (almoço)"
    # Partial/odd shapes never break and never invent anything.
    odd = {"data": {"profileData": {"topMerchantTypes": "GROCERIES", "ordersByDay": {"Friday": "x"}}}}
    assert user.parse_user(odd).categories == ["Mercado"] and user.parse_user(odd).patterns == []


async def test_saved_payment_is_preselected_but_user_can_pick_another(fake):
    ifood_fake, scripted = fake
    s = connected_session()
    s.profile = user.parse_user(BEHAVIOR_USER)  # Pix saved first
    scripted.turns = [{"reply": "", "action": "search_stores", "shopping_list": SHOPPING_LIST[:1]}]
    await agent.handle_message(s, "pode buscar")
    out = await agent.choose_store(s, 0)
    card = out[-1]
    assert card["data"]["selected"] == 2 and "**Pix**" in card["text"]  # options: Visa, Pix, Dinheiro
    assert "Pré-selecionada (salva na conta, a pessoa ainda confirma): 2" in agent._state_block(s)
    assert "setCartPayment" not in ifood_fake.tools()  # pre-selected, not applied

    await agent.choose_payment(s, 2, from_button=True)  # Dinheiro instead
    assert ("setCartPayment", {"cartId": "cart-1", "paymentMethodId": "pm-cash"}) in ifood_fake.calls


async def test_no_preselection_without_saved_payments(fake):
    _, scripted = fake
    s = connected_session()
    s.profile = user.parse_user(BASIC_USER)
    scripted.turns = [{"reply": "", "action": "search_stores", "shopping_list": SHOPPING_LIST[:1]}]
    await agent.handle_message(s, "pode buscar")
    out = await agent.choose_store(s, 0)
    assert out[-1]["data"]["selected"] is None and "Como você quer pagar?" in out[-1]["text"]


# ---------------------------------------------------------------- submitOrder failures


async def test_inline_error_is_raised_even_without_is_error():
    payload = {"success": False, "error": {"code": "CHECKOUT_FAILED", "message": "merchant closed"}}
    with pytest.raises(IFoodError, match="CHECKOUT_FAILED: merchant closed"):
        await ifood.IFoodMCP(_MCPClient(payload)).call("submitOrder", {"cartId": "c", "userConsent": True})


@pytest.mark.parametrize(
    "answer",
    [
        IFoodError("submitOrder", "CHECKOUT_FAILED: merchant closed"),  # what IFoodMCP raises for an inline error
        {"success": True, "data": {}},  # no orderId
    ],
)
async def test_failed_submit_never_announces_an_order(fake, answer):
    ifood_fake, scripted = fake
    s = connected_session()
    await to_summary(s, scripted)
    if isinstance(answer, Exception):

        async def call(tool, args, original=ifood_fake.call):
            if tool == "submitOrder":
                ifood_fake.calls.append((tool, args))
                raise answer
            return await original(tool, args)

        ifood_fake.call = call
    else:
        ifood_fake.submit_result = answer
    out = await agent.confirm_order(s)
    assert "submitOrder" in ifood_fake.tools()
    assert s.phase == "checkout" and s.order_id is None
    assert all("Pedido feito" not in m["text"] for m in s.messages)
    assert "Não consegui finalizar o pedido" in out[-1]["text"]


# ---------------------------------------------------------------- craving chips from the behavior profile


async def test_craving_chips_come_from_the_behavior_profile(fake):
    ifood_fake, scripted = fake
    ifood_fake.user = BEHAVIOR_USER
    scripted.chips = {
        "chips": [
            "Salada leve de frango",
            "salada leve de frango",
            "Wrap rápido",
            "Um nome de prato comprido demais para caber no botão",
            "Omelete",
            "Sopa",
            "Extra",
        ]
    }
    s = connected_session(address=None)
    await agent.resume(s)
    out = await agent.choose_address(s, 0, from_button=True)
    assert out[-1]["data"]["chips"] == ["Salada leve de frango", "Wrap rápido", "Omelete", "Sopa"]
    # Only behavior data reaches that prompt: no name, contact data or payments.
    [context] = scripted.chip_requests
    assert "Pede mercado no almoço" in context and "Agora:" in context
    for private in ("Maria", "maria@", "5511", "9999", "Pix"):
        assert private not in context


async def test_no_user_data_chips_come_from_the_time_only(fake):
    _, scripted = fake
    scripted.time_chips = {"chips": ["Almoço rápido", "Salada completa", "Me dá ideias"]}
    s = connected_session(address=None)
    await agent.resume(s)  # BASIC tier: name only, no address habits
    out = await agent.choose_address(s, 0, from_button=True)
    assert out[-1]["data"]["chips"] == ["Almoço rápido", "Salada completa", "Me dá ideias"]
    assert scripted.chip_requests == []  # the behavior prompt is never used without data
    [context] = scripted.time_chip_requests  # just the clock: nothing about the person
    assert context.startswith("Agora:") and "\n" not in context and "Maria" not in context
    # Same moment, same suggestion: a new craving question doesn't call the LLM again.
    await agent.new_conversation(s)
    await agent.choose_address(s, 0, from_button=True)
    assert len(scripted.time_chip_requests) == 1


async def test_time_only_chips_are_retried_until_they_come(fake):
    _, scripted = fake
    scripted.time_chips = [llm.LLMError("down"), {"chips": []}, llm.RateLimited("429"), {"chips": ["Jantar prático"]}]
    s = connected_session(address=None)
    await agent.resume(s)
    out = await agent.choose_address(s, 0, from_button=True)
    assert out[-1]["data"]["chips"] == ["Jantar prático"]  # no fixed fallback: it kept trying
    assert len(scripted.time_chip_requests) == 4


async def test_time_only_prompt_is_not_used_when_there_is_user_data(fake):
    ifood_fake, scripted = fake
    ifood_fake.user = BEHAVIOR_USER
    scripted.chips = {"chips": ["Wrap rápido"]}
    s = connected_session(address=None)
    await agent.resume(s)
    await agent.choose_address(s, 0, from_button=True)
    assert scripted.time_chip_requests == [] and len(scripted.chip_requests) == 1


async def test_failed_profile_suggestion_is_retried(fake):
    ifood_fake, scripted = fake
    ifood_fake.user = BEHAVIOR_USER
    scripted.chips = [llm.LLMError("down"), {"chips": ["Wrap rápido"]}]
    s = connected_session(address=None)
    await agent.resume(s)
    out = await agent.choose_address(s, 0, from_button=True)
    assert out[-1]["data"]["chips"] == ["Wrap rápido"]
    assert len(scripted.chip_requests) == 2


async def test_profile_without_food_info_uses_the_time_chips(fake):
    ifood_fake, scripted = fake
    ifood_fake.user = BEHAVIOR_USER
    scripted.chips = {"chips": []}  # "nothing useful about food" in the profile
    s = connected_session(address=None)
    await agent.resume(s)
    out = await agent.choose_address(s, 0, from_button=True)
    assert out[-1]["data"]["chips"] == TIME_CHIPS
    assert len(scripted.chip_requests) == 1  # an empty profile answer is final, not retried


async def test_slow_suggestion_is_waited_for(fake, monkeypatch):
    ifood_fake, _ = fake
    ifood_fake.user = BEHAVIOR_USER
    release = asyncio.Event()

    async def slow(*args, **kwargs):
        await release.wait()
        return {"chips": ["Wrap rápido"]}

    monkeypatch.setattr(llm, "generate_json", slow)
    s = connected_session(address=None)
    await agent.resume(s)
    reply = asyncio.create_task(agent.choose_address(s, 0, from_button=True))
    await asyncio.sleep(0.05)
    assert not reply.done() and s.activity == "Preparando sugestões…"  # no time limit, no fixed list
    release.set()
    out = await reply
    assert out[-1]["data"]["chips"] == ["Wrap rápido"]


# ---------------------------------------------------------------- address habits (listAddresses analytics)

HOME_HABITS = {
    "topMerchantTypes": [{"merchantType": "GROCERIES", "count": "7"}],
    "topWeekDayShift": "WEEKEND_DINNER",
    "ordersByMerchantTypeWeekdayAndShift": {"GROCERIES": {"SATURDAY": {"DINNER": "5"}}},
}


def with_home_habits(ifood_fake, monkeypatch, nested=False):
    original = ifood_fake.call

    async def call(tool, args):
        result = await original(tool, args)
        if tool == "listAddresses":
            home = next(a for a in result["addresses"] if a["addressId"] == "addr-home")
            home.update({"usagePattern": dict(HOME_HABITS)} if nested else HOME_HABITS)
        return result

    monkeypatch.setattr(ifood_fake, "call", call)


@pytest.mark.parametrize("nested", [False, True])  # fields on the address or under usagePattern (docs example)
def test_address_habits_are_parsed(nested):
    entry = {"addressId": "a", "alias": "CASA", "coordinates": {"latitude": 1, "longitude": 2}}
    entry.update({"usagePattern": dict(HOME_HABITS)} if nested else HOME_HABITS)
    [address] = shopping.parse_addresses({"addresses": [entry]})
    assert address.habits == HOME_HABITS
    merged = user.with_address_habits(user.UserProfile(first_name="Maria"), address.habits)
    assert merged.categories == ["Mercado (7 pedidos)"]
    assert merged.patterns == ["fim de semana · jantar", "Mercado · sábado · jantar (5 pedidos)"]
    assert merged.first_name == "Maria"


def test_address_habits_merge_without_repeats_or_mutating_the_profile():
    profile = user.parse_user(BEHAVIOR_USER)  # already has Mercado (12) and a weekday-lunch pattern
    merged = user.with_address_habits(profile, HOME_HABITS)
    assert merged.categories == ["Mercado (12 pedidos)", "Restaurante (3 pedidos)"]
    assert "fim de semana · jantar" in merged.patterns and "fim de semana · jantar" not in profile.patterns
    assert user.with_address_habits(None, {}).has_behavior is False


async def test_chips_use_the_chosen_address_habits(fake, monkeypatch):
    ifood_fake, scripted = fake
    with_home_habits(ifood_fake, monkeypatch)
    scripted.chips = {"chips": ["Pizza caseira", "Hambúrguer artesanal"]}
    s = connected_session(address=None)
    await agent.resume(s)  # BASIC getUser: the behavior comes only from the address
    await asyncio.gather(*s.chips_tasks.values())  # started for the first (last used) address while the user chooses
    assert len(scripted.chip_requests) == 1
    out = await agent.choose_address(s, 0, from_button=True)  # Casa
    assert out[-1]["data"]["chips"] == ["Pizza caseira", "Hambúrguer artesanal"]
    [context] = scripted.chip_requests  # the same suggestion, not a second call
    assert "Mercado (7 pedidos)" in context and "fim de semana · jantar" in context
    assert "Maria" not in context
    # The main agent sees the address habits too.
    assert "Mercado · sábado · jantar (5 pedidos)" in agent._system_prompt(s)


async def test_address_without_habits_gets_time_chips(fake, monkeypatch):
    ifood_fake, scripted = fake
    with_home_habits(ifood_fake, monkeypatch)
    s = connected_session(address=None)
    await agent.resume(s)
    out = await agent.choose_address(s, 1, from_button=True)  # Trabalho: no analytics
    assert out[-1]["data"]["chips"] == TIME_CHIPS
    await asyncio.gather(*s.chips_tasks.values())
    assert len(scripted.chip_requests) == 1  # only the pre-started one for Casa
    assert "Perfil da pessoa (do iFood" in agent._system_prompt(s)  # name only from getUser
    assert "fim de semana" not in agent._system_prompt(s)
