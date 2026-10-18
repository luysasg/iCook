# Racional da Experiência — iCook

Princípio geral: **conversa para o que é aberto, botão para o que é decisão.** O texto livre serve
para descobrir a vontade; escolhas com opções fechadas (endereço, mercado, pagamento, cupom,
confirmação) viram cards clicáveis, mas também aceitam texto ("o primeiro", "quero o Oba").

## Decisões

| Decisão | Por quê |
|---|---|
| **Uma pergunta por vez, respostas curtas** | Chat no celular: blocos longos não são lidos. Cada mensagem tem um único próximo passo claro. |
| **Chips de resposta rápida** | Respostas curtas ("2", "Nenhuma", "Tenho todos") com um toque. As sugestões de prato usam o horário e os hábitos do perfil, para quem chega sem ideia. |
| **Restrições antes da receita e de qualquer busca** | Uma receita ou um carrinho com alérgeno é um erro caro; perguntar depois obrigaria a refazer tudo. |
| **Receita antes da pergunta "o que você já tem?"** | Ninguém responde o que tem em casa sem saber o que a receita pede. Os ingredientes numerados permitem responder "1, 3, 5". |
| **Rótulos compatível / incerto / evitar** | Nunca afirmamos que um produto é seguro: na dúvida é "incerto" e o agente reforça conferir o rótulo. Honestidade acima de conveniência. |
| **Comparação lado a lado de mercados** | A pessoa decide com os dados que importam: o que falta em cada um, total com entrega, prazo e nota. O agente ordena, mas não escolhe por ela. |
| **Aviso de pedido mínimo antes do carrinho** | Evita a frustração de montar tudo e travar no checkout. Oferece saída: adicionar itens ou trocar de mercado. |
| **Pagamento salvo pré-selecionado, mas confirmado** | Reduz atrito sem decidir pela pessoa. |
| **Resumo com valores do carrinho real** | O total exibido é o que o iFood vai cobrar, não uma estimativa do agente. |
| **Pedido só pelo botão Confirmar** | Comprar envolve dinheiro: a confirmação é um gesto explícito, nunca um "sim" interpretado pelo LLM. Se a pessoa escrever "confirmo", o agente indica o botão. |
| **Status do que está acontecendo** ("Buscando ingredientes nos mercados…") | A busca leva alguns segundos; mostrar o passo atual evita a sensação de travamento. O LLM é proibido de escrever frases de espera vazias. |
| **Login sem perder o contexto** | Se a pessoa escreve antes de conectar, ou se a sessão expira, a mensagem/ação é guardada e retomada após o login. |
| **Privacidade visível** | O CPF digitado aparece mascarado e não vai para o histórico do LLM. Dados pessoais nunca chegam ao LLM nem aos logs. |
| **Erros com saída** | Toda falha diz o que fazer a seguir ("quer tentar outro mercado?"). No erro de envio do pedido, orienta conferir no app antes de tentar de novo, evitando pedido duplicado. |
| **Português do Brasil, tom leve** | Público do iFood; emojis pontuais para dar leveza sem poluir. |
