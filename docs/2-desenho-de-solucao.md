# Desenho de Solução — iCook

## Visão geral

Um chat web em que o **LLM conduz a conversa** e o **código conduz o iFood**. O LLM entende o que
a pessoa quer e decide o próximo passo; todas as chamadas ao MCP do iFood são feitas por código,
validadas antes do envio. O pedido só sai pelo botão **Confirmar pedido**.

## Fluxo

```
Login iFood ─► Endereço ─► Vontade ─► Pessoas ─► Restrições ─► Receita
                                                                  │
          ┌───────────────────────────────────────────────────────┘
          ▼
   O que já tem em casa? ─► Busca e comparação de mercados ─► Escolha do mercado
                                                                  │
          ┌───────────────────────────────────────────────────────┘
          ▼
   Carrinho + entrega ─► Pagamento ─► Cupom ─► Resumo ─► [Confirmar pedido] ─► Acompanhamento
```

| Etapa | O que acontece | MCP |
|---|---|---|
| 1. Login | OAuth 2.1 + PKCE em pop-up. Mensagens enviadas antes ficam guardadas e são retomadas. | — |
| 2. Perfil e endereço | Carrega o perfil (sugestões personalizadas) e a pessoa escolhe um endereço salvo. | `getUser`, `listAddresses` |
| 3. Conversa | Prato → quantas pessoas → alergias/restrições, uma pergunta por vez. | — |
| 4. Receita | Card com ingredientes numerados e modo de preparo, já ajustados às pessoas e restrições. | — |
| 5. Despensa | "Quais você já tem?" — por números, nomes ou "tenho todos". Só o que falta segue. | — |
| 6. Comparação | Uma busca por ingrediente; os `MAX_STORES` mercados com maior cobertura; buscas complementares dentro de cada um; uma chamada ao LLM escolhe produtos, arredonda quantidades e rotula alérgenos. | `searchOffers` |
| 7. Mercado | A pessoa escolhe. Abaixo do mínimo, o agente sugere itens ou outro mercado. Cria o carrinho (ou retoma o existente) e define entrega imediata. | `createCart`/`getCart`, `add/update/removeItemFromCart`, `getCartDeliveryMethods`, `setCartDelivery` |
| 8. Pagamento e cupom | Lista formas de pagamento (pré-seleciona a salva) e cupons disponíveis. | `listPaymentMethods`, `setCartPayment`, `listVouchers`, `setVoucher` |
| 9. Resumo | Itens, subtotal, entrega, descontos e total vindos do carrinho real. CPF opcional. | `getCart` |
| 10. Pedido | Só pelo botão. Se o iFood exigir documento, o agente pede o CPF e reenvia. | `submitOrder` |
| 11. Acompanhamento | Card de status atualizado no intervalo indicado pelo iFood; "o que eu pedi?" responde com os dados do pedido. | `trackOrder`, `getOrder` |

## Regras de negócio

- **Um mercado por pedido**, nunca dividido. O agente compara e a pessoa escolhe.
- **Quantidades arredondadas para cima** até a quantidade comprável.
- **Ingrediente não encontrado** em nenhum mercado: o agente avisa e o pedido segue sem ele, se a pessoa quiser.
- **Nenhum mercado aberto**: o agente informa e sugere tentar depois ou trocar ingredientes.
- **Troca de endereço** no meio do fluxo refaz a busca (mercados e taxas dependem dele).
- **Modo de teste** (`DRY_RUN`): todo o fluxo roda contra o iFood real, menos o `submitOrder`.
