# Feedback sobre o MCP do iFood

Dores reais encontradas ao construir o iCook sobre o MCP `consumer-mcp.ifood.com.br/marketplace`,
em ordem de impacto.

## 1. Gestão de endereços (principal feedback)

Hoje o MCP permite **listar** (`listAddresses`) e **criar** (`createAddress`) endereços, mas não
permite **remover** nem **alterar**. Para um agente conversacional isso é um beco sem saída: se a
pessoa diz "não moro mais lá" ou "o número está errado", o agente só pode mandar ela ao app.
Testes e demos também acumulam endereços que não dá para limpar.

Sugestões:

- **`deleteAddress`**, aceitando mais de uma forma de identificar o endereço:
  - por `addressId`;
  - por **alias** ("casa", "trabalho");
  - pelo **endereço completo** (rua, número, complemento), útil quando o agente só tem o texto que a
    pessoa falou.
  - Com ambiguidade (dois "casa"), devolver os candidatos em vez de apagar.
- **`updateAddress`**: alterar alias, complemento, número, ponto de referência e marcar como padrão,
  sem apagar e recriar (o que perde o histórico de uso do endereço).
- **`setDefaultAddress`** (ou campo no `updateAddress`) para o endereço principal.
- Retornar o endereço já **normalizado com coordenadas** em todas as respostas; hoje há endereços
  sem coordenadas que precisamos descartar.

## 2. Formatos de resposta não documentados

Carrinho, formas de pagamento, cupons e pedido não têm schema de saída documentado. Tivemos que
escrever um leitor tolerante que procura chaves em qualquer nível (`total`, `totalAmount`,
`totalPrice`...). Um `outputSchema` por ferramenta resolveria.

## 3. Carrinho

- `createCart` não é idempotente e não existe `listCarts`/`deleteCart`: se o agente perde o
  `cartId`, não há como recuperar ou limpar o carrinho aberto.
- Não há cancelamento de pedido.
- `setCartPayment` exige `cardTokenId`, então não é possível pagar com cartão não salvo.

