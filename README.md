# Bateu a Vontade

A chatbot that turns a food craving into an iFood grocery order. The user chats in
Brazilian Portuguese until they settle on a recipe and a shopping list (scaled to the
number of people, minus what they already have, respecting allergies). The agent then
compares open grocery stores on iFood, builds the cart in the store the user picks, and
places the order only when the user clicks **Confirmar pedido**.

## Run it

```bash
cp .env.example .env      # fill IFOOD_CLIENT_ID and GEMINI_API_KEY
uv sync
uv run uvicorn app.main:app --port 8000
```

Open http://localhost:8000 (it must be `localhost`: the iFood OAuth client only accepts
`http://localhost[:port]/callback` as redirect URI), click **Conectar iFood**, and log in
with the test user.

Tests (offline, fake iFood + scripted LLM): `uv run pytest`

## How it works

| Step | Who does it |
|---|---|
| Login | Required before anything: every message gets a **Conectar iFood** button until the account is connected |
| Delivery address | Right after login: `listAddresses`, the user picks where they are now (button or text). "Trocar endereço" later redoes the comparison |
| Recipe, servings, allergies, pantry, shopping list | Gemini (one call per user message, JSON output) |
| Store comparison | Code: one `searchOffers` per ingredient across open GROCERIES stores, top `MAX_STORES` by coverage, gap-filling searches inside each of them, then **one** Gemini call to match products and round quantities up |
| Cart | Code: `createCart` → `getCartDeliveryMethods` → `setCartDelivery` (ASAP) → `listPaymentMethods` → `setCartPayment` → `getCart` |
| Order | Only the **Confirmar pedido** button calls `submitOrder` (`userConsent: true`). The LLM has no access to it. |
| After the order | "cadê meu pedido?" → `trackOrder`; "o que eu pedi?" → `getOrder` |

Code map: `app/main.py` (web + login), `app/agent.py` (conversation state machine),
`app/shopping.py` (search/compare/cart), `app/prompts.py` (PT-BR prompts),
`app/ifood.py` (MCP client), `app/auth.py` (OAuth + PKCE), `app/static/` (chat UI).

## Known gaps / assumptions (first version)

- **Login is browser-based, not QR.** `id.ifood.com.br` only supports `authorization_code`
  and `refresh_token` grants (no device flow), and the client rejects non-loopback redirects,
  so a phone can't complete the login for the laptop.
- **No scopes are requested**: the client is not allowed to request any of the documented ones.
- **Cart/payment/order response shapes are undocumented**, so they are read leniently
  (`app/jsonutil.py`). Every MCP call and response is logged; the summary card has a
  "Detalhes técnicos do carrinho" section with the raw `getCart` JSON.
- **Payment** is chosen automatically: saved card → PIX → first online method.
- **Weight-sold items** (e.g. "Tomate Italiano Kg.") have no unit field; the matcher assumes
  the price is per kg and rounds up.
- **No address creation**: if the account has no saved address, the user is asked to add one
  in the iFood app.
- Sessions live in memory (restart = log in again). Gemini free tier is ~10 requests/minute.
- **Gemini fallback chain** (`GEMINI_MODELS`): each model gets `GEMINI_TIMEOUT` seconds (45 s for
  product matching); on a timeout or any API error (overload, 429, 404) the next model answers.
