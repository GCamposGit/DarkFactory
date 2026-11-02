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
- Cada rodada mostra no maximo 3 perguntas ainda sem resposta. Respostas parciais registram apenas as escolhas explicitas e liberam a proxima rodada.
- A opcao `recommended` e uma sugestao. Silencio, resposta em branco e o antigo `auto_accept_unanswered` nao escolhem pelo owner; a demanda segue em `waiting_human` ate todas as decisoes materiais terem resposta explicita.
- Perguntas tecnicas reversiveis continuam sob decisao automatica da fabrica e nao entram na fila de perguntas ao owner.
- Correcoes e ajustes pequenos ("corrigir o erro de digitacao no menu") nao geram perguntas.
- Uma demanda vaga como "Quero um painel que mostre minhas vendas do mes" so segue depois de objetivo, criterio de sucesso verificavel e modo de aceite (`auto` ou `owner`) explicitamente definidos.

## Formato móvel no Telegram (USR-438)

- **Corpo da mensagem**: o texto integral de cada alternativa e exibido em destaque no corpo da mensagem, com indicacao clara da recomendada (`⭐ (Recomendada)`), garantindo que nada fique truncado.
- **Botoes de selecao**: exibem apenas o numero da opcao e um resumo conciso de 2 a 3 palavras (ex.: `1. Usar o Grok`), otimizados para telas de celular sem quebra de layout.
- **Botao recomendado one-tap**: formato `⭐ Opção X: <resumo>` para aceite imediato com um toque.
- **Links publicos funcionais**: todos os links apontam exclusivamente para endpoints publicos acessiveis externamente (`https://darkhub.ggcampos.com`), eliminando qualquer referencia a `127.0.0.1` ou `localhost`.
