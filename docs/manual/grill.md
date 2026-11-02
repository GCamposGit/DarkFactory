<!-- covers: core/line/stage_grill.py, core/demands/grill.py -->
# Grill: o que a fabrica pergunta a voce

O grill so faz perguntas que apenas voce (owner) pode responder. Decisoes tecnicas a fabrica toma sozinha e registra como ADR curto no workspace da execucao (`GRILL.md`, secao Premissas).

## Categorias permitidas (conjunto fechado)

| Codigo | Pergunta tipica |
|---|---|
| `objective` | Qual e o objetivo, em uma frase? |
| `success` | Como saberemos que deu certo? (resultado que voce confere) |
| `business` | Regra de negocio que muda o resultado (preco, prazo, limite) |
| `new_secret` | Preciso de um segredo/conta nova que so voce tem |
| `acceptance` | Quem aprova o final: `auto` (testes passam) ou `owner` (voce confirma) |

Qualquer outra pergunta (arquitetura, biblioteca, teste, resiliencia) e descartada e vira ADR.

## Regras

- A sessao conserva todas as perguntas materiais, sem reclassificar perguntas excedentes como tecnicas.
- Cada rodada mostra no maximo 3 perguntas. Respostas parciais registram apenas escolhas explicitas e avançam para a proxima pagina ainda nao enviada; decisoes sem resposta continuam persistidas e bloqueantes, e os botoes ja enviados continuam validos.
- A opcao `recommended` e uma sugestao. Silencio, resposta em branco e o antigo `auto_accept_unanswered` nao escolhem pelo owner; a demanda segue em `waiting_human` ate todas as decisoes materiais terem resposta explicita.
- O prazo de 12 horas serve para acompanhamento e notificacao. Vencimento, silencio ou falha de entrega nao escolhem respostas, nao limpam `GRILL_PENDING.json` e nao liberam o planejamento.
- Perguntas tecnicas reversiveis continuam sob decisao automatica da fabrica, registradas com origem `auto_technical`, e nao entram na fila de perguntas ao owner.
- A origem `auto_canary` e permitida somente quando a execucao traz explicitamente `grill_policy=auto`. Segredos e novas contas continuam bloqueantes mesmo nessa politica.
- Correcoes e ajustes pequenos ("corrigir o erro de digitacao no menu") nao geram perguntas.
- Uma demanda vaga como "Quero um painel que mostre minhas vendas do mes" so segue depois de objetivo, criterio de sucesso verificavel e modo de aceite (`auto` ou `owner`) explicitamente definidos.

## Formato móvel no Telegram (USR-438)

- **Corpo da mensagem**: o texto integral de cada alternativa e exibido em destaque no corpo da mensagem, com indicacao clara da recomendada (`⭐ (Recomendada)`), garantindo que nada fique truncado.
- **Botoes de selecao**: exibem apenas o numero da opcao e um resumo conciso de 2 a 3 palavras (ex.: `1. Usar o Grok`), otimizados para telas de celular sem quebra de layout.
- **Botao recomendado one-tap**: formato `⭐ Opção X: <resumo>` para aceite imediato com um toque.
- **Links publicos funcionais**: todos os links apontam exclusivamente para endpoints publicos acessiveis externamente (`https://darkhub.ggcampos.com`), eliminando qualquer referencia a `127.0.0.1` ou `localhost`.
