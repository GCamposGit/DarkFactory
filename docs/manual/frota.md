<!-- covers: core/line/fleet_dispatcher.py, core/line/pending_delivery.py, core/line/express_lane.py, core/line/admission_store.py, core/line/failure_taxonomy.py, core/line/run_outcome.py, core/line/failure_alerts.py, core/line/stale_active.py -->
<!-- covers: core/line/fleet_dispatcher.py, core/line/recovery_receipt.py -->
<!-- covers: core/line/fleet_policy.py, core/line/operational_context.py -->
<!-- covers: core/harness/remote_worker.py -->
# Operação da frota

Falhas repetidas de candidato colocam o ticket em hold e `notify_ticket_hold` avisa o
owner pelo bot Telegram dedicado. O destino é `TELEGRAM_OWNER_CHAT_ID`; chats e usuários
compartilhados de operação não são usados como fallback. Sem destino válido, o alerta
continua visível no DarkHub com a instrução de configurar essa variável, registra
`missing_destination` e não declara entrega Telegram nem cria outbox. Holds de `HumanRequest`
seguem a mesma regra. Confira também [operação interativa](operacao_interativa.md#alertas-de-hold)
para o procedimento de verificação ponta a ponta.

O `/health` de cada worker remoto publica `workers`, `available_gib` e `observed_at`
medidos no próprio host com o critério de RAM do portão. Um host ocupado, sem capacidade
positiva ou sem leitura válida permanece fora da admissão.

Na elegibilidade local, um worker com memória suficiente é aceito (USR-452).
Zero workers permanece indisponível. O gate ajusta seu prazo ao paralelismo real,
sem forçar processos além da capacidade e sem executar suítes concorrentes.
## Espera de rota e retomada automática (USR-473)

Sem assinatura elegível, o roteador tenta OpenRouter com saldo confirmado, dentro do
orçamento vigente e preservando capacidade e revisão por outra família. Sem saldo ou
rota, o launcher grava a espera em `.factory/pending_delivery/<ticket>.json` antes de
sair com código75. O dispatcher permanece ativo: espera não depende de um chat ou
heartbeat de LLM. O serviço verifica snapshots e crédito, sem gerar texto para supervisionar.

A espera registra etapa, worktree, implementador e próximo horário. Cada espera é
consultada no máximo a cada15 minutos; o estado sobrevive ao reinício. REVIEW usa
`run_ticket.py --resume-delivery` na mesma worktree; o launcher preserva validação
vinculada ao candidato. PLAN/BUILD usam a retomada canônica do ticket, com suas reservas.
Processo vivo não é duplicado, ticket terminal sai da fila e falha diferente de
`route_wait` fica bloqueada com causa explícita. Uma espera de REVIEW bloqueada por
`no_route_available`, `review_agent_exhausted` ou `route_wait` é reavaliada no ciclo
seguinte (USR-512 T3). Com rota compatível, o dispatcher lança
`run_ticket.py --resume-delivery <ticket> --worktree <a mesma>`, sem PLAN e sem BUILD.
`deploy_evidence_missing`, `deploy_evidence_mismatch` e `implementing_family_unknown`
continuam bloqueadas, mas as duas de evidencia de ticket `completed` ou `cancelled` ha mais de 6 h
(`updated_at`, ou a idade do arquivo) sao movidas para `.factory/pending_delivery/archive/` e registradas em log (USR-490). O `launcher_exit:1` deixado por USR-467 só reabre depois de
confirmar candidato, gate vigente e revisão ainda ausente; os outros `launcher_exit`
não usam essa reabertura. Decisão material `waiting_human`
continua dependendo do owner. Nenhuma espera compra créditos nem aumenta o teto.

Durante recuperação, o mesmo serviço admite só retomadas:

```powershell
Set-Location C:\dev\DarkFac
python -m core.line.fleet_dispatcher --loop --resume-only
```

O modo `--resume-only` não admite novos BUILDs e não executa sentinela/digest adicionais.
A variável persistente `DARKFAC_FLEET_RESUME_ONLY=1` também ativa esse modo no boot.
A tarefa Windows `DarkFac-Fleet-Dispatcher` mantém boot e reinício em falha; não substitua
esse serviço por inferência recorrente. Ao sair da recuperação, remover `--resume-only`
da ação da tarefa e limpar `DARKFAC_FLEET_RESUME_ONLY` libera novamente a fila regular.

Para uma recuperação que não termina em ociosidade, confira primeiro a interface aceita
pela CLI e passe a lista como um único valor separado por vírgulas:

```powershell
python -m core.line.fleet_dispatcher --help
python -m core.line.fleet_dispatcher --loop --recovery-tickets "USR-1,USR-2"
```

`--recovery-tickets` mantém a ordem e dá prioridade aos itens da lista. Com
`recovery.active=false`, a lista não serializa nem bloqueia a frota: depois dela o dispatcher
continua pela fila regular e usa os hosts livres, sempre respeitando reservas e `suite_lock`.
Com `recovery.active=true`, o modo global de recuperação mantém no máximo uma entrega em
andamento e a lista segue serializada. Retomadas em execução têm precedência.

A tag `line-block` é a única marca de via expressa lida pelo despachante (USR-494). Um ticket
elegível com essa tag sai antes da lista de recuperação e do restante da fila, no primeiro host
livre. Título, campo extra ou outra tag não mudam a ordem. Tag excluída, dependência aberta e
cooldown deixam o ticket fora da fila mesmo com `line-block`. Com `recovery.active=false`, os
hosts livres seguintes seguem a lista e depois a fila regular. Com `recovery.active=true` e
nenhuma entrega em voo, o `line-block` ocupa o único slot e o item da lista espera o ciclo
seguinte. Com uma entrega ou retomada já em voo, o `line-block` não abre segunda entrega. O
comando lançado continua `run_ticket.py <id> --fleet --host-id <host>`.

Negação ao editar o motor no chat (USR-494 T2) grava a causa `self_modification` e acrescenta a tag `line-block` no control store. A frase Self-Modification, ou `permission denied` cujo caminho é `core/line`, `core/harness`, `run_ticket.py` ou `scripts/line_validate.py`, entra nessa causa. A mesma gravação não duplica a tag nem a causa. `protected_path` e `permission denied` de banco ou ACL ficam de fora. O run que recebeu a negação não relança a mesma edição. `classify_log_cause` lê `self_modification` na linha `[ERRO NA ENTREGA] causa:`; menção só na cauda do log não conta. Portão, revisão e piso de cota permanecem.

Quando a causa é `self_modification`, a fábrica abre uma ação do owner com um único comando (USR-494 T3), implementado em `core/line/express_lane.py`. Registrar de novo devolve a mesma ação aberta. O comando é:

```powershell
Set-Location C:\dev\DarkFac; python -m core.line.express_lane run --ticket USR-494
```

Troque `USR-494` pelo id do ticket (o comando já inclui `Set-Location <checkout>; ` para funcionar a partir de qualquer diretorio). O comando percorre todos os requisitos do host e imprime cada falha, inclusive o piso de cota de 15%. Com qualquer requisito ausente, não lança o ticket. Com o host válido, lança exatamente `run_ticket.py <ID> --fleet --host-id <host deste host>`, uma vez. Ticket já em voo (`implementing`, `validating` ou processo vivo) ou terminal (`completed`, `cancelled`) termina com sucesso e não cria outro processo. Portão, revisão e o piso de cota do `run_ticket.py` continuam valendo.

A segunda ocorrência da mesma causa `self_modification` alerta o owner uma vez (USR-494 T4). A primeira só grava o episódio em `express_episodes.json`, ao lado do estado do despachante, e fica em silêncio. O alerta sai pelo `NotificationService` no canal ALL, com a causa `self_modification` e o comando único `Set-Location <checkout>; python -m core.line.express_lane run --ticket <ID>` como próximo passo. Outra ocorrência desse episódio não gera outro alerta. A chave é o ticket e a causa classificada: o texto da cauda do log não abre episódio nem muda a chave. Entrega com `[DELIVERY_MERGED]` ou `merged_awaiting_deploy`, ou uma causa classificada diferente no mesmo ticket, apaga o episódio; duas falhas novas da mesma causa podem alertar outra vez. O alerta não liga `recovery.active`. Causas de classe `owner` entram em hold imediato, sem consumir o orçamento do candidato; a ação existente do owner continua sendo a via de resolução.

Três falhas de candidato ou de causa desconhecida criam hold só desse ticket; falhas de infraestrutura apenas aplicam backoff durável; o modo global de recuperação não é mais ativado por falhas, e os demais tickets seguem despachados.

O estado `recovery_list` em `last_cycle` informa `pending` e `cause`. A lista encerra com
`recovery_list_completed` quando os itens ficam terminais ou `recovery_list_expired` ao
vencer o TTL. Se nenhum item estiver admissível, publica `recovery_list_no_admissible`, mas
não bloqueia a fila regular. Ticket ausente ou pendência material não contam como concluídos.
Não passe os IDs como argumentos posicionais separados: a CLI recusa a chamada com código 2
antes de iniciar o ciclo.

Quando uma entrega falha depois de preservar um candidato confiável, o launcher retoma a
entrega nessa worktree (`_resume_delivery`), sem repetir PLAN ou BUILD. Se não houver sinal
confiável de desenvolvimento concluído, mantém o fluxo normal do ticket.

`completed` no ledger não encerra uma entrega cujo recibo ainda diz `deployed=false`.
O controlador mantém `merged_awaiting_deploy`, executa o deploy canônico e só encerra
a espera após verificar o SHA saudável de produção incluindo o candidato. Falha de
deploy permanece pendente para nova tentativa, sem chamar um agente para supervisionar.

### Deploy com convergencia de nos pendente (USR-541)

O deploy canonico que sai com codigo 3 (`EXIT_NODE_DIRTY`: VPS saudavel, mas Desktop ou Notebook ainda fora do SHA) deixa de ser repetido por inteiro a cada ciclo. O despachante grava o adiamento por SHA em `<state_root>/runs/deploy_deferrals.json` (SHA alvo, no pendente, motivo redigido, codigo, `attempts`) e preserva `node_sync` pending. Daí em diante usa so a recuperacao delimitada: `python -m core.infra.node_sync retry` seguido de `python -m core.infra.backup_cron --once`, com backoff exponencial (300 s ate 3600 s) e teto de 6 tentativas. A recuperacao so conclui o deploy (`pending_deploy` falso, `deploys` +1, `pending_releases` fechados) quando os dois comandos saem 0: `retry` so devolve 0 com VPS e nos incluidos no SHA candidato, e o backup pos-deploy segue exigido. Pendencia sumida sem essa prova, ou novo merge, reabre o deploy completo; um deploy completo que sai 0 limpa registro e contador. Teto esgotado grava `convergence_recovery_exhausted` em `recent_failures` uma vez e mantem a pendencia.

O estado do despachante expoe `deploy_deferral` (`state: awaiting_node_convergence`, `target_sha`, `pending_node`, `recovery_attempts`, `recovery_next_at`, `recovery_exhausted`), para o Hub e o briefing separarem "aguardando no" de "deploy falhou" (`canonical_deploy_exit:<codigo>` em `recent_failures`). Equivalente manual: `Set-Location C:\dev\DarkFac; python -m core.infra.node_sync retry; python -m core.infra.backup_cron --once`.

### Conclusao sem merge verificavel (USR-538)

Uma conclusão legada em REVIEW sem recibos de merge/deploy só volta a `planned` quando
o SHA do candidato está registrado e `git merge-base --is-ancestor <candidato> origin/main`
confirma que ele ainda está fora da main. A fila registra `completed_without_merge`, o
ledger de reconciliação recebe evento idempotente e o dispatcher retoma com
`--resume-delivery` na mesma worktree. Falha ao ler Git ou atualizar a fila mantém a
pendência bloqueada e preserva candidato e artefatos. Portão e revisão independente
vinculados ao digest atual continuam obrigatórios; somente merge confirmado em
`origin/main` e saúde de produção no SHA correspondente concluem e removem a espera.

O status `completed` também não é gravado durante a preparação do candidato. A
implementação registra `candidate:<sha>` e mantém o ticket aberto enquanto CI, PR ou
merge estiverem pendentes, falhos ou em conflito. A conclusão canônica exige recibo
de PR em estado `merged` com SHA confirmado em `origin/main`; tickets com deploy ao
vivo também exigem a verificação de convergência. A origem histórica da conclusão
prematura de USR-483 foi o commit `c64f1d9c`, que registrou `completed` com a evidência
`83f480ec659dd38032e5dbd1e1d83ea8bd4afd10` antes do merge.

## CI vermelho sem recibo de merge (USR-509)

Uma espera com causa `ci_fix_requires_revalidation` ou `ci_fix_route_wait`, sem
`merge.json` e sem `deploy_report.json`, é retomada de entrega mesmo quando o control
store já está `completed`. O tick lança `run_ticket.py --resume-delivery` na worktree
da espera. A linha fica `waiting` ou `running` com a mesma causa de recuperação de CI.

`merge.json` com `merge_sha` permanece na trilha de evidência de deploy. Divergência
de SHA, digest, veredito ou família fica `deploy_evidence_mismatch`. Evidência coerente
fica `merged_awaiting_deploy`. Exceção: um projeto externo sem smoke aplicável grava
`smoke_applicable: false` no `deploy_report.json`; com evidência coerente a espera vira
terminal (`proof: merged_no_smoke`) e nunca dispara o redeploy da DarkFac (EC-05).

Ticket `completed` com outra causa e sem prova de deploy fica `blocked` com causa
`deploy_evidence_missing`.

O limite de uma correção de CI fica em `DARKFAC_STATE_ROOT/runs/ci_recovery/<TICKET>.json`,
fora do contexto apagável. Esgotá-lo conta uma falha `ci_red` no orçamento durável do ticket.
Reabrir o ledger em `planned` preserva esse contador e a mesma worktree. A espera só bloqueia
automaticamente ao chegar a três falhas de candidato; abaixo disso, retoma depois do prazo.
Uma falha ao reabrir o ledger não apaga o contador nem permite repetir a correção com orçamento novo.

## Esperas terminais, elegibilidade e progresso (USR-475 T2)

Antes de honrar `blocked` ou um `next_check` no futuro, o tick lê o status durável do control store.

`cancelled` e `canceled` são a prova. A espera sai do disco mesmo com o JSON em `blocked` ou com o prazo ainda por vencer.

`completed` só sai do disco quando o pacote de deploy é coerente e `deployed` é verdadeiro: `merge.json`, `validation.json`, `review_state.json` e `review_execution.json` concordam em SHA e digest, a revisão aprova sem `forced`, e a família do revisor não está entre as famílias de implementação. Sem essa prova a espera fica `blocked` com `deploy_evidence_missing` ou `deploy_evidence_mismatch` e o arquivo permanece, inclusive durante o backoff. A recuperação de CI sem SHA em `merge.json` nem em `deploy_report.json` continua sendo entrega: o tick não lança `--resume-delivery` enquanto `next_check` estiver no futuro.

Cada transição reconciliada é registrada em `pending_delivery/reconciliations/<ticket>.json` com status e causa anteriores e novos, motivo e evidência. Repetir a mesma observação não acrescenta outro evento. Status duráveis contraditórios bloqueiam só o ticket correspondente; um ledger de reconciliação ilegível ou inconsistente também falha fechado sem apagar a espera.

Hold, backoff de um ticket, espera `pending_delivery` ou falha de lançamento (`lancamento_falhou`) não retiram do mesmo ciclo os outros tickets `planned` independentes. Reserva global de recuperação, reserva de admissão e store indisponível continuam devolvendo o ticket à frente da fila daquele host.

O contador de fila bloqueada (`blocked_cycles`) só continua quando o fingerprint, os tickets e os motivos atuais coincidem com o ciclo anterior. Estado legado sem esse vínculo recomeça em um ciclo; sem bloqueio atual, o contador e as causas são zerados. Assim uma espera antiga não prolonga a contagem para outro conjunto de tickets.

Cada ciclo grava `activity`. O valor `running` exige filho (`status` `running` com pid), `in_flight`, despacho ou mudança de etapa de um ticket já observado. A primeira leitura de uma etapa não é avanço. Sem isso, o ciclo fica `blocked` quando há retomada `blocked` ou `dispatcher_busy`, ou quando `blocked_causes` está preenchido; nos demais casos fica `idle`. O processo do despachante estar vivo não é esteira em execução.

Falha de classe diferente de `infra` alerta uma vez por fingerprint SHA-256 de ticket, causa e evidência. O ledger fica em `DARKFAC_STATE_ROOT/failure_alert_fingerprints.json`. A mesma combinação fica em silêncio. Causa ou evidência diferente produz um alerta novo. Envio que falha não grava o fingerprint, então a tentativa seguinte pode alertar. O hold da terceira falha de candidato usa esse mesmo fingerprint.

## Retomada de BUILD e falhas de filhos de entrega (USR-475 T1)

`run_ticket` mantem uma worktree por ticket e `progress.json` checkpointa cada fatia de BUILD, junto do digest do plano, do candidato e do caminho da worktree. Um erro tecnico com esses vinculos intactos retoma somente o restante do plano. Se a evidencia local divergir, o launcher para sem resetar o plano, o checkpoint ou a worktree.

Quando um filho de `pending_delivery` falha, a fila grava primeiro uma entrada estruturada com `run_id`, codigo, causa, classe, etapa e detalhe redigido. A proxima tentativa conserva esse historico. Falha de filho na etapa `review` conserva `resume_delivery`; por isso o dispatcher inicia `--resume-delivery` na mesma worktree. Se o control store ja diz `completed`, mas ainda nao existe recibo de merge/deploy e ha uma falha de entrega registrada, a espera continua acionavel em vez de virar um bloqueio terminal.

O `core.line.fleet_dispatcher` consulta a fila continuamente e despacha tickets `planned`
na ordem Notebook, Grokbox e Desktop. Com tickets elegíveis, o Notebook livre recebe trabalho
antes do Grokbox e do Desktop. O serviço não depende de sessão interativa.

Queda do Grokbox é esperada (USR-492). Uma sonda sem `/health` (`health_indisponivel`) não chama
`notify`, não abre alerta nem ação do owner e não interrompe o ciclo. O Notebook livre segue
recebendo o ticket; com o Notebook cheio, o Desktop ainda recebe. O ciclo seguinte sonda o
Grokbox de novo. Não há memória de offline entre ciclos.

Um job em voo no Grokbox que termina por infraestrutura (`exit 75` ou `[REMOTE_JOB_LOST]` no log)
libera a reserva de admissão, mantém o ticket `planned` e não entra em cooldown global. O ciclo
seguinte lança esse ticket no próximo host livre da ordem. O mesmo recurso do Grokbox continua
no disjuntor até a versão do recurso mudar, para não repetir a tentativa idêntica; isso não tira
o ticket dos outros hosts. Espera de rota (`harness_infra_error: route_wait`) no Grokbox continua
em cooldown. Falha de lançamento e infra dos outros hosts também continuam em cooldown. O alerta
da segunda `self_modification` permanece só na via expressa.

Uma reserva de host tem prazo; quando o host desaparece, o próximo reconciliador expira a reserva e
o job volta a ser elegível para admissão em outro host. A indisponibilidade de uma VM é
tratada como falha normal de infraestrutura, sem concluir o ticket.

No início do ciclo, o dispatcher reconcilia e consulta uma vez as reservas canônicas do control
store e reaproveita esse snapshot para decidir a capacidade inicial e publicar `free_hosts`.
Hosts com reserva `active` ou `draining` ficam fora da capacidade livre, mesmo que não apareçam
no `in_flight` deste processo. Depois do probe e da reserva local de admissão, o dispatcher
consulta o store novamente imediatamente antes de cada lançamento. Se surgir uma reserva para
aquele host, ele libera a admissão local, não lança o ticket e atualiza o snapshot usado no ciclo.
As reservas de outros processos não são renovadas nem liberadas; leases vencidas são expiradas
pelo reconciliador e só então o host volta a ser elegível. Se qualquer consulta canônica falhar,
o dispatcher não inicia trabalho e publica `free_hosts` vazio naquele ciclo.

## Política efetiva de hosts e workers (USR-471 T2)

A topologia persistente fica em `.factory/config/line_fleet.json`: cada host declara
endpoint, prioridade, `max_workers` e `min_free_memory_gib`; `disabled_hosts` e
`hosts[].enabled=false` são exclusões de política. `DARKFAC_FLEET_DISABLED_HOSTS`
adiciona exclusões do processo. A exclusão é decidida antes da saúde, então uma resposta
saudável do Desktop não o torna elegível quando ele está excluído.

Consulte a decisão efetiva e atualize o snapshot compartilhado sem iniciar outro dispatcher:

```powershell
Set-Location C:\dev\DarkFac
python -m core.line.fleet_policy --effective
```

O relatório registra a origem da configuração, as exclusões de configuração e ambiente,
os limites e workers configurados, a leitura de recursos, a elegibilidade e o motivo por
host. O snapshot fica em `STATE_ROOT\runs\fleet_policy_snapshot.json` (ou sob
`DARKFAC_STATE_ROOT`); cada processo pode regenerá-lo atomicamente. Endpoints no relatório
mostram somente esquema, host e porta; credenciais, caminho e query são removidos, e as
leituras publicadas aceitam somente os campos tipados de capacidade.

`DARKFAC_TEST_WORKERS` é uma lista de prioridade explícita. Se um worker nela estiver
offline, a seleção tenta os demais endpoints configurados que não foram excluídos. Hosts
excluídos não são sondados. Sem candidatos elegíveis, o modo automático informa os motivos
e continua localmente; `--remote-required` termina com causa estruturada
`no_eligible_workers`, sem criar dispatcher ou reiniciar host.

Cada despacho inicia `run_ticket.py` direto de `PROJECT_ROOT`, sem clone efêmero `fleet-dispatch-*`
(EC-02). `run_ticket.py` cria a única worktree do ticket (`.worktrees/<ticket>-<ts>`) ou reaproveita a
preservada de uma tentativa anterior, para `darkfac` e para projetos externos (checkout resolvido por
`project_checkout`).

O processo filho recebe `DARKFAC_STATE_ROOT` resolvido no checkout do despachante (ou o
override configurado), pois fila, reservas e demais dados mutáveis são compartilhados entre
workers. Ele não herda `DARKFAC_PROJECT_ROOT`. Durante a entrega, `run_ticket.py` define
`DARKFAC_PROJECT_ROOT` para a worktree do ticket em cada tentativa do portão, inclusive nos retries
remotos. Assim o portão recusa uma worktree candidata suja.

Como o filho roda do checkout compartilhado, `node_sync` e a sincronização pós-merge (`GitAutonomyManager._cleanup`) adiam o fast-forward enquanto
houver `run_ticket.py` da frota em voo (`running_fleet_runs`), com `logger.warning`
(`fleet_runs_in_flight=...`); o próximo ciclo tenta de novo. Dois `gate_timeout` seguidos no mesmo
`main_sha` colocam o ticket em hold com causa `gate_timeout_repeated`. Uma worktree preservada só é
reaproveitada se a base registrada em `branch.<ticket>.darkfacbase` for a base atual do projeto, e o
`progress.json` órfão só é arquivado pelo host que o gravou (`host`). Worktree legada sem `darkfacbase` só é reaproveitada se todos os commits após a base citarem o ID do ticket. O despachante entrega ao filho o registro selecionado em `STATE_ROOT/runs/<ID>.dispatch_ticket.json` (`DARKFAC_DISPATCH_TICKET`), que o intake prefere ao ledger local.

## Pin de execução e admissão (USR-510)

Em host com `worker_url`, o filho do despacho recebe `DARKFAC_TEST_WORKERS` igual só a essa URL, `DARKFAC_REMOTE_HARNESS=required` e `DARKFAC_FLEET_GATE_REMOTE=1`. Esse valor é o pin de execução do portão. O pin de execução não é a lista que a admissão lê. O mesmo filho recebe `DARKFAC_ADMISSION_WORKERS` com a lista que já estava em `DARKFAC_TEST_WORKERS` no ambiente do despachante, ou o default grokbox+desktop (`http://100.77.130.5:8080,http://100.78.181.90:8080`) quando essa lista estava vazia, mais a URL do pin, sem repetir uma URL.

No Notebook, host sem `worker_url`, o filho segue com `DARKFAC_REMOTE_HARNESS=off` e o despacho não substitui `DARKFAC_TEST_WORKERS`.

Ao encerrar um job, ou desfazer um checkout criado parcialmente, o despachante usa
`core.git.ticket_workspace.cleanup`. Antes de remover, a rotina confirma que o caminho é uma
worktree secundária registrada e desbloqueada, verifica `git status` (incluindo arquivos
rastreados, staged e não rastreados) e procura processos com o diretório atual dentro do
checkout. Alteração inesperada ou processo encontrado preserva os arquivos e o registro Git.
O comando Git de remoção não usa `--force`, então uma alteração criada depois da inspeção
também faz o Git recusar a remoção. Uma falha global de inspeção preserva o checkout. No
Windows, `AccessDenied` em metadados de um processo isolado não bloqueia toda a busca; a
consulta de arquivos abertos se limita a processos cujo diretório atual está na raiz do
repositório ou em uma pasta ancestral.

O argumento interno `discard_path_hashes` identifica arquivos gerados pelo próprio chamador.
A rotina restaura um arquivo ao `HEAD` apenas se ele for rastreado, for a única alteração e
seu SHA-256 ainda corresponder ao conteúdo registrado na geração. Se o conteúdo mudou, a
worktree é preservada. No encerramento de uma tentativa sem implementação, isso permite
descartar o ledger temporário criado pelo runner sem perder edições posteriores.

`CleanupResult.outcome` registra `removed`, `blocked_changes`, `blocked_process` ou `error`;
`reason` identifica a causa e `method` informa como a remoção foi concluída. Os chamadores
registram esses campos. Qualquer nova tentativa de remoção deve passar novamente por
`ticket_workspace.cleanup`; não use comandos Git ou de filesystem em paralelo para remover a
worktree. A deduplicação do deploy continua agrupando merges pela janela de silêncio configurada.

## Varredura de worktrees sem owner (USR-500)

O dispatcher de frota, iniciado pela tarefa persistente `DarkFac-Fleet-Dispatcher` no
checkout principal, chama `scripts/cleanup_orphan_worktrees.py` pelo seu gancho de manutenção.
O processo de manutenção roda em paralelo aos ciclos e registra a saída em
`orphan-worktree-cleanup.log` no diretório de logs do despachante. Uma trava do
sistema operacional impede duas varreduras simultâneas no checkout. O estado persistente
fica em `.factory/runs/orphan_worktree_cleanup.json`, com `status`, `last_attempt_at`,
`failure_count`, `next_attempt_at` e `last_failure` quando aplicável. Uma exceção na
varredura grava `status=failed` e agenda nova tentativa em `next_attempt_at` com backoff
exponencial de 1 hora, dobrando a cada falha e limitado a 24 horas. O gancho não inicia
outro subprocesso antes desse horário. Depois que a varredura retorna, inclusive quando
há erro isolado de classificação ou limpeza de uma worktree, o estado registra sucesso,
zera `failure_count` e mantém a cadência diária. Se o processo for interrompido antes de
gravar o resultado, um ciclo posterior pode retomar a manutenção, inclusive após recarregar
o despachante.
A rotina considera uma
worktree órfã quando o ticket está ausente ou terminal nas fontes de tickets, não há
processo, alteração, lock ou commit detached sem referência segura. O limite é 48 horas;
worktrees com idade igual ou inferior ao limite não são enviadas para remoção.

Ledgers gerados (`.factory/demands/demands.json`, `.factory/learning/learning_ledger.json`,
`.factory/owner_actions/owner_actions.json`, lista de `core.infra.node_sync.is_generated_ledger`)
não contam como trabalho: a varredura os restaura ao `HEAD` antes de remover. Quando o ticket é
terminal e a worktree tem mais de 7 dias, qualquer outra alteração é arquivada antes da remoção
em `<state_root>/backups/worktree_archive/<nome>-<timestamp>/` (`changes.patch` de
`git diff HEAD --binary`, `untracked.txt` e cópias em `untracked/`), com verificação de que os
arquivos existem; sem arquivo verificado a worktree é preservada (`archive_failed`). Ticket
aberto, checkout principal, worktree com lock ou com processo vivo nunca são arquivados.
O registro `cleanup_pending` não bloqueia mais a classificação: a worktree é reavaliada a cada
varredura. Registros `discard_path_changed` vêm de checkouts `fleet-dispatch-*` legados cujo ledger foi
alterado pelo próprio ticket depois do hash gravado na criação; como o HEAD destacado carrega
commits fora da base, eles seguem preservados (`detached_outside_base`).

Cada remoção é delegada a `ticket_workspace.cleanup`, que revalida alterações e processos
logo antes de remover. Registros `cleanup_pending` permanecem para observabilidade e são
retentados pela varredura diária. O resumo JSON registra contagens agregadas de `removed`, `preserved`
e `failures`, além de `blocked`, `ignored` e `errors`, com o motivo por caminho. Falha de
leitura ou de limpeza de uma worktree é registrada e a análise continua nas demais. Para
executar manualmente, use
`python scripts/cleanup_orphan_worktrees.py`; `--scheduled` respeita o mesmo controle diário.

## Reinício quando o SHA do checkout muda (USR-508)

Na largada, o despachante lê o HEAD completo do checkout em execução (`PROJECT_ROOT`) com
`current_git_sha` e `use_env` falso. `DARKFAC_GIT_SHA` e `origin/main` não entram nessa
comparação. Entre ciclos, antes de `run_cycle`, o loop lê o mesmo HEAD de novo.

Se os dois SHAs têm 40 hexadecimais e são diferentes, o processo se substitui com o
interpretador que já está rodando e os argumentos originais da largada:

```text
<python> -m core.line.fleet_dispatcher <argumentos originais>
```

A substituição não chama `kill`, `terminate` nem `wait` nos handles em voo nem nos filhos
de `pending_delivery`, e não remove worktrees. Esses runs continuam no
sistema; o processo novo os enxerga pelo inventário de processos.

SHA igual, SHA ilegível, `--once` e `--dry-run` não substituem o processo por essa
comparação. O estado do ciclo (`.factory/runs/fleet_dispatcher_state.json`) publica
`code_sha` (HEAD observado, ou nulo se ilegível) e `code_sha_changed` (verdadeiro só
quando os dois SHAs completos diferem). Se o exec falha, o erro fica em
`fleet-dispatcher.log` e o loop segue para o ciclo com o código que já estava em
memória. Essa comparação não grava `DARKFAC_DISPATCHER_IMPORT_RESTART_SHA`.

## Import obsoleto na retomada (USR-508)

`ImportError` e `ModuleNotFoundError` dentro de `pending_delivery.tick` fecham aquela
retomada com status `check_failed` e causa `stale_dispatcher_import`. Quando a exceção
traz `name`, o resultado inclui `missing` com esse nome. O JSON da espera permanece no
disco, com o mesmo status e o mesmo pid de antes da exceção. `RuntimeError` e as demais
exceções do tick seguem com a causa igual ao nome da classe.

No `--loop`, com o despacho real (`launch` ausente) e fora de `--dry-run`, o
despachante se substitui depois do ciclo e antes do resumo diário e do sentinela, mesmo
com o HEAD igual ao da largada. A substituição grava
`DARKFAC_DISPATCHER_IMPORT_RESTART_SHA` com o HEAD completo desta geração e usa o mesmo
comando do reinício por mudança de checkout. Handles em voo e filhos de
`pending_delivery` continuam sem `kill`, `terminate` ou `wait`.

A variável é gravada antes da substituição. Com ela igual ao HEAD atual, uma nova
ocorrência só registra a causa no ciclo e o loop dorme: não há segundo exec nessa
geração. Se o exec falha, o erro fica no log e essa geração também não tenta outro
exec. `--once`, `--dry-run` e um `launch` presente não disparam esse reinício.

## Notebook e Desktop (Windows)

Instale a tarefa a partir do checkout principal atualizado, nunca de uma worktree.
Use uma conta dedicada de serviço nos dois computadores, com senha gerenciada pela
organização e direito de **Log on as a batch job**. Conceda a ela leitura/execução no
checkout e no ambiente virtual, gravação em `.factory\runs` (estado) e na pasta de logs.
Não a torne administradora local. O Python, o checkout e o estado da tarefa devem estar
em caminhos locais permanentes, acessíveis antes de qualquer usuário entrar.

O instalador usa o `python.exe` encontrado no `PATH`, o checkout onde o script está e o
usuário atual como conta de serviço. São os campos `Command`, `WorkingDirectory` e
`UserId` do XML produzido por `core.line.fleet_task_xml.generate_task_xml`. O XML ainda
define gatilho `BootTrigger`, logon `Password`, nível `LeastPrivilege`, uma única
instância e limite de execução ilimitado. Para ajustar os três caminhos/identidade,
altere os valores no instalador e no gerador antes de registrar novamente.

No PowerShell, como a conta de serviço, rode:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_fleet_dispatcher.ps1
```

Informe a senha da conta de serviço quando solicitada. O Agendador armazena a credencial
protegida pelo Windows e executa a tarefa mesmo sem sessão interativa. A tarefa reinicia
após falha a cada minuto, até 999 vezes. O instalador recusa execução a partir de uma
worktree. Para trocar Python, checkout ou conta, ajuste os valores correspondentes e
rode novamente o instalador no checkout principal.

Configure variáveis operacionais no ambiente de sistema ou no ambiente da conta de
serviço antes de iniciar a tarefa. `DARKFAC_FLEET_LOG_DIR` escolhe a pasta absoluta de
log, que precisa permitir gravação para a conta. Sem ela, usa
`C:\dev\DarkFac\.factory\runs\fleet_dispatcher_logs`. O arquivo
`fleet-dispatcher.log` é UTF-8, limitado a 10 MiB com cinco cópias rotacionadas; as
mensagens também seguem para o console do processo. Não coloque segredos em arquivos
versionados ou argumentos da tarefa.

Verifique em `Agendador de Tarefas > Biblioteca do Agendador de Tarefas >
DarkFac-Fleet-Dispatcher > Propriedades`:

- `Geral`: conta de serviço correta, “Executar estando o usuário conectado ou não” e
  “Executar com privilégios mais altos” desmarcado.
- `Disparadores`: “Ao iniciar o sistema”.
- `Ações`: Python completo, argumentos `-m core.line.fleet_bootstrap --project-root <checkout>
  --state-root <checkout>\.factory --loop` e diretório de início igual ao checkout.
- `Configurações`: reinício após falha a cada minuto e instâncias simultâneas ignoradas.

O instalador recusa reinstalar (código de saída 3) enquanto o `fleet_dispatcher_state.json`
mostra um ticket em voo (`in_flight`) de ciclo recente; a fila e o SHA ficam intactos e basta
repetir o comando depois que o run concluir (USR-471).

Confira `Histórico` e `Último Resultado da Execução`. Para acompanhar tarefa e log:

```powershell
Get-ScheduledTask -TaskName DarkFac-Fleet-Dispatcher | Get-ScheduledTaskInfo
Get-Content C:\dev\DarkFac\.factory\runs\fleet_dispatcher_logs\fleet-dispatcher.log -Tail 100 -Wait
```

Se `DARKFAC_FLEET_LOG_DIR` apontar a outra pasta, use-a no segundo comando.

## Revisão OpenRouter e evidência de contexto (USR-474)

O payload da revisão inclui a SPEC e os critérios de aceite do ticket, conteúdo real
relevante da worktree dentro dos limites configurados e sanitizado, além de um manifesto
com caminhos, tamanhos e hashes para verificar o material enviado. O OpenRouter não lê
arquivos da worktree por ferramenta; a revisão só pode aprovar o contexto efetivamente
transportado. Contexto ausente recusa a revisão como `review_context_unavailable`, e uma
resposta truncada recusa como `review_output_truncated`.
Somente uma resposta completa, com `truncated=false`, pode sustentar aprovação; saída
truncada não é evidência de revisão concluída.

O evento de uso atribui `ticket_id` a partir da requisição ou de `DARKFAC_TICKET_ID`
para relacionar tokens e custo ao ticket. Se essa identidade não puder ser determinada, a
atribuição fica ausente e deve ser tratada como lacuna de evidência, sem inferência a
partir do prompt ou da resposta. A resposta normalizada preserva `finish_reason` e
`completion_tokens`; `truncated` identifica `finish_reason` `length` ou uma conclusão
de 4096 tokens.

Para recuperação, `--recovery-tickets` aceita exatamente um argumento CSV, preservando a
ordem e ignorando espaços e itens vazios. Confira sem iniciar a frota com
`python -m core.line.fleet_dispatcher --help`; a forma válida é
`python -m core.line.fleet_dispatcher --loop --recovery-tickets "USR-1,USR-2"`.
Passar `USR-1 USR-2` como tokens posicionais separados é erro de CLI (código 2) e é
recusado antes de inicializar o dispatcher ou executar um ciclo.

## grokbox (systemd)

O arquivo versionado é [`scripts/fleet-dispatcher.service`](../../scripts/fleet-dispatcher.service).
Ele aguarda `network-online.target`, usa `User=darkfac` e `Group=darkfac`, trabalha em
`/opt/darkfac` e executa `/opt/darkfac/.venv/bin/python -m core.line.fleet_dispatcher
--loop`. O `Restart=always` reinicia após encerramento normal ou falha, com espera
explícita de dez segundos (`RestartSec=10`). `TimeoutStopSec=30` permite encerramento
limpo, `LogsDirectory=darkfac` cria `/var/log/darkfac` com modo `0750`, e
`WantedBy=multi-user.target` habilita a inicialização no boot.

Os caminhos e a identidade são exemplos ajustáveis: revise `User`, `Group`,
`WorkingDirectory`, `ExecStart`, `EnvironmentFile`, `LogsDirectory` e
`LogsDirectoryMode` para a implantação. Use uma conta de sistema sem shell/login
interativo (`/usr/sbin/nologin`) e conceda acesso de leitura/execução ao checkout e à
venv, gravação ao estado sob `.factory/runs` e à pasta criada para logs. O arquivo de
ambiente fica fora do checkout, pertence a `root:darkfac` e usa modo `0640`.

Na VM grokbox, prepare a conta, checkout e venv; instale a unit e crie o ambiente
externo:

```sh
id -u darkfac >/dev/null 2>&1 || sudo useradd --system --home-dir /opt/darkfac --shell /usr/sbin/nologin darkfac
sudo install -d -o darkfac -g darkfac /opt/darkfac /etc/darkfac
# Atualize/deploy o checkout e crie /opt/darkfac/.venv antes de habilitar o serviço.
sudo install -o root -g root -m 0644 /opt/darkfac/scripts/fleet-dispatcher.service /etc/systemd/system/fleet-dispatcher.service
sudo touch /etc/darkfac/fleet-dispatcher.env
sudo chown root:darkfac /etc/darkfac/fleet-dispatcher.env
sudo chmod 0640 /etc/darkfac/fleet-dispatcher.env
sudoedit /etc/systemd/system/fleet-dispatcher.service
sudoedit /etc/darkfac/fleet-dispatcher.env
sudo systemctl daemon-reload
sudo systemctl enable --now fleet-dispatcher.service
```

No ambiente externo, defina `DARKFAC_FLEET_LOG_DIR=/var/log/darkfac` e as variáveis
operacionais necessárias aos workers. `EnvironmentFile=-/etc/darkfac/fleet-dispatcher.env`
carrega esse arquivo; o prefixo `-` permite iniciar mesmo quando ele ainda não existe.
Os endereços opcionais são `DARKFAC_FLEET_URL_GROKBOX` e
`DARKFAC_FLEET_URL_DESKTOP` (URLs base HTTP; o código tem defaults). Não copie tokens
ou senhas para a unit nem para o repositório; injete segredos apenas no arquivo local,
com permissões restritas. `DARKFAC_FLEET_LOG_DIR` pode apontar a outra pasta absoluta,
desde que o usuário do serviço possa gravar nela e que ela exista.

Os logs são gravados em `$DARKFAC_FLEET_LOG_DIR/fleet-dispatcher.log`, codificados em
UTF-8 e limitados a 10 MiB por arquivo com cinco cópias. A saída também é encaminhada ao
journal. Verifique e consulte os logs:

```sh
sudo systemctl status fleet-dispatcher.service
sudo systemctl is-enabled fleet-dispatcher.service
sudo journalctl -u fleet-dispatcher.service -n 100 --no-pager
sudo journalctl -u fleet-dispatcher.service -f
sudo tail -n 100 -f /var/log/darkfac/fleet-dispatcher.log
```

Se grokbox ou a VM ficar indisponível, não apague nem altere manualmente a reserva:
heartbeats cessam, o TTL expira, o reconciliador libera o slot e o ticket `planned` volta
a ser elegível em outro host. O despachante não espera esse TTL quando o job em voo no
Grokbox termina com `exit 75` ou `[REMOTE_JOB_LOST]`: libera a reserva no mesmo ciclo,
sem alerta e sem cooldown global, e o ciclo seguinte escolhe o próximo host livre.
`Restart=always` cobre falhas do processo local; a recuperação de host morto vem do
lease e da admissão normal da frota.

## Frota e VPS enxuta (USR-369)

Regras da VPS (Hetzner CX23, 2 vCPU, 4 GB):

- Todas as imagens sao construidas no CI e publicadas no GHCR. Nenhum `build:` nos compose de `deploy/dokploy/`.
- Soma dos limites de memoria <= 3,2 GB (teste: `tests/shape/test_vps_lean_usr369.py`).
- Concorrencia de build = 1 (`MAX_CONCURRENT_BUILDS` em `scripts/dokploy_redeploy.py`; os disparos sao sequenciais).
- n8n removido da pilha implantada.

## Orcamento de memoria (limites)

| Servico | Compose | Limite |
|---|---|---|
| darkfac-coordinator | cloud | 512M |
| darkfac-worker | cloud | 1536M |
| darkfac-backup-cron | cloud | 256M |
| darkfac-canary | cloud | 192M |
| darkfac-pg-tailnet | cloud | 64M |
| darkhub | hub | 512M |
| Total | | 3072M (teto 3200M) |

## Como restaurar o n8n

Os arquivos foram removidos do repositorio (historico preservado). Para restaurar:

```powershell
git -C C:\dev\DarkFac show a0e0f8d3:deploy/dokploy/docker-compose.n8n.yml
git -C C:\dev\DarkFac show a0e0f8d3:deploy/dokploy/env.n8n.example
```

Recoloque os dois arquivos em `deploy/dokploy/`, recrie o compose no Dokploy e reajuste os limites: o n8n pede 1024M + 256M, o que estoura o teto de 3,2 GB; reduza outros servicos antes e ajuste o teste. Enquanto existir um compose `n8n` ou `darkfac-n8n` no Dokploy, `dokploy_redeploy.py` sem `--only` o ignora; `--only darkfac-n8n` ainda o alcanca.

## Imagem do cloud no GHCR

O job `cloud` de `.github/workflows/hub-image.yml` publica `ghcr.io/gcamposgit/darkfac-cloud:<sha>` (coordinator, worker, backup-cron, canary) junto com a imagem do Hub. O `DARKFAC_GIT_SHA` do compose `darkfac-cloud` e gravado pela propria fabrica: `scripts/dokploy_redeploy.py` (`pin_cloud_git_sha`) acrescenta `DARKFAC_GIT_SHA=<sha da imagem>` ao env do compose no Dokploy antes de cada deploy, preservando as outras variaveis. Esse sha e o do alvo quando o commit toca os paths do workflow; quando o alvo esta fora desses paths e ha runtime pendente desde o SHA live, e o ultimo ancestral que o workflow publica (USR-515). O sync segue no SHA alvo. Antes do POST o script espera a tag `ghcr.io/gcamposgit/darkfac-cloud:<sha da imagem>` no GHCR (mesmo mecanismo do Hub); se ela nao aparecer no prazo, nao grava o sha novo nem dispara o deploy, e o sha anterior continua rodando. Depois da espera, o pin e revalidado contra o SHA live sob lock antes da troca; se o Hub avancou alem dele, o deploy e recusado. A mesma protecao cobre compose, blue-green e retrigger apos um deploy joined. A verificacao pos-deploy consulta `docker.getContainers` e exige que coordinator e worker estejam saudaveis, em execucao, com a imagem GHCR no SHA exato. O titulo do deploy Dokploy fica informativo: `Manual deployment` nao invalida evidencia da imagem em execucao. O stdout registra imagem, estado e saude para cada componente; sem prova completa, o backup pos-deploy nao roda. Se Git nao provar a linha necessaria para selecionar um pin, o diff acumulado nao autoriza consultar a tag do alvo. Sem sha valido, o disparo falha fechado em vez de construir na VPS. O alcance desse atraso sai no stdout (`published=` e `reach=`).

Mudancas em `.factory/config/**` (precos, roteamento, registro de modelos) reconstroem a imagem do Hub (USR-397); o ledger (`.factory/demands`, `.factory/owner_actions`) continua somente dados.

## Falhas registradas

Cada tentativa do launcher grava `DARKFAC_STATE_ROOT/runs/outcomes/<TICKET>.json` atomicamente,
fora do contexto temporário. O resultado contém ticket, `run_id`, código de saída, causa,
`cause_class`, estágio, diagnóstico em uma linha e instante UTC. O dispatcher prioriza esse
resultado quando o `run_id` coincide; sem arquivo válido desse run, usa o log legado.
O diagnóstico é sanitizado e exclui banners de cotas. O alerta informa o caminho do log real;
execuções manuais também mantêm um log sanitizado ao lado do outcome. `recent_failures` conserva
as 20 últimas falhas, com causa e classe além do host e do código.

O ledger `DARKFAC_STATE_ROOT/actionable_failures.json` conserva a falha mais recente de cada ticket:
fase, fingerprint, número de tentativas, última evidência, transição (`retrying` ou `blocked`) e
próximo passo. Ele é gravado antes do aviso. Hub/Telegram deduplicam por fingerprint e transição:
repetir a mesma evidência não avisa de novo; uma mudança persistida de evidência ou a passagem de
`retrying` para `blocked` pode gerar um novo aviso. O estado e o ledger de avisos sobrevivem ao
reinício.

Ao resumir um gate, a triagem usa a primeira linha real de `FAILED`, `ERROR`, timeout ou exceção.
Linhas de progresso `[SUITE_LOCK] acquired`/`waiting` e cabeçalhos `[TEST_PHASE]` não substituem
essa causa; `[SUITE_LOCK] timed out` permanece uma falha válida. Outcome, motivo do dispatcher e
detalhe encaminhado ao registro de falhas usam a mesma linha sanitizada.

## Orçamento por ticket e recuperação

`core.line.failure_taxonomy` é a fonte única das classes `infra`, `candidate`, `owner` e `unknown`.
Infraestrutura inclui gate sem RAM, indisponível, timeout, sem testes, base suspeita, worktree suja,
espera de rota/cota/trava, contexto, recurso, timeout do agente, permissões do host e rede/gh.
Essas falhas saem com 75 e nunca consomem `candidate_failures` nem criam hold por repetição.

O SQLite `admission.sqlite3` mantém `ticket_budget` por projeto e ticket. Infra incrementa
`infra_streak` e persiste `next_attempt_at`: 10, 20, 40, 80 e no máximo 120 minutos.
Reiniciar o dispatcher ou trocar host, fingerprint ou candidato não reduz essa espera.
Um novo SHA conhecido de `origin/main` limpa somente a sequência e a espera de infraestrutura.
Holds legados cuja causa é infraestrutura são removidos ao abrir o store.

Falhas `candidate` e `unknown` incrementam `candidate_failures`. A terceira cria um hold só desse ticket.
O alerta Hub/Telegram traz ticket, fase, fingerprint, tentativas, última evidência, próximo passo e
caminho do log. Uma nova evidência pode alertar novamente; a transição para `blocked` também é
notificada uma vez. Infraestrutura registra evidência e próximo passo, mas não alerta.
O `run_id` impede cobrança duplicada entre launcher, filho de entrega e dispatcher.
Mudar o main, o candidato ou o contexto não libera esse hold nem zera o contador.
Merge comprovado zera o orçamento e o limite de correção de CI. Fingerprints permanecem como
diagnóstico e identidade de reservas; não bloqueiam tentativas depois do backoff.

`rebase_conflict` é causa `candidate`. A recuperação reserva cada tentativa antes do reparo e cada
inferência antes da chamada ao harness. `rebase_recovery` persiste o resultado por fingerprint: HEAD,
candidato, upstream, branch, worktree, prompt do ticket, arquivos/estágios Git e evidências. A mesma
entrada não repete recuperação; mudança verificável abre uma tentativa controlada. Se o processo
reinicia durante um rebase, o launcher continua a worktree existente e trata uma reserva sem resultado
como interrupção técnica, sem repetir o trabalho. Sem rota elegível, `route_wait` não gasta inferência.
O hold guarda `rebase_conflict` e o comando de retomada, não cria `ticket_holds` nem marca
`waiting_human`. `reset_budget`, no merge comprovado, apaga as linhas desse ticket.

`pending_delivery.tick` usa o mesmo orçamento: falha infra reagenda, candidato conta até o limite.
Retorno zero sem conclusão espera `RECHECK_SECONDS` (900s), evitando relançamento imediato.
CI/revisão esgotados contam uma falha de candidato por tentativa, preservada entre relançamentos.

Sem execução em voo, com host comprovadamente livre e todos os pendentes retidos por hold,
backoff ou falha por mais de dois ciclos, o sentinela publica `stalled_blocked`, com causas
agregadas (`hold=3, backoff_infra=10`, por exemplo). A amostra leve também atualiza esse estado
sem sondar cotas. Há um alerta por episódio; uma retomada permite um novo episódio posterior.

O estado do dispatcher publica `heartbeat_epoch` para indicar que o controlador fez um ciclo e
`last_progress_epoch` para a última evidência útil. Só início recente de trabalho, despacho, log ou
heartbeat do filho, ou transição de etapa sustenta `activity=running`. Um processo apenas vivo e um
heartbeat do próprio controlador não contam como progresso. A evidência expira após
`progress_window_sec` (padrão 120 segundos), configurável por `DARKFAC_FLEET_PROGRESS_WINDOW_SEC`
ou `--progress-window-sec`; depois disso o estado é `idle` ou `blocked`. Esses campos são gravados
no JSON do dispatcher e preservados para a decisão após reinício. Hold, backoff, espera de entrega
ou falha de lançamento isolam o ticket afetado e deixam a fila independente elegível.

O recibo é produzido pelo dispatcher depois do deploy canônico e da prova HTTP
do SHA exato. Um marcador escrito pelo processo filho não libera a recuperação.
A entrega pendente fica em `pending_recovery_releases.json`, ao lado do estado do
dispatcher, e é retomada após reinício antes de admitir novos tickets. Validação,
revisão independente, merge e índice de learning packs vêm dos artefatos reais;
CI e saúde são consultados novamente. Falha no deploy ou na prova mantém a espera.

O SQLite também guarda reservas e a trava global de recuperação. Causas operacionais aparecem
em `idle_reasons`, `skipped` e `recovery.receipt_cause`; ausência de rota, worker indisponível,
reserva ativa e receipt incompleto são espera, nunca sucesso.

Na recuperação, há no máximo uma entrega em voo para toda a frota. A concorrência normal só volta após uma linha `[RECOVERY_RECEIPT] {JSON}` no log da entrega. O receipt deve ser real (`simulated: false`), recente (até sete dias), vinculado ao digest do candidato em cada estágio e incluir `[HARNESS_PASS]`, revisão independente aprovada, CI verde, SHA de merge alcançável, deploy desse SHA, smoke HTTP 2xx e learning pack indexado. Evidência ausente, stale, simulada ou de outro candidato deixa a trava ativa e registra a primeira causa recusada. O hash do receipt e o candidato recuperado ficam persistidos, tornando o retorno ao teto normal idempotente após reinício.

Para diagnosticar, consulte o estado do ciclo em `.factory/runs` e o SQLite de admissão em `DARKFAC_STATE_ROOT` (ou a raiz de estado configurada). `recovery.active=true` mantém a frota serializada; `recovery.receipt_cause` aponta o estágio que falta. Corrija a evidência ou altere de modo verificável o candidato/recurso e deixe o despachante retomar com backoff. Não apague reservas nem a trava manualmente.

## Status ativo sem processo (USR-498)

Ticket `implementing` ou `validating` sem processo da frota, sem trava de linha no proprio ticket, sem entrega pendente `running` e sem job aberto, parado ha mais de 24 horas, volta a `planned`. A causa fica numa linha nova de `delivery_evidence`: `stale_active cause=stale_active_no_liveness previous_status=<status anterior>`. O texto que ja estava na evidencia permanece, e a mesma linha nao e gravada de novo. A segunda varredura seguida nao altera o ledger.

A reconciliacao nao remove nem reescreve branch, worktree ou arquivo de trava. Run com alguma dessas evidencias de vida nao entra na lista. `completed` e `cancelled` continuam recusados por `requeue_ticket`. Cada acao emite uma linha de log `stale_active_reconciled` com o ticket, o status anterior, a idade em segundos e a causa.

O despachante chama esse sweep no início de cada ciclo, depois de recolher os jobs encerrados e antes de montar a fila (`select_tickets`). O limiar default é 24 horas. O ticket devolvido a `planned` nesse ciclo já pode ser despachado no mesmo ciclo. `--resume-only` não reconcilia e não admite ticket novo. `--dry-run` do despachante só lista a ação `would_requeue` e não grava o ledger. Ticket que está em `in_flight` neste despachante não é reconciliado. Se o sweep falha, o ciclo registra a ação `error` com a causa `stale_active_reconcile_failed` em `reconciled` e segue. As ações do ciclo ficam em `cycle["reconciled"]` e em `.factory/runs/fleet_dispatcher_state.json`.

```powershell
python -m core.line.stale_active --dry-run
python -m core.line.stale_active --max-actions 25 --idle-hours 24
```

`--dry-run` so lista o que seria devolvido a `planned` e nao grava o ledger. `--max-actions` limita quantos tickets um sweep altera (padrao 25). O mesmo comando de dry-run vale fora do despachante, para ver o sweep sem esperar o ciclo.

## Gancho do sentinela da linha (USR-439)

O despachante contínuo integra o gancho do sentinela (`sentinel_hook`) executado em `self.run_sentinel_hook()` a cada ciclo de 30 segundos, delegando a `maybe_run_sentinel()`. A sonda de rotas e cotas fica na janela de 45 minutos (2700s) ou em `--force`. Dentro dessa janela cada ciclo só amostra a fila: atualiza `idle_since_epoch` com o estado do despachante (`free_hosts`, `in_flight`, `queue_remaining`), a rota já gravada e `running_fleet_runs`, sem `pick` nem `read_quotas`. Estado do despachante ausente não faz `git fetch` nem inventa ticket. USR-501 acrescenta o achado `idle_queue`: host livre, rota e ticket elegível sem execução há mais de 15 minutos alertam uma vez por episódio no Hub e no Telegram. Na mesma amostra, o achado `status_without_process` alerta uma vez quando um status local ativo ou running (in_flight do despachante, pending_delivery running ou reserva active em host da frota) não tem processo em `running_fleet_runs` e o heartbeat passou de 120 segundos. Processo vivo ou heartbeat de até 120 segundos não alerta. Run de nuvem fora desses hosts não entra. O sentinela só registra o achado: não grava reserva, entrega pendente, trava nem estado do despachante. USR-501 T4 acrescenta `stale_lock`: `line.lock` e `line.fleet-*.lock` alertam uma vez por episódio, no Hub e no Telegram, quando `started_at` é anterior ao committer time do `merge_sha` em `merge.json` do mesmo ticket. Trava posterior a esse merge, merge ausente ou ilegível e merge de outro ticket ficam em silêncio. Git sem committer time não usa o mtime do arquivo, e o alerta não apaga a trava. A amostra de 15 minutos, ainda sem `pick` nem `read_quotas`, avalia `idle_queue`, `status_without_process` e `stale_lock`. O briefing lista `idle_queue`, `active_without_process` e `stale_lock` quando esses achados estão presentes.

USR-473: espera de deploy exige SHA do recibo de merge e evidencia coincidente
do candidato validado e aprovado. Recibo inconsistente permanece visivel como
falha de verificacao. O smoke registra o SHA do ticket e a versao saudavel que
o contem, sem substituir identidade do candidato.

USR-473: identidade de implementacao ausente e provas de deploy ausentes ou
inconsistentes ficam bloqueadas com causa persistida. Contencao do mutex de
retomada impede novas admissoes naquele ciclo; nenhuma cota e consumida em espera.
USR-468: admissao remota combina `/health` (ocupacao) com `/metrics/hardware`
(RAM, CPUs e instante de coleta). Saude generica nunca vira capacidade; RAM
insuficiente continua zero. Corretores de falhas tambem registram sua familia
para impedir revisao pelo mesmo provedor de qualquer implementador.

USR-468 integra o controlador de retomadas USR-473 ao breaker de admissao.
O candidato composto exige revisao independente e portao integral proprios;
rodadas recusadas do USR-473 permanecem preservadas. Completed sem recibo de
deploy nao remove espera; uma retomada ativa impede readmissao do mesmo ticket
mesmo que o inventario externo de processos esteja vazio.

## Recuperacao integrada USR-477

O corretivo preserva as revisoes recusadas de USR-473/468. Completed sem report
de deploy usa merge, gate e revisao coincidentes para retomar o deploy; prova
ausente ou divergente permanece bloqueada. Falha do deploy fica no historico
com codigo e proxima tentativa, sem supervisor LLM. Runtime do harness nao entra
no candidato. O revisor pago usa o alias sonnet, de familia diferente dos
implementadores; familias tambem reconhecem os ids de gateway do registro. A
mudanca responde aos JSONs truncados e achados contraditorios da rota anterior,
sem aumentar rodadas, cota ou orcamento. Resultado real ainda exige gate e deploy.

USR-477: saida zero sem entrega em recovery registra falha e causa persistida.
Retomada de deploy exige prova da familia independente. Se a validacao for
corrompida apos merge, o recibo preserva SHA/PR reais sem digest autorizado;
a ausencia desse digest bloqueia release ate a recuperacao da evidencia.

## Encerramento sem entrega e recuperacao de CI (USR-479)

Retorno zero sem prova de merge conta uma falha desconhecida no orçamento do ticket.
A entrega pendente aguarda 900s antes de reavaliar conclusão; o limite de três falhas continua valendo.
A correcao de CI e unica por tentativa: o contador permanece consumido ao
retomar, inclusive depois de gate e revisao verdes. Retomar a entrega segue
gate, revisao independente, novo CI e merge; BUILD nao e repetido. O teste
comportamental cobre esse ciclo inteiro na mesma worktree. Revisoes recusadas
de candidatos anteriores permanecem preservadas; o novo candidato exige suas
proprias provas de portao, revisao e producao.


### Retomada e prioridade ap?s os bloqueios USR-484

Com recovery global ativo, a lista ordena tentativas e mant?m no m?ximo uma entrega em voo.
Um ticket em hold ou em backoff cede a vez ao pr?ximo da lista e depois ? fila regular.

A retomada direta de um BUILD exige status Git leg?vel e limpo (exceto o ledger), commit al?m da base e
`build_binding.json`: HEAD exato e hashes de SPEC, tickets, pesquisa e progress. A entrega grava esse
v?nculo ap?s sincronizar o candidato. Artefatos antigos sem v?nculo, novo HEAD, artefato alterado ou
falha de `git status` seguem PLAN/BUILD, sem reaproveitar a prova antiga.

Saida com codigo 75 (route_wait, no_build_route) e espera estruturada: aplica backoff persistido por ticket, mas nunca ativa a recuperacao global da frota. O mesmo vale para recusa por trava de linha ou reserva de outro ticket vivo (kind lock): fica registrada por ticket, sem recuperacao global.

A recusa `[RESERVA DE FROTA]` (teto de frota atingido) conta como trava, nao como falha: aplica backoff infra sem consumir candidato nem acender a recuperacao global. O mesmo vale para a recusa `ja possui PR ativo` (ticket com PR aberto, codigo 2).

O laço do despachante publica as cotas no Hub a cada 5 minutos por `core.usage.sync_task.publish_once()`, sem sonda paga. Falha de publicação fica só no log e não interrompe o ciclo.

Causas de entrega (USR-475): `rebase_conflict` e falha do candidato; `merge_blocked` e infra e espera com backoff sem gastar tentativa.
A recusa da etapa DOCS (docs-sync) sai como `docs_sync_failed`, causa de candidato que gasta orçamento; o texto do docs-sync cita nomes de arquivo e nunca é inferido como infraestrutura. Só `fix_commit_failed` de rede ou permissão vira infra. `stage_artifact_missing` também é de candidato.

Relancamento pelo despachante (USR-447): se a worktree preservada do ticket tem commits a frente de `origin/main` e plano valido (`SPEC.md` e `tickets.json` na worktree ou em `state_root()/artifacts/ticket_runs/<id>/.darkfac/runs/<id>`), `default_launch` acrescenta `--resume-delivery --worktree <caminho>` ao comando da frota; sem isso o ticket e relancado inteiro.

A varredura diaria de worktrees orfas (`maybe_run_scheduled`) reside no dominio `core/git/orphan_worktree_cleanup.py` consumido por `core/line/fleet_dispatcher.py`, enquanto `scripts/cleanup_orphan_worktrees.py` permanece apenas como launcher CLI (sem imports de `scripts` em `core/`, `USR-531`).
# Prioridade e continuidade de entrega

Na seleção regular, o dispatcher ordena tickets elegíveis por prioridade numérica decrescente. Empates preservam a ordem de criação e, em seguida, o ID, mantendo o resultado estável. A prioridade não altera dependências, reservas, bloqueios, faixas de recuperação ou o modo `resume-only`.

Após um merge que ainda aguarda deploy, o dispatcher grava a entrega em `pending_deliveries.json`, vinculada ao ticket, run e digest do candidato. Um processo novo recupera esse diário e conclui o deploy pendente antes de admitir trabalho regular. Só um deploy canônico bem-sucedido marca a entrada como entregue e permite anexar recibo/evidências; falhas preservam a espera para uma nova tentativa.
