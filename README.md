# Bateu a Vontade

Um chatbot que transforma uma vontade de comer em um pedido de mercado no iFood. A pessoa
conversa em português até definir uma receita: o agente pergunta o prato, para quantas pessoas e
se há alergias ou restrições, mostra a receita com os ingredientes numerados e pergunta o que ela
já tem em casa. Com o que falta, ele compara os mercados abertos que entregam no endereço
escolhido, monta o carrinho no mercado que a pessoa escolher e só faz o pedido quando ela toca
em **Confirmar pedido**.

Projeto feito para um hackathon do iFood. Serve para quem quer cozinhar em casa sem montar a
lista de compras na mão.

## Funcionalidades

- **Login com a conta iFood** (OAuth 2.1 + PKCE) em uma janela pop-up. Mensagens enviadas antes do
  login ficam guardadas e são retomadas depois dele.
- **Escolha do endereço de entrega** entre os endereços salvos na conta (`listAddresses`).
- **Conversa guiada por LLM**, via OpenRouter: uma pergunta por vez, com sugestões de resposta
  rápida (*chips*).
- **Sugestões personalizadas de pratos** a partir do perfil (`getUser`) e dos hábitos do endereço.
  Sem dados de perfil, as sugestões levam em conta só o dia e o horário.
- **Card da receita** com ingredientes numerados e modo de preparo. A resposta sobre o que já tem
  em casa pode vir em números ("1, 3"), como "tenho todos"/"não tenho nenhum" ou com os nomes.
- **Comparação de mercados**: uma busca por ingrediente, os `MAX_STORES` mercados com maior
  cobertura, buscas complementares dentro de cada um e uma única chamada ao LLM para escolher os
  produtos, arredondar quantidades para cima e rotular cada item como compatível, incerto ou
  evitar em relação às restrições alimentares.
- **Aviso de pedido mínimo**: se a lista não atinge o mínimo de um mercado, o agente sugere
  adicionar itens ou escolher outro mercado.
- **Carrinho, entrega, pagamento e cupom** feitos por código: o carrinho já aberto em um mercado é
  retomado em vez de recriado, e um pagamento salvo na conta vem pré-selecionado (mas a pessoa
  precisa confirmar).
- **Resumo do carrinho** (`getCart`) com o total e um campo opcional de CPF/CNPJ/passaporte.
- **Pedido só pelo botão**: o LLM não tem acesso ao `submitOrder`.
- **Acompanhamento do pedido**: o card de status consulta o `trackOrder` no intervalo que o
  próprio iFood indica. "O que eu pedi?" responde com os dados do `getOrder`.
- **Modo de teste** (`DRY_RUN=true`): o fluxo inteiro roda contra o iFood, menos o `submitOrder`. O
  status do pedido é simulado.
- **Regras do checkout aplicadas em código** (`app/guard.py`): toda chamada MCP é validada antes do
  envio. Os IDs precisam ter vindo da resposta de listagem correspondente, `options` é sempre
  enviado, e o `submitOrder` exige entrega e pagamento definidos, um `getCart` depois da última
  alteração, o clique no botão e `userConsent: true`.
- **Privacidade**: e-mail, telefone, nome completo, CPF e IDs da conta nunca chegam ao LLM, à
  interface ou aos logs do servidor.

## Tecnologias

- **Python 3.14**, gerenciado com [uv](https://docs.astral.sh/uv/)
- **FastAPI** + **Uvicorn**: servidor web e API do chat
- **mcp** (SDK oficial do Model Context Protocol): cliente do servidor MCP do iFood (Streamable HTTP)
- **httpx**: chamadas ao OpenRouter e ao servidor de autenticação do iFood
- **python-dotenv**: leitura do `.env`
- **OpenRouter**: acesso ao LLM, com saída validada por JSON Schema e uma cadeia de modelos de reserva
- Front-end em HTML, CSS e JavaScript puros (`app/static/`), sem etapa de build
- **pytest** + **pytest-asyncio** (testes) e **Ruff** (lint e formatação)

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/getting-started/installation/), que instala o Python 3.14
  automaticamente se ele não estiver na máquina
- Um **client OAuth do iFood** (`IFOOD_CLIENT_ID`) com redirecionamento para
  `http://localhost:<porta>/callback`
- Uma **chave de API do OpenRouter**: https://openrouter.ai/keys

## Instalação

```bash
git clone <url-do-repositório>
cd Hackaton-IFOOD
uv sync
cp .env.example .env
```

Depois, preencha pelo menos `IFOOD_CLIENT_ID` e `OPENROUTER_API_KEY` no `.env`.

## Configuração

As variáveis ficam no `.env`. O arquivo `.env.example` traz todas elas com os valores padrão.

| Variável | Obrigatória | Padrão | Para que serve |
|---|---|---|---|
| `IFOOD_CLIENT_ID` | sim | — | ID do client OAuth do iFood |
| `IFOOD_CLIENT_SECRET` | não | vazio | Só se o endpoint de token exigir autenticação do client |
| `IFOOD_MCP_URL` | não | `https://consumer-mcp.ifood.com.br/marketplace` | Servidor MCP do iFood (também usado como `resource` no OAuth) |
| `IFOOD_AUTH_URL` | não | `https://id.ifood.com.br` | Servidor de autorização do iFood |
| `IFOOD_SCOPE` | não | vazio | Escopos OAuth. Deixe vazio: o client não pode pedir os escopos documentados |
| `OPENROUTER_API_KEY` | sim | — | Chave da API do OpenRouter |
| `OPENROUTER_URL` | não | `https://openrouter.ai/api/v1` | URL base da API do OpenRouter |
| `OPENROUTER_MODELS` | não | três modelos `:free` (veja `.env.example`) | Modelos separados por vírgula, testados em ordem: se um demora, está sobrecarregado ou devolve JSON inválido, o próximo responde. Use só modelos com suporte a `structured_outputs` |
| `LLM_TIMEOUT` | não | `25` | Segundos de espera por modelo antes de passar ao próximo (a escolha de produtos usa 45 s) |
| `APP_PORT` | não | `8000` | Porta usada para montar a URL de retorno do login (`http://localhost:<APP_PORT>/callback`). Precisa ser a mesma porta passada ao `uvicorn` |
| `MAX_STORES` | não | `3` | Quantos mercados comparar lado a lado |
| `DRY_RUN` | não | `false` | `true` executa tudo, menos o `submitOrder` final (bom para demonstrações) |

> Os modelos `:free` têm limite de requisições por minuto e por dia, e podem registrar os
> prompts. Para usar um modelo pago, basta trocar `OPENROUTER_MODELS`, sem mudar código.

## Como usar

```bash
uv run uvicorn app.main:app --port 8000
```

1. Abra http://localhost:8000. Precisa ser `localhost`, porque o client OAuth do iFood só aceita
   `http://localhost[:porta]/callback` como redirecionamento.
2. Clique em **Conectar ao iFood** no card do chat e faça login. O login abre em um pop-up (ou em
   uma nova aba, se pop-ups estiverem bloqueados), e a conversa continua de onde parou.
3. Escolha o endereço, diga o que quer comer e siga a conversa até o botão **Confirmar pedido**.

Para testar sem fazer pedidos de verdade, coloque `DRY_RUN=true` no `.env`.

A interface usa uma API JSON simples. A sessão fica em um cookie `sid`. Por exemplo:

```bash
curl -c cookies.txt http://localhost:8000/api/state
curl -b cookies.txt -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" -d '{"message": "quero fazer uma lasanha"}'
```

Principais rotas: `GET /api/state`, `POST /api/chat`, `POST /api/choose_address`,
`POST /api/choose_store`, `POST /api/choose_payment`, `POST /api/choose_voucher` (corpo
`{"number": n}`; no cupom, `0` = sem cupom), `POST /api/confirm`, `POST /api/track`,
`POST /api/reset` e `POST /api/logout`. Sem login, toda mensagem só recebe o card para conectar a conta.

## Testes

Os testes rodam offline, com um iFood falso e respostas roteirizadas do LLM. Mesmo assim, o
`app/config.py` exige `IFOOD_CLIENT_ID` e `OPENROUTER_API_KEY` no ambiente ou no `.env`.
Qualquer valor serve.

```bash
uv run pytest
uvx ruff check app tests
uvx ruff format --check app tests
```

## Estrutura do projeto

```
.
├── app/
│   ├── main.py       # servidor FastAPI: página do chat, login OAuth (pop-up) e rotas /api/*
│   ├── agent.py      # máquina de estados da conversa: decisão do LLM → próximo passo no iFood
│   ├── guard.py      # regras do checkout do iFood, validadas em toda chamada MCP
│   ├── shopping.py   # busca de ingredientes, comparação de mercados, carrinho, pagamento e cupom
│   ├── recipe.py     # receita, parsing de ingredientes e das respostas da despensa/documento
│   ├── tracking.py   # card de acompanhamento do pedido (status, polling e simulação no DRY_RUN)
│   ├── views.py      # payloads dos cards da interface (mercados, resumo, botões de escolha)
│   ├── user.py       # perfil do getUser + hábitos do endereço, com lista de campos permitidos
│   ├── prompts.py    # prompts do sistema e schemas JSON das respostas do LLM
│   ├── llm.py        # cliente OpenRouter com saída JSON e modelos de reserva
│   ├── ifood.py      # cliente MCP do iFood e remoção de dados pessoais dos logs
│   ├── auth.py       # OAuth 2.1 + PKCE (troca e renovação de tokens)
│   ├── jsonutil.py   # leitura tolerante das respostas MCP e formatação em reais
│   ├── config.py     # configurações lidas do .env
│   └── static/       # interface do chat (HTML, CSS e JS puros)
├── tests/            # testes de ponta a ponta com iFood falso e LLM roteirizado
├── .env.example      # modelo das variáveis de ambiente
└── pyproject.toml    # dependências e configuração do pytest e do Ruff
```

## Limitações conhecidas

- **O login é pelo navegador, não por QR code.** O `id.ifood.com.br` só aceita os fluxos
  `authorization_code` e `refresh_token`, e o client recusa redirecionamentos que não sejam
  loopback.
- **Nenhum escopo é pedido**, então, na conta de teste, o `getUser` só devolve o nível BASIC. A
  pré-seleção de pagamento e as sugestões por comportamento seguem os formatos documentados, mas
  só foram testadas offline.
- **Os formatos de resposta de carrinho, pagamento, cupom e pedido não são documentados.** Por isso
  são lidos de forma tolerante (`app/jsonutil.py`). Toda chamada e resposta MCP aparece no log do
  servidor, já sem dados pessoais.
- **Cartões não salvos não são oferecidos**: o `setCartPayment` exige um `cardTokenId`.
- **Itens vendidos por peso** (ex.: "Tomate Italiano Kg.") não trazem a unidade. O agente assume
  preço por kg e arredonda para cima.
- **Não dá para cadastrar endereço pelo chat.** Sem endereço salvo, a pessoa precisa cadastrar um
  no app do iFood.
- **As sessões ficam em memória**: recarregar a página mantém a conversa, mas reiniciar o servidor
  apaga tudo. Elas também não expiram: cada navegador novo cria uma sessão que só some quando o
  servidor reinicia.
- **Os itens entram no carrinho com `options: []`**, sem consultar o `getItem` antes.
