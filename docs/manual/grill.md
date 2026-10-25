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

- No maximo 3 perguntas por demanda.
- Toda pergunta traz uma opcao `recommended`; se voce nao responder, ela vale.
- Correcoes e ajustes pequenos ("corrigir o erro de digitacao no menu") nao geram perguntas.
- Uma demanda vaga como "Quero um painel que mostre minhas vendas do mes" so segue depois de objetivo, criterio de sucesso verificavel e modo de aceite (`auto` ou `owner`) definidos.
