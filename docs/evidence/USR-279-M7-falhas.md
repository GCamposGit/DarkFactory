# USR-279 M7 - Inventario de falhas e lacunas de M1-M6

Inventario atualizado em 2026-10-22 a partir do plano, das evidencias M1/M2, da fila de demandas e do backlog de acoes do owner. O inventario registra lacunas ainda abertas; M1 e M2 permanecem confirmados apenas nos limites descritos em suas evidencias. Nenhum marco M3-M6 e declarado concluido aqui.

| Marco | Falha ou lacuna | Evidencia | Vinculo de resolucao |
| --- | --- | --- | --- |
| M1 / CI | O workflow proprio do produto esta presente, mas nao ha evidencia de execucao verde do GitHub Actions associada ao SHA do produto; essa verificacao pertence ao marco M4. | `docs/evidence/USR-279-M1.md`, secao Pendencias, registra CI nao verificada; `docs/plans/USR-279-greenfield-pilot.md`, M4 criterio 2 exige CI verde por SHA. | `USR-279` permanece planned e inclui CI/merge como criterio; validar a execucao para o SHA antes de confirmar M4. |
| M2 / M6 | A wheel foi gerada no checkout local durante M2, mas nao ha evidencia de build da wheel em clone limpo fora do checkout DarkFac, como exige M6. | `docs/evidence/USR-279-M2.md`, criterio 2, registra wheel gerada em `C:\dev\pilot-hello`; `docs/plans/USR-279-greenfield-pilot.md`, M6 criterio 1 exige clone novo com build. | `USR-279` permanece planned; executar e registrar o build no clone como parte da prova M6. |
| M3 | A demanda do piloto ainda nao foi enviada pelo Telegram; OA-047 registra essa intervencao e depende da liberacao do deploy prevista em OA-043. | `docs/plans/USR-279-greenfield-pilot.md`, M3; `.factory/owner_actions/owner_actions.json`, OA-047 (open, bloqueada por OA-043). | OA-047 e OA-043; USR-279 continua planned ate intake e registro do ticket do piloto. |
| M4 | Nao ha implementacao, PR, CI verde ou merge do produto documentados. M4 depende da demanda registrada em M3. | `docs/plans/USR-279-greenfield-pilot.md`, M4; `docs/evidence/USR-279-M1.md`, Pendencias, confirma que CI nao foi verificada. | `USR-279` permanece planned; fechar M3 e registrar SHAs, PR, CI e merge de M4. |
| M5 | Deploy e smoke do produto nao foram executados/documentados; a liberacao de projeto, acesso e webhook esta pendente em OA-043. | `.factory/owner_actions/owner_actions.json`, OA-043 (open, bloqueia USR-279); `docs/plans/USR-279-greenfield-pilot.md`, M5. | OA-043 e USR-279; registrar deploy e smoke por SHA depois de liberados os acessos. |
| M6 | A prova completa de independencia esta pendente. A evidência M1 registra clone, testes e smoke iniciais, mas M6 requer prova de clone novo com install, testes, build e smoke sob ambiente sem dependencia do core; a build da wheel no clone ainda nao esta provada. | `docs/evidence/USR-279-M1.md`, secao Criterio 3; `docs/evidence/USR-279-M2.md`, criterio 2 registra wheel apenas no checkout; `docs/plans/USR-279-greenfield-pilot.md`, M6. | `USR-279` permanece planned; concluir e anexar a evidencia completa de clone independente apos M3-M5. |

## Estado dos vinculos

- `USR-279` existe em `.factory/demands/demands.json` com status `planned` e conserva o escopo pendente do piloto.
- OA-043 e OA-047 existem em `.factory/owner_actions/owner_actions.json` e estao abertas; OA-047 depende de OA-043.
- A base de M1/M2 esta nos PRs #490 e #492, respectivamente, associados aos commits `5eb0cb15c4f3d0703c5bf3feddf725b9f72cfc44` e `e5c2ef15ba0641820f05a0687146918b3f9ff95a`.

Este inventario nao executa M3, M4, M5 ou M6 e nao transforma evidencias parciais em criterios atendidos.
