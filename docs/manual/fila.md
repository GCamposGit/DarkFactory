<!-- covers: core/git/app_token.py, core/infra/node_sync.py, scripts/sync_usage_to_cloud.py, scripts/snapshot_queue.py, core/demands/queue_store.py, core/demands/store.py -->
# Fila de tickets (USR-343)

A fila de desenvolvimento vive no control store: Postgres quando `DARKFAC_HF02_DATABASE_URL`
está definida, senão o SQLite local `.factory/control.db`. Tabelas (criadas com
`CREATE TABLE IF NOT EXISTS`):

- `tickets (project_id, ticket_id, status, payload, updated_at)`
- `owner_actions (action_id, status, payload, updated_at)`

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

## Acoes do owner e status (S4b)

- Escritores de `owner_actions` (respostas do Hub via `answer_sync`, espelhos, alertas de CI, CLI)
  gravam no control store (`OwnerActionStore` no ledger padrao). Nao existem mais PRs `chore/oa-*`
  nem `ticket/queue-*`; a leitura mescla o control store sobre o `owner_actions.json`.
- `DemandsStore.save_ticket` e `update_status` gravam a linha em `tickets` do control store no ledger do checkout compartilhado. No ledger da worktree a mesma chamada só atualiza a linha `(project_id, ticket_id)` já presente na fila. `DARKFAC_DEMANDS_PATH` deixa a gravação só no arquivo.
- Snapshot diario: `python C:\dev\DarkFac\scripts\snapshot_queue.py` (a flag `--force` ignora o
  marcador do dia) escreve `demands.json` e `owner_actions.json` a partir do control store em UM
  commit, publicado em `main` com o token do GitHub App (`core/git/app_token.py`). E idempotente
  (marcador no diretorio de estado; ledger igual nao gera commit) e roda uma vez por dia dentro do
  laco de `scripts/sync_usage_to_cloud.py`. Le e escreve so numa worktree
  descartavel de `origin/main`: o checkout de quem chama nunca fica sujo (USR-414). O control store e mesclado sobre os arquivos de `origin/main` (ids so do arquivo sao sincronizados de volta ao control store antes do export e permanecem) e, se qualquer id existente for perdido ou a contagem de ids cair, o snapshot aborta com erro sem commitar (USR-419, USR-434). O fetch sincroniza a ref `origin/main` antes do checkout da worktree e `next-id` respeita os ids gravados em todo o historico git para evitar reutilizacao de ids.
- `core/infra/node_sync.py` trata os dois ledgers como arquivos gerados: antes do pull do Desktop
  (`/system/update`) e na remediacao do Notebook, edicoes locais sao copiadas para
  `<estado>/ledger_backups/` e revertidas (`git checkout`), entao o update nao aborta mais (USR-374).
  USR-394: no `/system/update` a edicao local e guardada e reaplicada apos o pull se o arquivo nao mudou no
  upstream; se mudou, vale o upstream e a copia local vai para `.factory/runs/node_sync_conflicts/`.
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
  excluido nao esconde VPS divergente. No redeploy, a flag vence o env e vale para pin, GHCR,
  `/health`, sync e backup; SHA explicito invalido recusa sem POST. Sem os dois, `origin/main` e
  lido uma vez. Backup so com a VPS `converged` nesse SHA. `check-main` nao troca o candidato.

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
