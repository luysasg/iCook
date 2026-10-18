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
with the test user. The login opens in a popup (or a new tab if pop-ups are blocked); the
chat tab stays put and continues the interrupted task when the login finishes.

**Demo mode:** `DRY_RUN=true` in `.env` runs everything against iFood (cart, payment, voucher,
`getCart`) except the final `submitOrder`; the tracker shows a simulated status.

Tests (offline, fake iFood + scripted LLM): `uv run pytest` · Lint: `uvx ruff check app tests`

## How it works

| Step | Who does it |
|---|---|
| Login | Required before anything. Messages typed before login are kept and replayed after it |
| Delivery address | Right after login: `listAddresses`, the user picks where they are now (button or text). "Trocar endereço" later redoes the comparison |
| Dish, servings, allergies | Gemini (one call per user message, JSON output, one question at a time, quick-reply chips) |
| Recipe → pantry | The recipe card comes first (numbered ingredient checklist + steps), then "what do you already have?". Numbers ("1, 3"), "não tenho nenhum" and "tenho todos" are parsed in code; names go through Gemini. Only the missing ingredients are searched |
| Store comparison | Code: one `searchOffers` per ingredient across open GROCERIES stores, top `MAX_STORES` by coverage, gap-filling searches inside each of them, then **one** Gemini call to match products, round quantities up and label them compatible / uncertain / avoid for the user's restrictions |
| Cart | Code: `createCart` (items with `options: []`), or `getCart` + add/update/remove when that store already has a cart → `getCartDeliveryMethods` → `setCartDelivery` (ASAP) |
| Payment | `listPaymentMethods` → the user picks (card token only for saved cards) → `setCartPayment` |
| Voucher | `listVouchers` → the user picks one or "sem cupom" → `setVoucher` (code + campaignId from the list) |
| Review | `getCart` → summary card (items, fees, voucher, total, optional CPF/CNPJ/passport) |
| Order | Only the **Confirmar pedido** button calls `submitOrder` (`userConsent: true`). The LLM has no access to it |
| After the order | `trackOrder` right away; the tracker card polls at the `agentHints.retryMessage` cadence and stops on `pollingStop`. "o que eu pedi?" → `getOrder` |

The order of these steps is enforced in code by `app/guard.py`, which checks every MCP call before
it is sent: IDs must come from the matching list/get response, `options` is always present, a store's
open cart is resumed instead of recreated, and `submitOrder` needs delivery + payment set, a fresh
`getCart`, the button click and `userConsent: true` (and is always blocked in `DRY_RUN`).

Code map: `app/main.py` (web + popup login), `app/agent.py` (conversation state machine),
`app/guard.py` (MCP rule enforcement), `app/shopping.py` (search/compare/cart),
`app/prompts.py` (prompts), `app/ifood.py` (MCP client), `app/auth.py` (OAuth + PKCE),
`app/static/` (chat UI).

## Known gaps / assumptions (first version)

- **Login is browser-based, not QR.** `id.ifood.com.br` only supports `authorization_code`
  and `refresh_token` grants (no device flow), and the client rejects non-loopback redirects,
  so a phone can't complete the login for the laptop.
- **No scopes are requested**: the client is not allowed to request any of the documented ones.
- **Cart/payment/voucher/order response shapes are undocumented**, so they are read leniently
  (`app/jsonutil.py`). Every MCP call and response is logged to the server console (the full
  `getCart` payload included); the UI only shows the clean summary.
- **Card methods without a saved card** are not offered: `setCartPayment` needs a `cardTokenId`.
- **Weight-sold items** (e.g. "Tomate Italiano Kg.") have no unit field; the matcher assumes
  the price is per kg and rounds up.
- **No address creation**: if the account has no saved address, the user is asked to add one
  in the iFood app.
- Sessions live in memory: a page reload keeps the whole conversation, a server restart doesn't
  (log in again).
- **Grocery items are added with `options: []`** without calling `getItem` first; any non-empty
  option list is rejected by the guard unless its IDs came from `getItem`. Gemini free tier is ~10 requests/minute.
- **Gemini fallback chain** (`GEMINI_MODELS`): each model gets `GEMINI_TIMEOUT` seconds (45 s for
  product matching); on a timeout or any API error (overload, 429, 404) the next model answers.
