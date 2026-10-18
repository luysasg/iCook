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

## 3. Erros inconsistentes

Alguns erros vêm com `isError`, outros inline como `{"success": false, "error": {...}}` com sucesso
no protocolo. A exigência de CPF no `submitOrder` só é descoberta pela mensagem de erro em texto.
Sugestão: sempre `isError` + `code` estável (ex.: `DOCUMENT_REQUIRED`, `BELOW_MINIMUM_ORDER`) e
informar no `getCart` se o documento será exigido.

## 4. Autenticação diferente da documentação

- A documentação descreve o fluxo Pending Action (QR code / device flow), mas o servidor de
  autorização do MCP (`id.ifood.com.br`) só aceita `authorization_code` e `refresh_token`.
- O client só aceita redirecionamento loopback e recusa todos os escopos documentados; sem
  escopos, o `getUser` só retorna o nível BASIC e não conseguimos testar a personalização de verdade.
- URLs de ambiente divergem entre páginas da documentação.

## 5. Carrinho

- `createCart` não é idempotente e não existe `listCarts`/`deleteCart`: se o agente perde o
  `cartId`, não há como recuperar ou limpar o carrinho aberto.
- Não há cancelamento de pedido.
- `setCartPayment` exige `cardTokenId`, então não é possível pagar com cartão não salvo.

## 6. Catálogo de mercado

- Itens vendidos por peso ("Tomate Italiano Kg.") não têm campo de unidade/peso, só "Compra por
  Peso" na descrição, e `quantity` é inteiro. O agente precisa adivinhar se o preço é por kg.
- `getMerchant` traz só resumo e destaques, não o catálogo; a busca dentro da loja exige
  `searchOffers` com `merchantId`.
- `listItemTaxonomy` está documentada, mas não existe no servidor.
- Informação de alérgenos/ingredientes estruturada nos itens ajudaria muito um agente culinário;
  hoje rotulamos por inferência sobre o nome.

## O que funcionou bem

Um único servidor MCP com todas as ferramentas, `searchOffers` com filtros úteis (`onlyOpen`,
`merchantType`, `merchantId`), a exigência de `userConsent` no `submitOrder` e o `trackOrder`
indicando o intervalo de polling.
