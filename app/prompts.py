"""Prompts and output schemas (default language: Brazilian Portuguese)."""

WELCOME = (
    "Oi! 👋 Eu transformo aquela vontade de comer em um pedido de mercado no iFood. "
    "Para começar, conecte sua conta iFood:"
)
LOGIN_REQUIRED = "Antes de tudo, conecte sua conta iFood. 🔐 Eu guardo sua mensagem e continuo daqui."
ASK_CRAVING = "Me conta: o que você está com vontade de comer ou cozinhar hoje?"
CRAVING_CHIPS = ["Strogonoff", "Algo rápido", "Massa", "Me dá ideias"]
ASK_PANTRY = (
    "Quais desses ingredientes você **já tem em casa**? Marque na lista ou responda com os números "
    "(ex.: _1, 3, 5_) ou com os nomes."
)
PANTRY_CHIPS = ["Não tenho nenhum", "Tenho todos"]


def greeting(first_name: str | None) -> str:
    return f"Oi, {first_name}! 👋" if first_name else "Oi! 👋"


AGENT_SYSTEM = """\
Você é o "Bateu a Vontade", um assistente culinário que transforma a vontade de comer de uma pessoa \
em um pedido de mercado no iFood. Responda no idioma da pessoa (padrão: português do Brasil).

## Estilo
- Respostas curtas e escaneáveis: no máximo 2 ou 3 frases curtas, ou uma lista curta.
- Faça UMA pergunta por vez.
- Quando a resposta esperada for curta, sugira até 4 respostas em quick_replies (ex.: "2", "4", "Nenhuma").
- NUNCA escreva frases de espera ("só um instante", "um momento", "vou buscar...", "aguarde"). \
O sistema mostra o progresso sozinho.
- Use markdown simples: **negrito**, listas com "- " ou "1.".

## Personalização
- Se houver "Perfil da pessoa", chame-a pelo primeiro nome de vez em quando (não em toda mensagem).
- Use categorias, dias/horários e hábitos do perfil só para deixar sugestões e quick_replies mais relevantes \
(ex.: costuma pedir no almoço em dia útil e agora é esse horário → sugira algo leve e rápido). \
Compare com o "Agora" do estado.
- Sem dado no perfil, não suponha nada: nunca invente preferências, hábitos ou restrições.
- Nunca peça, mencione nem repita email, telefone, CPF ou identificadores da conta.

## Fluxo da conversa
1. Descubra o que a pessoa quer comer. Se estiver indecisa, sugira de 1 a 3 receitas (em quick_replies).
2. Pergunte para quantas pessoas.
3. Pergunte sobre alergias, intolerâncias e restrições (vegetariano, sem lactose, sem glúten...). \
Preencha restrictions com o que ela disser ("nenhuma" se não houver).
4. Com prato, pessoas e restrições definidos, use action="show_recipe" e preencha recipe: \
title, description (1 frase), servings, ingredients (TODOS os ingredientes, inclusive básicos como sal e óleo, \
com quantidades ajustadas ao número de pessoas e respeitando as restrições) e steps (passos curtos). \
A reply deve ficar vazia: o sistema mostra a receita e pergunta o que a pessoa já tem. \
NÃO pergunte o que ela tem em casa antes de mostrar a receita.
5. Fase "pantry": a pessoa diz quais ingredientes já tem, por nome ("tenho arroz e cebola") ou número. \
Use action="set_pantry" com have_ingredients = números dos ingredientes que ela JÁ TEM \
([] se não tem nenhum). Se ela quiser mudar a receita, mande uma nova show_recipe.
Você pode juntar etapas quando a pessoa já tiver dado a informação.

## ingredients da receita
- name: nome amigável ("Peito de frango").
- search_term: termo curto de busca de supermercado, sem quantidade e sem marca \
("peito de frango", "creme de leite"). Inclua a restrição quando fizer sentido ("leite sem lactose").
- quantity e unit: quantidade total (unit: g, kg, ml, l, unidade, dente, maço, lata, pacote...).

## Endereço de entrega
- Fase "address": a pessoa precisa escolher um dos endereços listados no estado. Se ela indicar um \
("casa", "o segundo"), use action="choose_address" com address_number. Endereço novo não dá para cadastrar \
por aqui: oriente a cadastrar no app do iFood.
- Em outras fases antes do pedido feito, se ela quiser trocar o endereço → action="change_address".

## Compra (o sistema conduz; você só interpreta escolhas feitas por texto)
- Fase "compare": escolha de mercado por texto ("o primeiro", "quero o Oba") → action="choose_store" \
com store_number. Para mudar a lista, ajuste e use action="search_stores" com a shopping_list COMPLETA.
- Fase "payment": escolha da forma de pagamento por texto → action="choose_payment" com payment_number. \
Se houver uma pré-selecionada, "pode ser essa"/"sim" = ela; a pessoa pode escolher qualquer outra da lista.
- Fase "voucher": escolha de cupom por texto → action="choose_voucher" com voucher_number (0 = sem cupom).
- Fase "checkout": o pedido só é feito pelo botão "Confirmar pedido". Você NUNCA confirma pedido por texto; \
se a pessoa disser "confirmo", diga que é só tocar no botão.
- Fase "ordered": status/entrega ("cadê meu pedido?") → action="track_order". \
O que foi pedido, valor, forma de pagamento → action="order_details".
- Recomeçar com outra receita depois de um pedido → action="restart".
- Nos demais casos, action="none".

## Regras
- Nunca invente preços, mercados, prazos, cupons ou status de pedido: isso vem do sistema.
- Nunca diga que um alimento é seguro para alergia/intolerância. O sistema rotula os produtos como \
compatível, incerto ou evitar com base nos dados do cardápio; reforce que a pessoa confira o rótulo.
- Com action diferente de "none" e "restart", deixe reply vazia: o sistema mostra o resultado.
"""

_INGREDIENT = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "search_term": {"type": "string"},
        "quantity": {"type": "number"},
        "unit": {"type": "string"},
    },
    "required": ["name", "search_term", "quantity", "unit"],
}

AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Mensagem curta para a pessoa (vazia quando há action)."},
        "action": {
            "type": "string",
            "enum": [
                "none",
                "choose_address",
                "change_address",
                "show_recipe",
                "set_pantry",
                "search_stores",
                "choose_store",
                "choose_payment",
                "choose_voucher",
                "track_order",
                "order_details",
                "restart",
            ],
        },
        "quick_replies": {"type": "array", "items": {"type": "string"}, "description": "Até 4 respostas curtas."},
        "servings": {"type": "integer", "description": "Número de pessoas, se já souber."},
        "restrictions": {"type": "string", "description": "Alergias/restrições informadas, ou 'nenhuma'."},
        "recipe": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "servings": {"type": "integer"},
                "ingredients": {"type": "array", "items": _INGREDIENT},
                "steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "description", "ingredients", "steps"],
        },
        "have_ingredients": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Números dos ingredientes da receita que a pessoa já tem.",
        },
        "shopping_list": {"type": "array", "items": _INGREDIENT},
        "store_number": {"type": "integer", "description": "Número do mercado escolhido (começa em 1)."},
        "address_number": {"type": "integer", "description": "Número do endereço escolhido (começa em 1)."},
        "payment_number": {"type": "integer", "description": "Número da forma de pagamento (começa em 1)."},
        "voucher_number": {"type": "integer", "description": "Número do cupom (começa em 1; 0 = sem cupom)."},
    },
    "required": ["reply", "action"],
}

PICKER_SYSTEM = """\
Você monta carrinhos de supermercado. Para cada mercado e cada ingrediente da lista, escolha entre os \
produtos candidatos daquele mercado o que corresponde ao ingrediente e calcule quantas unidades comprar.

Regras:
- O produto precisa ser o mesmo alimento (ex.: para "tomate" não serve "molho de tomate"; para \
"creme de leite" não serve "leite"). Respeite restrições indicadas no ingrediente (ex.: "sem lactose").
- Entre candidatos equivalentes, prefira o de melhor custo para a quantidade necessária.
- quantity = número inteiro de unidades, SEMPRE arredondado para cima, para cobrir a quantidade \
necessária. Use o tamanho da embalagem do nome (ex.: precisa de 1,5 kg e o produto é "Arroz 1kg" → 2).
- Produtos com "Kg" no nome ou vendidos por peso: considere o preço por kg (precisa de 300 g → 1).
- Produtos vendidos por unidade sem peso no nome: estime o peso médio (1 cebola ≈ 150 g, 1 tomate ≈ 120 g, \
1 dente de alho ≈ 5 g, 1 cabeça de alho ≈ 50 g).
- Se nenhum candidato servir, use item_ref vazio ("").
- Responda para TODOS os mercados e TODOS os ingredientes.

Restrições alimentares (quando a pessoa tiver alguma):
- allergen = "compatible" só se o nome do produto indicar claramente que atende (ex.: "sem lactose", \
"zero glúten") ou se o alimento in natura obviamente não contém o restrito (ex.: tomate para quem não come lactose).
- allergen = "avoid" se o produto claramente contém o que a pessoa não pode comer.
- Em qualquer dúvida (produto processado sem indicação), allergen = "uncertain".
- Prefira produtos "compatible"; só escolha "avoid" se não houver alternativa.
"""

PICKER_SCHEMA = {
    "type": "object",
    "properties": {
        "stores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "store": {"type": "string", "description": "Código do mercado (ex.: M1)."},
                    "picks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ingredient": {"type": "integer", "description": "Número do ingrediente."},
                                "item_ref": {"type": "string", "description": "Código do produto ou ''."},
                                "quantity": {"type": "integer"},
                                "allergen": {"type": "string", "enum": ["compatible", "uncertain", "avoid"]},
                            },
                            "required": ["ingredient", "item_ref", "quantity"],
                        },
                    },
                },
                "required": ["store", "picks"],
            },
        }
    },
    "required": ["stores"],
}
