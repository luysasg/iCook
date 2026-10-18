# Documento Técnico Resumido — iCook

## Arquitetura

```
Navegador (HTML/CSS/JS puro)
      │  JSON (/api/*), sessão por cookie
      ▼
FastAPI (app/main.py)
      │
      ▼
Agente — máquina de estados (app/agent.py)
      │  address → chat → pantry → compare → payment → voucher → checkout → ordered
      ├──► LLM via OpenRouter (app/llm.py): saída JSON validada por schema, modelos de reserva
      └──► Guard (app/guard.py) ──► Cliente MCP (app/ifood.py) ──► MCP iFood (Streamable HTTP)
                                                                 consumer-mcp.ifood.com.br/marketplace
```

| Módulo | Responsabilidade |
|---|---|
| `agent.py` | Fases da conversa; traduz a decisão do LLM no próximo passo do iFood |
| `guard.py` | Regras do checkout validadas em toda chamada MCP |
| `shopping.py` | Busca, comparação de mercados, carrinho, pagamento, cupom e resumo |
| `recipe.py` | Receita e parsing das respostas (despensa, documento) |
| `ifood.py` | Cliente MCP, detecção de 401/erros inline e remoção de dados pessoais dos logs |
| `auth.py` | OAuth 2.1 + PKCE, troca e renovação de token |
| `llm.py` / `prompts.py` | Cliente OpenRouter, prompts e JSON Schemas |
| `tracking.py` / `views.py` | Card de acompanhamento e payloads dos cards da interface |

**Stack:** Python 3.14 + uv, FastAPI/Uvicorn, SDK oficial `mcp`, httpx, OpenRouter; front-end sem build;
pytest (testes offline com iFood falso e LLM roteirizado) e Ruff.

## Uso do MCP

| Fase | Ferramentas |
|---|---|
| Perfil e endereço | `getUser`, `listAddresses` |
| Busca | `searchOffers` (`merchantType: GROCERIES`, `resultKind: ITEM`, `onlyOpen`, e `merchantId` nas buscas complementares) |
| Carrinho | `createCart`, `getCart`, `addItemToCart`, `updateCartItem`, `removeItemFromCart` |
| Entrega | `getCartDeliveryMethods`, `setCartDelivery` (entrega imediata, ignora retirada) |
| Pagamento e cupom | `listPaymentMethods`, `setCartPayment`, `listVouchers`, `setVoucher` |
| Pedido | `submitOrder`, `trackOrder`, `getOrder` |

Estratégia de busca: uma `searchOffers` por ingrediente em paralelo (concorrência 4) → ranqueia
mercados por cobertura e distância → buscas complementares só para os buracos dos melhores
mercados → **uma única** chamada ao LLM para escolher produto e quantidade por mercado. Isso mantém
o número de chamadas ao LLM constante, independente do tamanho da lista.

## Decisões técnicas principais

1. **O LLM não chama ferramentas MCP.** Ele devolve um JSON (`reply`, `action`, campos da ação)
   validado por schema; o código executa. Elimina IDs inventados, passos pulados e pedidos não
   autorizados. `submitOrder` só é alcançável pelo endpoint do botão.
2. **Guard em toda chamada MCP.** IDs (entrega, pagamento, cartão, cupom, `cartItemId`) precisam ter
   vindo da resposta de listagem correspondente; `options` sempre enviado; carrinho existente é
   retomado em vez de recriado (`createCart` não é idempotente); `submitOrder` exige entrega,
   pagamento, `getCart` após a última alteração, clique no botão e `userConsent: true`.
3. **Leitura tolerante das respostas** (`jsonutil.py`). Formatos de carrinho, pagamento, cupom e
   pedido não são documentados; o código procura chaves conhecidas em qualquer nível e trata erros
   que vêm inline (`success: false`) sem `isError`.
4. **401 detectado por hook HTTP.** O SDK MCP transforma o 401 em erro genérico; um hook no httpx
   marca a rejeição e a sessão volta ao card de login, guardando a ação para retomar.
5. **Privacidade por padrão.** Lista de campos sensíveis removidos dos logs; o perfil passa ao LLM
   só por lista de campos permitidos; documento nunca entra no histórico.
6. **Resiliência do LLM.** Cadeia de modelos de reserva no OpenRouter (timeout, sobrecarga ou JSON
   inválido → próximo modelo), trocável só por `.env`.
7. **`DRY_RUN`.** Fluxo inteiro contra o iFood real, exceto `submitOrder`, para demonstração segura.

## Limitações conhecidas

Sessões em memória; login por navegador (sem QR/device flow); sem escopos OAuth, `getUser` só no nível
BASIC; cartões não salvos não são oferecidos (`setCartPayment` exige `cardTokenId`); itens por peso
assumidos como preço por kg; itens entram com `options: []` sem consultar `getItem`.
