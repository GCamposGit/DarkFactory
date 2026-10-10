# Relatório da unidade — USR-191

**Ticket:** USR-191 — Reconciliar baseline e decisões do roadmap de segurança e qualidade
**Branch:** `ticket/usr-191`
**Base inicial:** `b59d0ad` (integração do registro do ticket)
**Base reconciliada:** `85795d8` (após integração dos PRs #267–#279 e do ajuste de timeout USR-135)
**Escopo:** DarkFac somente. Nenhum código de produto, scanner, agendador, campanha ou configuração de produção foi alterado.

## Resultado

Criados os registros de decisão, baseline atual e relatório. A fotografia distingue fatos da base, status de PRs e itens reservados na branch de planejamento. Seis IDs existentes (USR-170..174 e USR-176) foram recuperados sem criar IDs novos; USR-175 e USR-162 foram reutilizados como registrados. Após reconciliar os registros dos workers, a candidata contém 184 IDs únicos; `origin/main` contém 178. Os seis IDs reservados continuam ausentes de `origin/main` e só ficam visíveis a todos os workers quando esta candidata for integrada. O PR #279 atualizou a ação OA-011 e não alterou a fila de demandas. `roadmap.json` mantém as 38 entregas em oito ondas e a Onda 0 inicia execução delimitada para DarkFac, sem ativação de rotinas.

## Falhas e recuperação

1. `run_ticket.py USR-191` primeiro não encontrou o ticket porque o checkout principal estava um commit atrás. Foi atualizado por fast-forward até o PR #244 já integrado.
2. O launcher recusou gravar `FETCH_HEAD` no sandbox estrito; com a permissão Git da sessão criou a worktree isolada corretamente.
3. O helper do Codex terminou antes de abrir o terminal (`helper_unknown_error: setup refresh had errors`), sem editar arquivos. O incidente está formalizado em USR-188; não foi duplicado.
4. Claude chegou ao limiar exato de 15,0% e foi bloqueado pelo fail-closed; nenhum override foi usado.
5. O checkout de trabalho foi criado em `ticket/usr-191` sobre `origin/main`; os PRs #247, #249, #250 e #251 foram integrados durante a tarefa e reconciliados por rebase.
6. O conector opcional de conhecimento e a escrita de progresso remoto estavam indisponíveis; nenhuma exportação externa foi realizada.
7. Durante as atualizações concorrentes, foram integrados USR-162/PR #230, USR-190/PR #247, USR-197/PRs #250/#251, OA-003/PR #254, diagnóstico USR-188/PR #255, registro USR-198/PR #256, registro USR-200/PR #258, diagnóstico USR-187/PR #259 e registros recentes #267–#279. A fotografia foi rebased para `eb37442` e depois atualizada para registrar #279; os seis IDs reservados foram preservados sem colisões.

## Evidências pendentes nesta etapa

O portão oficial do candidato `d5770d61cf40cd28558d564bb4a310696081f293` terminou `[HARNESS_FAIL]`: 3.767 passaram, 22 foram ignorados, 1 falhou (3.790 descobertos). Em `tests/test_suite_lock_stalled.py::test_idle_live_holder_triggers_a_stalled_warning_but_is_not_killed`, o teste esperava o PID 7836 do holder isolado e recebeu o PID 11512 no diretório global do worker. A reprodução focal local passou com `--basetemp` dentro do worktree; a primeira tentativa sem `--basetemp` foi impedida na montagem do temp pelo sandbox. Uma execução anterior no SHA `a833124...` falhou no mesmo teste. O diagnóstico do USR-187/PR #259 identificou divergência entre o PID de `Popen` e `os.getpid()` do holder no launcher Windows; ainda não há correção integrada nem PASS remoto. O sintoma permanece vinculado ao isolamento de `suite_lock`/PR #225.

O candidato `1991d679c0409bd70d1146a3cecd9a9af2a0ab09` passou sintaxe/tipos, mas `unit_and_integration_tests_parallel` excedeu 1.504,4 s (código 124; 0 testes reportados). No candidato documental `adfd5fb9cc931960bcffe2810603a35ba30801ac`, sobre a base `a236473`, o mesmo estágio excedeu 1.502,8 s (código 124; `count=0`). Na execução mais recente, candidato `6f59bb59190170418632c15ec184d0e64c1a1a39` sobre `eb37442`, o worker remoto estava indisponível; o fallback local excedeu 3.007,1 s (código 124; `count=0`). O registro USR-200/PR #258 executará a investigação reservada em USR-176, sem duplicar a perda remota já integrada em USR-162.

Na branch de implementação USR-188, a correção candidata desativa o servidor MCP `node_repl` apenas na chamada headless, sem alterar a configuração global nem afrouxar o sandbox. O teste focal passou (38 casos passaram no par de arquivos; 6 erros de setup ocorreram porque o sandbox bloqueou a criação de repositórios Git temporários) e uma chamada Codex headless read-only respondeu corretamente. O gate oficial no SHA `6f59bb59190170418632c15ec184d0e64c1a1a39` passou sintaxe/tipos, mas o worker remoto ficou indisponível e o fallback local excedeu 3.007,1 s (código 124; `count=0`). O resultado permanece `[HARNESS_FAIL]`; não há PASS oficial nem PR de implementação aberto para USR-188/USR-191. O incidente está coberto pelo USR-200. A revisão independente não foi exportada a harness externo; a tentativa anterior foi recusada pelo auto-review porque o destino não estava especificado.

## Próxima sequência

Após integrar USR-191 e confirmar os seis registros reservados na fila compartilhada, iniciar SQ-02 e SQ-03 como tickets DarkFac sem reutilizar IDs existentes. SQ-02 usa as classes aprovadas e bloqueio por perfil ausente; SQ-03 propõe o processo de governança sem editar arquivos protegidos até verificar a rota. SQ-31 fica condicionado à conclusão do perfil e do vocabulário compartilhável.
