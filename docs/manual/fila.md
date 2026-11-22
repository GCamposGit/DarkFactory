<!-- covers: core/git/app_token.py, core/infra/node_sync.py, scripts/sync_usage_to_cloud.py, scripts/snapshot_queue.py, core/owner_actions/*.py, hub/backend/service*.py, hub/backend/api.py, hub/backend/main.py, hub/backend/models.py, hub/frontend/interventions.js, core/orchestrator/adapters/control_postgres.py, core/ops/briefing.py, deploy/dokploy/docker-compose.hub.yml, .env.example, core/demands/queue_store.py, core/demands/store.py -->
# Fila de tickets (USR-343)

A fila de desenvolvimento vive no control store: Postgres quando `DARKFAC_HF02_DATABASE_URL`
está definida, senão o SQLite local `.factory/control.db`. Tabelas (criadas com
`CREATE TABLE IF NOT EXISTS`):

- `tickets (project_id, ticket_id, status, payload, updated_at)`
- `owner_actions (action_id, status, payload, updated_at)`

No Hub em produção, o compose preenche `DARKFAC_HF02_DATABASE_URL` com
`DARKHUB_CONTROL_DATABASE_URL` quando a primeira está vazia: as duas apontam para o mesmo
Postgres. Com `DARKFAC_OWNER_ACTIONS_MODE=control` e nenhum banco configurado, a fila do owner
responde "indisponível" (503) em vez de aparecer vazia.

## Registrar um ticket

```powershell
python C:\dev\DarkFac\run_ticket.py --create --queue-only --title "Titulo" --problem "Problema" --criteria "Criterio"
```

O comando grava o ticket (status `planned`) no control store e termina. Não cria worktree
`ticket/queue-*`, PR nem merge. O Telegram usa o mesmo caminho.

## Leitura

`core.demands.store.DemandsStore` (usado pelo despachante, Hub e `/tickets`) lê o control store
primeiro e complementa com `.factory/demands/demands.json` (fallback e histórico). Em conflito
de ID vale o control store, exceto quando o ledger já marca o ticket como `completed` ou
`cancelled`. Se o control store estiver inacessível, a leitura cai para o `demands.json`.
Com `DARKFAC_DEMANDS_PATH` definida, só o arquivo indicado é lido.

## Resolver para entrega (USR-424)

`queue_store.get_ticket(ticket_id)` devolve o `UserTicket` da control store.
Retorna `None` sem exceção quando o id não existe ou quando o store ainda não
foi criado, e não cria `control.db` nesse caminho.

`DemandsStore.materialize_ticket(ticket_id)` e `resolve_for_delivery(ticket_id)`
copiam esse mesmo id para o `demands.json` da worktree, uma única vez, sem
chamar `next_ticket_id`. A segunda chamada não acrescenta linha. Uma linha
local aberta com o mesmo id é substituída no lugar pela linha da fila. Uma
linha local já `completed` ou `cancelled` permanece no arquivo, sem regravar,
quando a fila ainda não está terminal, mesmo que a linha não tenha
`created_at` nem `updated_at`. A fila aberta continua aberta. No checkout
compartilhado (ledger control-only, USR-415) a materialização não altera
`demands.json` nem o git status. Id ausente nos dois lados devolve `None` e
não cria linha na fila nem no arquivo.

`DemandsStore.save_ticket` no ledger da worktree atualiza a linha
`(project_id, ticket_id)` que já está na fila. Não cria linha para um ticket
que existe só no arquivo, mesmo quando o `demands.json` da worktree é o ledger
padrão daquele checkout (`.git` é arquivo). Com `DARKFAC_DEMANDS_PATH`
definida, a gravação permanece só no arquivo: a fila não é criada nem
atualizada.

`complete_ticket` consulta esse registro antes de exigir o id no `demands.json`
da worktree e sem usar o ledger compartilhado. Ao concluir, a mesma linha
`(project_id, ticket_id)` da fila fica `completed` com `delivery_evidence`.
Id ausente na worktree e na fila devolve `TicketCompletionReport` com
`ok=false` e `cause=ticket_absent`, sem linha nova e sem commit. Ticket que
já está só no ledger local conclui sem criar o control store. Branch sem
mudança fora do ledger continua recusada com `no_changes`.

`--resume-delivery` usa a mesma resolucao antes de chamar a entrega, nesta
ordem: ledger da worktree, fila do control store (materializa o mesmo id, sem
`next_ticket_id` e sem o agente de correcao), arquivo do checkout compartilhado
(sem gravar a worktree e sem criar linha na fila) e `origin/main` (o rebase
posterior traz a linha). Nao depende do ledger compartilhado. Nao copie o
ledger compartilhado para a worktree so para a entrega passar. Id ausente nos
quatro devolve exit 1, `ok=false`, `cause=ticket_absent` e `error` no formato
`Ticket <ID> not found in demands store`. O stderr cita o ledger da worktree, a
control store, o checkout compartilhado e `origin/main`. O ledger e o
`control.db` permanecem como estavam. Gate e revisao ja validos para o
candidato atual nao sao reexecutados; `merge.json` e o relatorio de deploy do
attempt anterior sao apagados.

## Reentrada na fila (USR-498)

`DemandsStore.requeue_ticket` continua o unico caminho que devolve um ticket a `planned`.
`completed` e `cancelled` seguem recusados quando `reopen` nao e pedido. Com `reopen=True`,
o completed exige motivo, a evidencia anterior e limpa e o motivo entra em
`problem_statement`; `cancelled` nao reabre. O argumento `evidence` e ignorado nesse
caminho.

Na transicao comum, `evidence` e acrescentado ao `delivery_evidence` que ja existia,
numa linha nova. A linha igual nao e repetida. Ticket que ja esta `planned` nao e
regravado e nao ganha outra linha. Branch, worktree e trava de linha nao entram
nesse metodo.

## Limpeza ao encerrar ticket (USR-500)

`DemandsStore.update_status` primeiro persiste a transicao e libera o lock do ledger. Ao
marcar o ticket como `completed` ou `cancelled`, tenta remover sua worktree mais recente
por `core.git.ticket_workspace.cleanup`. Uma worktree com alteracoes, lock, processo em
execucao ou estado de processo desconhecido e preservada; a falha de limpeza nao desfaz
nem bloqueia o status terminal. Tentativas que nao removeram o diretorio ficam registradas
em `.factory/runs/cleanup_pending/` para observabilidade e ficam fora da classificacao de
orfaos pela varredura diaria.

## Reconciliação no ciclo do despachante (USR-498)

No início de cada ciclo, antes de montar a fila (`select_tickets`), o despachante chama `reconcile_stale_active`. O limiar default é 24 horas: ticket `implementing` ou `validating` sem vida além desse prazo volta a `planned` e pode ser despachado no mesmo ciclo. A causa fica no ticket (`delivery_evidence`) e a ação fica em `cycle["reconciled"]`, gravado em `.factory/runs/fleet_dispatcher_state.json`.

`--resume-only` não reconcilia. `--dry-run` do despachante só lista (`would_requeue`) e não grava. Ticket em `in_flight` deste despachante fica de fora. Falha do sweep vira ação `error` (`stale_active_reconcile_failed`) e o ciclo segue.

```powershell
python -m core.line.stale_active --dry-run
```

`--dry-run` lista o que voltaria a `planned` e não grava o ledger. `--max-actions` (padrão 25) e `--idle-hours` (padrão 24) limitam o sweep manual.

## Acoes do owner e status (S4b)

- Escritores de `owner_actions` (respostas e cliques do Hub, espelhos, alertas de CI, CLI)
  gravam no control store (`OwnerActionStore` no ledger padrao). Nao existem mais PRs `chore/oa-*`
  nem `ticket/queue-*`; o PostgreSQL e a unica fonte de leitura e escrita de OAs. `owner_actions.json` e apenas exportacao.
- `DemandsStore.save_ticket` e `update_status` gravam a linha em `tickets` do control store no ledger do checkout compartilhado. No ledger da worktree a mesma chamada só atualiza a linha `(project_id, ticket_id)` já presente na fila. `DARKFAC_DEMANDS_PATH` deixa a gravação só no arquivo.
- Snapshot diario: `python C:\dev\DarkFac\scripts\snapshot_queue.py` (a flag `--force` ignora o
  marcador do dia) escreve `demands.json` e `owner_actions.json` a partir do control store em UM
  commit, publicado em `main` com o token do GitHub App (`core/git/app_token.py`). E idempotente
  (marcador no diretorio de estado; ledger igual nao gera commit) e deve ser agendado separadamente, fora da publicacao de cotas. Le e escreve so numa worktree
  descartavel de `origin/main`: o checkout de quem chama nunca fica sujo (USR-414). O export de OAs reflete somente o control store e nunca importa linhas do Git. A protecao contra perda de ids continua valendo para demandas. O fetch sincroniza a ref `origin/main` antes do checkout da worktree e `next-id` respeita os ids gravados em todo o historico git para evitar reutilizacao de ids.
- `core/infra/node_sync.py` trata os dois ledgers como arquivos gerados: antes do pull do Desktop
  (`/system/update`) e na remediacao do Notebook, edicoes locais sao copiadas para
  `<estado>/ledger_backups/` e revertidas (`git checkout`), entao o update nao aborta mais (USR-374).
  USR-394: no `/system/update` a edicao local e guardada e reaplicada apos o pull se o arquivo nao mudou no
  upstream; se mudou, vale o upstream e a copia local vai para `.factory/runs/node_sync_conflicts/`.
  Antes de avançar o Notebook, a checagem final do Git permite somente os artefatos não rastreados
  de runtime já reconhecidos pela classificação do checkout. Arquivos rastreados com alterações e
  os demais arquivos não rastreados continuam bloqueando o avanço.
- USR-420: convergencia e backup usam o SHA da execucao (`--expected-sha` ou `DARKFAC_EXPECTED_SHA`).
  O alvo imutavel e somente `^[0-9a-f]{40}$` (40 hex minusculos; maiuscula nao e normalizada).
  `classify_checkout` e `verify` comparam HEAD e `/health` a esse SHA. `origin/main` que avancou
  depois nao substitui o alvo. Sem SHA valido, `node_sync` nao sonda e nao le `origin/main`.
  Se `rev-list` nao resolve o candidato, a classe nao e `converged` e nao ha `fetch`, salvo quando
  `HEAD` ja e esse SHA. So `main` limpa e `behind` faz
  `git fetch origin <sha>` e `git merge --ff-only <sha>`; worktree, outra branch, sujo, adiantado
  ou divergente nao recebem `fetch`, `merge`, `reset`, `checkout`, `clean` nem `restore`.
  `DARKFAC_FLEET_DISABLED_HOSTS` e lida uma vez por `verify`/`sync` (virgula, borda e caixa ignoradas).
  Host sai `excluded` (`disabled_by_policy`, `target_sha`) sem git, health, update, restart ou redeploy.
  `ok` exige a VPS incluida e os nos incluidos convergidos. VPS excluida deixa `ok` falso; Desktop
  excluido nao esconde VPS divergente. No redeploy, a flag vence o env e vale para o alvo do
  sync e do backup; SHA explicito invalido recusa sem POST. Sem os dois, `origin/main` e
  lido uma vez. Backup so com a VPS `converged` nesse SHA. `check-main` nao troca o candidato.
  USR-491: `classify_checkout` ignora, ao contar nao rastreados, apenas os arquivos e diretorios
  de runtime da frota explicitamente listados em `.factory/runs` (incluindo
  `fleet_policy_snapshot.json` e `model_audit_receipt.json`). Codigo e outros arquivos nao
  rastreados continuam classificando o checkout como `dirty`. Quando os nos convergem no SHA
  fixado, esses artefatos de runtime nao impedem o fluxo normal do backup pos-deploy.
  USR-515: antes da espera de tag, o pin de GHCR, compose, blue-green, `darkfac-cloud` e `/health`
  pode ser o ancestral publicado quando o alvo esta fora dos paths de `hub-image.yml` e ha runtime
  no intervalo. O sync permanece no SHA alvo. O stdout traz `published=` e `reach=`.

# Fila de demandas (entrada)

## Entrada unica

Toda demanda do owner (Telegram `/demand`, DarkHub, chat) entra por UMA funcao:
`core.line.owner_intake.submit_owner_demand(texto, source=..., demands_service=...)`.

1. Cria o ticket no armazenamento duravel (`DemandsStore`, `demands.json` no diretorio de estado
   compartilhado), nunca em arquivo efemero de container.
2. Registra o ticket na fila canonica. Quando `core.demands.queue_store.register_ticket`
   (USR-343 S4a) estiver em `main`, o gancho `_register_in_canonical_queue` passa a usa-lo sem
   outra mudanca; ate la o `DemandsStore` e a fila canonica.
3. Se `autosubmit_enabled()`, empurra o ticket para a linha (`submit_ticket_to_line`).

## Um alimentador so

O `CloudCoordinator` inicia um unico alimentador: `FeederCycle` (agenda, a cada 15 min) que
envolve `BacklogFeeder` (avalia G1, dependencias e vagas). Um redeploy nao perde nada: o
alimentador le o armazenamento duravel e o `submit_ticket_to_line` e idempotente
(`ticket:<id>`).

Antes de admitir trabalho, o launcher e o intake da linha consultam os PRs abertos no repositorio
do projeto (`ProjectRegistry`). Um PR que identifica o mesmo ticket pela branch, titulo ou corpo
bloqueia uma segunda implementacao e retorna o numero e o link da execucao existente. Se o GitHub
nao puder ser consultado, a admissao falha fechada. Enquanto nao ha PR, a reserva unica por
`project_id` e `ticket_id` no control store arbitra concorrencia entre launcher e linha; a reserva
e liberada no encerramento, permitindo nova tentativa, e tickets diferentes continuam independentes.

## Grill de um toque

A mensagem de grill no Telegram traz, por pergunta com recomendacao, o botao
"Aceitar recomendada": payload `cb:grill:<run_id>#<question_id>:<indice da opcao>`, o mesmo
caminho dos botoes de opcao (`build_telegram_line_grill_handler`), que grava a resposta e
acorda o job de grill.


### Fonte unica de OAs no DarkHub

Em producao, defina `DARKFAC_OWNER_ACTIONS_MODE=control` e
`DARKFAC_HF02_DATABASE_URL` com a URL PostgreSQL de escrita usada pela fabrica.
Nao use uma URL somente leitura. A conexao deve alcancar a mesma base e schema
dos workers. `DARKHUB_CONTROL_DATABASE_URL` sozinho nao configura a fila de OAs.
Para desenvolvimento/testes isolados, selecione `DARKFAC_OWNER_ACTIONS_MODE=file`;
`DARKFAC_OWNER_ACTIONS_PATH` escolhe o arquivo. Falha de conexao nunca seleciona arquivo
nem SQLite em memoria. O SQLite local continua disponivel no control store da fabrica.

Na inicializacao, o Hub importa `hub/data/owner_actions/resolutions.json` e `dynamic.json`
uma unica vez e grava `control_migration_v1.json` no mesmo volume depois de persistir tudo.
Done no banco e terminal; a migracao pode ser retomada apos falha parcial. Resolucao sem
definicao e arquivo corrompido impedem a migracao e deixam a fila indisponivel, com erro
no log. Preserve o volume e os arquivos legados ate confirmar a migracao. Depois do
marcador, eles sao ignorados. Nao existe RemoteLedger nem reconciliacao por `answer_sync`.

GET de OAs/intervencoes e mutacoes devolvem HTTP 503, `available=false`, `ok=false`
quando nao ha conexao com o control store. O painel remove a lista anterior e mostra
"Fila indisponivel: sem conexao com o banco". `read_at` identifica a ultima leitura
concluida e aparece no topo da fila, inclusive quando vazia.

Validacao apos deploy: verificar o marcador de migracao no volume, comparar ids/status
do Hub com o banco, fechar uma OA de teste e conferir persistencia apos reinicio. Durante
indisponibilidade controlada do banco, GET e POST devem devolver 503 e nenhuma mutacao
pode afirmar sucesso. Este procedimento nao foi executado em producao nesta entrega.

## Conexao compartilhada do control store (USR-455)

`PostgresControlStore._connect()` centraliza toda abertura de conexao (antes espalhada por
`self._psycopg.connect(self.raw_url)` em cada metodo) usando `resolve_control_database` e seus
limites padronizados de `connect_timeout`/`statement_timeout`. Semantica de fila e reservas nao
mudou; apenas a conexao passou a ser unica e consistente. `_connect()` resolve `raw_url` sob
demanda quando `_resolved_database` nao foi preenchido (construcao manual em teste), em vez de
tratar o store como desconfigurado.

## Politica de frota e auditoria de modelos no Hub (USR-471)

- `GET /line/fleet-policy` (`LineDemandsMixin.get_fleet_policy`) expoe o mesmo snapshot de
  `python -m core.line.fleet_policy --effective` (`effective_policy_snapshot()`), com
  configuracao por host, leituras vivas de recursos, exclusoes de configuracao/ambiente e
  elegibilidade resultante. Falhas de leitura degradam por linha ou retornam snapshot vazio
  com `error` sem HTTP 500.
- `GET /line/model-audit` (`LineDemandsMixin.get_model_audit(ticket)`) recomputa a auditoria de
  execucao real (`audit_recorded(ticket)`) a partir do ledger de `ExecutionSample`, nunca de
  recibo em cache. Sem amostras ou em erro de leitura retorna HTTP 200 com `passed: false` e
  `violations` explicitas (`audit_unavailable`).

## Higiene de `node_sync` e caminhos absolutos em `OwnerActionDraft` (USR-532)

- `GENERATED_LEDGER_FILES` em `core/infra/node_sync.py` inclui `.factory/learning/learning_ledger.json`
  (alem de `.factory/demands/demands.json` e `.factory/owner_actions/owner_actions.json`), garantindo que
  alteracoes locais de aprendizagem nao travem `git pull --ff-only` nos nos secundarios (`USR-394`);
  `.factory/runs/model_audit/` e `.factory/runs/review_benchmark.jsonl` ficam registrados em
  `FLEET_RUNTIME_UNTRACKED_*` (fora do `.gitignore`, preservando a visibilidade exigida por USR-471 T6).
- `OwnerActionDraft` (`core/owner_actions/models.py`) valida comandos em passos destinados ao owner para
  rejeitar invocacoes relativas dependentes de `CWD` (`git <subcmd>` sem `-C <abs>`, script `.py` relativo
  ou `python -m core.*` sem `Set-Location <abs>`), mantendo leitura retrocompatível de `OwnerAction` historicas.
