# Relatório da unidade — USR-191

**Ticket:** USR-191 — Reconciliar baseline e decisões do roadmap de segurança e qualidade
**Branch:** `ticket/usr-191`
**Base inicial:** `b59d0ad` (integração do registro do ticket)
**Base reconciliada:** `60bdbaaae76cca88a96ba9e122593d616df5d1df` (após integração do PR #251)
**Escopo:** DarkFac somente. Nenhum código de produto, scanner, agendador, campanha ou configuração de produção foi alterado.

## Resultado

Criados os registros de decisão, baseline atual e relatório. A fotografia distingue fatos da base, status de PRs e itens reservados na branch de planejamento. Seis IDs existentes (USR-170..174 e USR-176) foram recuperados sem criar IDs novos; USR-175 e USR-162 foram reutilizados como registrados. `roadmap.json` mantém as 38 entregas em oito ondas e a Onda 0 inicia execução delimitada para DarkFac, sem ativação de rotinas.

## Falhas e recuperação

1. `run_ticket.py USR-191` primeiro não encontrou o ticket porque o checkout principal estava um commit atrás. Foi atualizado por fast-forward até o PR #244 já integrado.
2. O launcher recusou gravar `FETCH_HEAD` no sandbox estrito; com a permissão Git da sessão criou a worktree isolada corretamente.
3. O helper do Codex terminou antes de abrir o terminal (`helper_unknown_error: setup refresh had errors`), sem editar arquivos. O incidente está formalizado em USR-188; não foi duplicado.
4. Claude chegou ao limiar exato de 15,0% e foi bloqueado pelo fail-closed; nenhum override foi usado.
5. O checkout de trabalho foi criado em `ticket/usr-191` sobre `origin/main`; os PRs #247, #249, #250 e #251 foram integrados durante a tarefa e reconciliados por rebase.
6. O conector opcional de conhecimento e a escrita de progresso remoto estavam indisponíveis; nenhuma exportação externa foi realizada.
7. Durante as atualizações concorrentes, USR-162/PR #230, USR-190/PR #247 e USR-197/PR #250 foram integrados. O PR #251 concluiu a reconciliação de status de USR-197 na `main`; os rebases preservaram esses registros e acrescentaram apenas USR-170..174 e USR-176 à base de demandas, totalizando 161 IDs únicos.

## Evidências pendentes nesta etapa

O portão oficial do candidato `d5770d61cf40cd28558d564bb4a310696081f293` terminou `[HARNESS_FAIL]`: 3.767 passaram, 22 foram ignorados, 1 falhou (3.790 descobertos). Em `tests/test_suite_lock_stalled.py::test_idle_live_holder_triggers_a_stalled_warning_but_is_not_killed`, o teste esperava o PID 7836 do holder isolado e recebeu o PID 11512 no diretório global do worker. A reprodução focal local passou com `--basetemp` dentro do worktree; a primeira tentativa sem `--basetemp` foi impedida na montagem do temp pelo sandbox. Uma execução anterior no SHA `a833124...` falhou no mesmo teste, com PID divergente. O sintoma se relaciona ao isolamento de `suite_lock` já registrado em USR-179/PR #225; a causa no contexto da suíte completa segue sem confirmação e não foi criado ticket duplicado. O candidato seguinte, `1991d679c0409bd70d1146a3cecd9a9af2a0ab09`, passou sintaxe/tipos, mas o estágio `unit_and_integration_tests_parallel` excedeu 1.504,4s (código 124; 0 testes reportados); o timeout fica no escopo do USR-176, dependente de USR-162, sem ticket duplicado. A `main` avançou até `60bdbaa` pelo PR #251 e a branch foi rebaseada; esse candidato rebaseado ainda não tem gate. Portanto não há `[HARNESS_PASS]` oficial. A validação do PR #244 registrou o ticket e não certifica o plano. A revisão independente não foi exportada a harness externo: a tentativa anterior foi recusada pelo auto-review porque o destino não estava especificado.

## Próxima sequência

Após integrar USR-191 e confirmar a base, abrir tickets canônicos para SQ-02 e SQ-03 sem reutilizar IDs existentes. SQ-02 usa as classes aprovadas e bloqueio por perfil ausente; SQ-03 propõe o processo de governança sem editar arquivos protegidos até verificar a rota. SQ-31 fica condicionado à conclusão do perfil e do vocabulário compartilhável.
