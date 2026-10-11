
## Congelamento V0 (vigente desde 11/10/2026, todos os harnesses)

- Só se implementam tickets com a tag `v0-critico` no `.factory/demands/demands.json`. O resto está com a tag `congelado-v0` e `horizon: later`. Plano, critério de saída e ordem: `docs/plans/V0_OPERACIONAL_2026-10-11.md`.
- Pedido novo fora do V0 entra como `congelado-v0`, salvo se o owner disser explicitamente que quer furar o congelamento.
- A regra "Defeito Encontrado = Correção Imediata ou Ticket Formal" continua valendo, com triagem: se o defeito impede uma condição do V0, o ticket recebe `v0-critico`; se não impede, é registrado com `horizon: later` e a tag `congelado-v0`.
- Uma sessão opera a linha por vez. Tickets com no máximo 3 critérios de aceite; ticket maior é fatiado antes de rodar.
