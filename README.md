# Bateu a Vontade

A chatbot that turns a food craving into an iFood grocery order. The user chats in
Brazilian Portuguese until they settle on a recipe and a shopping list (scaled to the
number of people, minus what they already have, respecting allergies). The agent then
compares open grocery stores on iFood, builds the cart in the store the user picks, and
places the order only when the user clicks **Confirmar pedido**.

## Run it

```bash
cp .env.example .env      # fill IFOOD_CLIENT_ID and OPENROUTER_API_KEY
uv sync
uv run uvicorn app.main:app --port 8000
```

Open http://localhost:8000 (it must be `localhost`: the iFood OAuth client only accepts
`http://localhost[:port]/callback` as redirect URI), click **Conectar ao iFood** on the card in
the chat, and log in with the test user. The login opens in a popup (or a new tab if pop-ups are
blocked); the chat tab stays put and continues the interrupted task when the login finishes. The
same card shows up in the chat whenever iFood says the session is no longer authenticated.

**Test mode:** `DRY_RUN=true` in `.env` runs everything against iFood (cart, payment, voucher,
`getCart`) except the final `submitOrder`; the confirmation card says "Modo de teste: seu pedido não
será feito." and the tracker shows a simulated status.

Tests (offline, fake iFood + scripted LLM): `uv run pytest` · Lint: `uvx ruff check app tests`

## How it works

| Step | Who does it |
|---|---|
| Login | Required before anything. Messages typed before login are kept and replayed after it |
| Profile | `getUser` once per session, right after login: greeting by first name; a compact profile (first name, saved payment methods, behavior data when the tier has it) goes into the system prompt. Email, phone, full name, tax ID and user IDs never reach the LLM, the UI or the logs. On failure: generic greeting, one retry in the background |
| Delivery address | Right after login: `listAddresses`, the user picks where they are now (button or text). "Trocar endereço" later redoes the comparison |
| Dish, servings, allergies | LLM via OpenRouter (one call per user message, JSON-schema output, one question at a time, quick-reply chips) |
| Recipe → pantry | The recipe card comes first (numbered ingredient checklist + steps), then "what do you already have?". Numbers ("1, 3"), "não tenho nenhum" and "tenho todos" are parsed in code; names go through the LLM. Only the missing ingredients are searched |
| Store comparison | Code: one `searchOffers` per ingredient across open GROCERIES stores, top `MAX_STORES` by coverage, gap-filling searches inside each of them, then **one** LLM call to match products, round quantities up and label them compatible / uncertain / avoid for the user's restrictions |
| Cart | Code: `createCart` (items with `options: []`), or `getCart` + add/update/remove when that store already has a cart → `getCartDeliveryMethods` → `setCartDelivery` (ASAP) |
| Payment | `listPaymentMethods` → the user picks (card token only for saved cards; a method saved in `getUser` comes pre-selected but must still be confirmed) → `setCartPayment` |
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
`app/prompts.py` (prompts), `app/user.py` (getUser profile), `app/ifood.py` (MCP client + log redaction), `app/auth.py` (OAuth + PKCE),
`app/static/` (chat UI).

## Known gaps / assumptions (first version)

- **Login is browser-based, not QR.** `id.ifood.com.br` only supports `authorization_code`
  and `refresh_token` grants (no device flow), and the client rejects non-loopback redirects,
  so a phone can't complete the login for the laptop.
- **No scopes are requested**: the client is not allowed to request any of the documented ones.
  So `getUser` returns only the BASIC tier for the test account (`fullName`, `email`, `phone`):
  no saved payment methods and no `profileData`. Pre-selection and behavior-based suggestions
  follow the documented ADVANCED/BEHAVIOR shapes but are only tested offline.
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
  option list is rejected by the guard unless its IDs came from `getItem`.
- **LLM fallback chain** (`OPENROUTER_MODELS`): each model gets `LLM_TIMEOUT` seconds (45 s for
  product matching); on a timeout, any API error (overload, 429, no provider) or invalid JSON the
  next model answers. Models must support JSON-schema output (`structured_outputs` on
  openrouter.ai/models). `:free` models are rate-limited (per minute and per day; higher daily
  limit once the account has bought credits) and may log prompts; switch to a paid model by
  editing `OPENROUTER_MODELS`, no code change.
