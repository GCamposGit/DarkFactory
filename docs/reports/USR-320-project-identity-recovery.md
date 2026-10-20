# USR-320: identidade de projeto e recuperacao do run

## Defeito reproduzivel

O dispatch lia a identidade por `get_run_status`, uma consulta que tambem decodifica
telemetria de todos os jobs. Falhas eram convertidas em `None`; o handler entao usava
`JobKey.ticket_id` como projeto. Assim, um erro de leitura podia produzir
`UnknownProjectError` para `USR-315`, embora `runs.project_id` fosse `darkfac`.
O contexto tambem era construido antes do bloco que registra o resultado do job:
uma excecao ali deixava o job em execucao ate expirar o lease.

SQLite e Postgres agora oferecem uma leitura pequena da identidade persistida,
independente da telemetria. O dispatch exige essa identidade e nunca deduz projeto
pelo ticket. Falhas de infraestrutura geram `project_lookup_unavailable` e retry;
run ausente, projeto ausente e projeto nao registrado tem causas distintas.
A construcao do contexto participa do mesmo finish/materialize e heartbeat do handler.

## Evidencia operacional de 20/10/2026

- Consulta do store confirmou `project_id=darkfac` no run `run-5c1eeae3564c`.
  O SHA efetivamente carregado pelo worker do incidente nao foi recuperado;
  isso limita a atribuicao da causa operacional. A correcao cobre o caminho
  reproduzivel acima, sem afirmar que o HEAD do checkout prova a versao do processo.
- USR-315 ja foi entregue: PR 503 mergeado em
  `7433f3df16bd277c1838829d885d54caa2046c41`, ancestral de `origin/main`;
  ledger `completed` com evidencia e OA-040 `done` foram conferidos.
- O endpoint canonico `/api/line/runs/run-5c1eeae3564c/cancel` reconciliou o run
  obsoleto com motivo `superseded_by_verified_delivery_pr503`. Resultado: um job
  pendente cancelado; 11 tentativas de planning preservadas, nenhuma tentativa de
  development ou integration. Nenhuma etapa produtiva foi declarada bem-sucedida.
- O mesmo endpoint reservou o trabalho de USR-320 para a sessao Codex; o ticket
  foi marcado `implementing` antes de encerrar a readmissao, evitando duplicacao.
- O run M7 `run-2c333b2c26f8` foi preservado. O branch remoto continha trabalho
  real em `8656286`; depois o mecanismo existente mudou o job para pending com
  `LEASE_EXPIRED` e reservou Grokbox. Nao houve cancelamento nem segunda implementacao.

## Validacao

Regressoes cobrem accept/claim/dispatch de planning e sucessores, duas entradas
reais do registro (`darkfac` e `pilot-hello`) com tickets USR, ausencia de contexto,
indisponibilidade, run ausente, paridade SQLite/Postgres mock e erro Postgres sem
vazar detalhes privados. O teste com Postgres descartavel e opcional e nao e
apresentado como prova de execucao em producao.

Os logs e recibos operacionais desta sessao ficam em
`C:\dev\DarkFac\.factory\test_logs\usr320`; o handover registra os resultados do
portao oficial, revisao independente, PR e deploy quando observados.
