<!-- covers: run_ticket.py, core/line/stages.py, core/line/ticket_engine.py, core/line/ticket_stages.py, core/line/ticket_extras.py, core/line/route_wait.py, core/line/admission.py, core/line/review_payload.py, core/line/stage_review.py -->
# Motor da esteira

O launcher prioriza o `[HARNESS_RESULT]` final ao classificar um gate falho
(USR-452). Com testes descobertos e saída1 da suíte, mensagens de timeout ou
worker indisponível citadas dentro de um traceback não autorizam repetir a suíte
como falha de transporte. Timeout real continua identificado pela saída124.
Na entrega em recuperação (USR-468), `merge.json` preserva o digest do candidato
validado. A revisão grava as famílias efetivamente utilizadas em
`review_execution.json`. O dispatcher usa esses vínculos após o deploy para
construir o recibo; não aceita um recibo arbitrário emitido em log pelo filho.

## Wake do grokbox no preflight (USR-456)

`run_ticket.py` sempre consulta `/health` do grokbox no preflight. O email de wake só é enviado quando a
execução recebe autorização explícita (`--allow-grokbox-wake-email`); texto livre, variáveis de ambiente e
autorização de outros canais não concedem essa permissão. Sem autorização, a sonda e o launcher seguem
normalmente e o log registra `provider=grokbox_wake_email outcome=suppressed` com causa estruturada.

O throttle de 30 minutos usa `.factory/grokbox_wake/state.json` no checkout comum a todas as worktrees
ligadas ao mesmo repositório. `DARKFAC_STATE_ROOT` explícito continua apontando para a raiz configurada,
inclusive em testes e execuções isoladas. Um lock entre processos cobre leitura, decisão, reserva do
intervalo, envio e gravação. A reserva é persistida antes do envio para manter a garantia de no máximo
uma tentativa por janela mesmo quando o provedor aceita o email e a resposta expira. Raiz compartilhada
indisponível, estado ilegível, gravação impossível ou lock indisponível suprime o email
(`shared_state_root_unavailable`, `state_unreadable`, `state_write_failed` ou `lock_unavailable`);
sondagem e launcher continuam operantes.
 
## Retomada por rota (USR-473)

Em `route_wait`, o launcher grava a etapa e a worktree em
`.factory/pending_delivery/` antes de sair. O dispatcher existente retoma a entrega
automaticamente, mesmo depois de reiniciar. REVIEW continua na mesma worktree pelo
`--resume-delivery`, preservando gate válido para o candidato. A regra de contingência
OpenRouter e o modo de recuperação sem novos BUILDs estão em [frota.md](frota.md).

## Admissão antes da worktree (USR-468)

`_stage_preflight` chama `evaluate_admission` (`core.line.admission.admit`) depois da estimativa de
tamanho e antes de `_stage_route`, do dry run, da worktree e do PLAN. A consulta é somente leitura:
`pick` com `reserve_quota=False`, sem agente e sem arquivo de candidato.

A decisão traz `outcome` (`allow` ou `wait`), `code`, `stage`, `resource`, `observed_at`,
`evidence_ref` e `not_before` (`null` quando o outcome é `allow`).

Sem rota de `planning`, de `development` ou de `review`, ou sem worker de gate utilizável, o launcher
encerra com exit 75 (`EXIT_ROUTE_WAIT`). O stdout recebe um JSON com `ok` false, `cause` `route_wait`,
`error` igual ao `code`, `ticket_id`, `exit_code` 75 e os campos da decisão. O stderr recebe
`[AGUARDANDO]` e `harness_infra_error: route_wait`. O ticket volta a `planned` quando estava
`discovered`, `accepted`, `implementing` ou `validating`. Um ticket que já estava `planned` permanece
na fila. A worktree não é criada e o dry run não imprime `[DRY RUN]`.

A revisão prospectiva usa outro harness que a rota de build (o nome do harness, sem distinção de
maiúsculas). Uma rota de mesma família sem nível de contingência produz `review_same_family`; a cascata
do USR-488 (`docs/manual/roteamento.md`) tenta antes outra família, OpenRouter, outro modelo e persona. O harness que escreveria é o pin `--harness`
quando ele declara escrita; em contexto headless o pin Antigravity continua fora da escrita e a revisão
é comparada com o harness que o `pick` de `development` elegeu. Um pin não inventa rota quando esse
`pick` devolve `None`.

O gate conta como capacidade com `workers` inteiro maior que zero, `busy` false e instante de
observação com menos de 90 segundos. As causas de espera são `gate_capacity_zero` (`workers` <= 0),
`gate_worker_busy` (`busy` true), `gate_worker_stale` (90 segundos ou mais), `gate_capacity_absent`
(coleção vazia) e `gate_health_generic` (faltam `workers`, `busy` ou o instante, inclusive um body
`{"status": "ok"}`). `status` não entra na conta. Um worker utilizável na coleção libera o gate. Entre
falhas, a causa segue a ordem zero, busy, stale, genérico: coleção vazia e health genérico permanecem
espera e não cobrem um zero, um busy ou um stale.

`DARKFAC_REMOTE_HARNESS=off` não tira os workers da leitura da admissão: o modo off só
impede o despacho do portão. A fonte é `DARKFAC_ADMISSION_WORKERS` quando a variável está
definida, mesmo vazia; senão `DARKFAC_TEST_WORKERS` quando definida; senão o default de
`worker_urls`. URL em `DARKFAC_FLEET_DISABLED_HOSTS` não é sondada. Health cujo `node_id` ou
`hostname` cai em host desabilitado sai da coleção e não admite. `CI` e
`DARKFAC_HARNESS_WORKER_JOB` continuam com lista vazia. Worker local com `workers` 0 não
bloqueia a frota quando um remoto está livre (`workers` > 0, `busy` false, observação com
menos de 90 segundos): o `evidence_ref` do gate nesse allow é `gate://<host>` desse remoto.
Health só `{"status": "ok"}` não cobre o zero local; sem worker utilizável a espera segue
`gate_capacity_zero` com `gate://local`.

`allow` não é gravado. `_stage_route`, o PLAN, cada fatia de development e o REVIEW chamam `pick` de
novo na leitura daquele momento.

## Retomar candidato apos falha de entrega (USR-484)

Depois de uma falha na entrega, o launcher pode reutilizar a worktree preservada
quando ela contem commits alem da base e os artefatos de PLAN, RESEARCH e BUILD sao
validos. O ledger do ticket pode ser a unica diferenca local. Nesse caso, a retomada
segue para a entrega sem repetir PLAN ou BUILD. Worktree suja em outro caminho,
ausencia de commit ou artefato invalido mantem o fluxo normal de planejamento e
implementacao.

## Sequencia canonica de estagios (USR-375 S2)

`core/line/stages.py` define a ordem: INTAKE, GRILL, PLAN, RESEARCH, BUILD, VALIDATE, REVIEW, MERGE, DEPLOY_SMOKE, DOCS, LEARNING.

Cada estagio deixa um artefato em `.darkfac/runs/<run_id>/`, validado por `require_artifact(stage, ws)`:

| Estagio | Artefato | Modelo |
|---|---|---|
| INTAKE | `DEMAND.md` | texto nao vazio |
| GRILL | `GRILL.md` | texto nao vazio |
| PLAN | `SPEC.md` + `tickets.json` | `PlanningPlan` |
| BUILD | `progress.json` | `BuildReport` |
| VALIDATE | `validation.json` | `ValidationReport` (`passed` true) |
| REVIEW | `review_verdict.json` | `ReviewVerdict` (`approve`, sem bloqueios) |
| MERGE | `merge.json` | `MergeReceipt` |
| DEPLOY_SMOKE | `deploy_report.json` | `DeployReport` (`smoke_ok` true) |
| RESEARCH | `research.json` | `ResearchArtifact` (`research_skipped` com motivo, `research_done` ou `research_reused` com fontes) |
| DOCS, LEARNING | opcionais | retornam `None`: nunca bloqueiam uma entrega ja mesclada |

Artefato ausente, vazio ou invalido levanta `StageArtifactMissing` (com `stage` e `reason`). O contrato esta ligado ao `run_ticket`, ao `fleet_dispatcher` e a linha (slice S4, abaixo).

## run_ticket executa a sequencia canonica (USR-375 S4)

Nao existe mais caminho direto agente -> portao -> merge. O `run_ticket` (e o `fleet_dispatcher`, que so
lanca `run_ticket.py <ID> --fleet`) executa os estagios em ordem e chama `require_artifact` antes de avancar:

1. **INTAKE** grava `DEMAND.md`; **GRILL** grava `GRILL.md` com as decisoes do intake (G1 semantico, S6).
   Correcao pequena (`estimate_ticket_size == small`) pula SOMENTE o GRILL.
2. **PLAN** chama `stage_planning.run_planning` (modelo de nivel alto) e grava `SPEC.md` + `tickets.json`;
   correcao pequena grava um plano minimo (um sub-ticket igual a demanda), sem agente.
3. **RESEARCH** (USR-376, entre PLAN e BUILD) chama `research.policy.run_research_stage` com o ledger
   `.factory/research/` do checkout principal e grava `research.json`. Sem busca web deterministica neste
   processo o estagio registra `research_skipped` com o motivo "busca web indisponivel neste host"; nunca
   inventa fontes. A politica (R1..R7) dispensa correcoes, docs e mudancas pequenas. Sem `research.json`
   valido o ticket nao chega ao BUILD nem a entrega (`_require_plan_and_build` exige PLAN, RESEARCH e BUILD).
4. **BUILD** roda o agente de desenvolvimento uma vez por sub-ticket do plano, em sequencia, na mesma
   worktree, e grava `progress.json` (`harness` = familia do implementador).
   Antes de cada fatia, o launcher relê a cota e chama `pick` de novo (`development`, modo `write`),
   sem override e sem a tupla do preflight (USR-428). Um plano de uma fatia, inclusive o plano mínimo
   de correção pequena, faz esse pick uma vez. Um plano de várias fatias repete esse pick antes do
   agente de cada uma; a leitura pode trocar o harness write (Codex acima de 15% numa fatia, Claude
   na seguinte quando o Codex está no piso de 15%). Com `--fleet`, o pick da fatia usa `execution_context`
   headless: Antigravity não inicia o agente mesmo como harness de operação, com Google acima de 15%
   e os demais no piso.
   `--harness` só segue naquela fatia se a leitura nova tiver headroom conhecido acima de 15%; 15.0, abaixo
   ou desconhecido descarta o pin e usa o pick fresco. O modelo passado ao agente é o declarado para esse
   harness na etapa development, não o modelo de outro harness que o pick fresco tenha elegido.
   `--force` e a autorização no prompt não iniciam um harness que esse pick recusa. Se o pick de uma fatia
   devolve `None`, essa fatia não chama o agente, a entrega não começa, o processo sai com 75
   (`EXIT_ROUTE_WAIT`) e o JSON traz `cause` `route_wait`, `stage` `BUILD` e o caminho da worktree. O
   diretório permanece, com o arquivo que o PLAN gravou nele e com o que as fatias anteriores já gravaram.
   `_discard_untouched_workspace` e `_report_workspace_fate` não rodam nesse caminho e portanto não removem
   a worktree. O ticket volta a `planned` se estava `implementing` ou `validating`; um ticket que já estava
   `planned` permanece na fila.
5. Na entrega (`--resume-delivery`, processo novo): exige PLAN, RESEARCH e BUILD. O id e resolvido nesta
   ordem: arquivo do ledger da worktree; fila duravel do control store (mesmo id, uma linha materializada
   no ledger da worktree, sem `next_ticket_id` e sem o agente de correcao); arquivo do checkout
   compartilhado (segue para a entrega sem gravar a worktree e sem criar linha na fila); `origin/main`
   (o rebase posterior traz a linha). Id ausente nos quatro devolve exit 1, `ok=false`,
   `cause=ticket_absent` e `error` no formato `Ticket <ID> not found in demands store`. O stderr cita o
   ledger da worktree, a control store, o checkout compartilhado e `origin/main`. O ledger e o
   `control.db` permanecem como estavam. Nao copie o ledger compartilhado para a worktree so para a
   entrega passar.
   **VALIDATE** grava `validation.json` (`ValidationReport`, campo `candidate_digest`) depois do portao.
   No resume do projeto `darkfac`, um `validation.json` ja `passed` cujo `candidate_digest` coincide com
   o candidato atual permanece e o portao nao roda, tambem quando so a politica de revisao mudou.
   Digest vazio permanece somente quando a ultima rodada de `review_state` e `approve`,
   `approval_binding_valid` aceita esse candidato e essa politica, e `forced` e false. Digest diferente,
   `passed=false`, veredito diferente de `approve` ou digest vazio sem esse binding apagam
   `validation.json` e o portao roda. O veredito so entra nessa decisao quando o digest gravado esta
   vazio: um digest coincidente permanece mesmo se a ultima rodada nao for `approve`. **REVIEW** roda `stage_review.ReviewStage`
   com familia diferente do implementador e grava `review_verdict.json` so na aprovacao. Aprovacao
   vigente (ultima rodada `approve`, binding valido, sem `forced`) nao chama o agente revisor:
   `review_verdict.json` approve e mantido ou reescrito como `approve`. Mudanca de candidato (digest
   diferente) segue o fluxo normal de gate e revisao. Mudanca so de politica, com digest coincidente,
   nao reexecuta o portao e a revisao segue o fluxo normal. O prompt leva o `git diff` inteiro, grava
   os mesmos bytes em `candidate.diff` na arvore do revisor e lista cada arquivo alterado.
   `changes_required` reinvoca o agente, comita e repete o portao.
   **MERGE** grava `merge.json` (PR e SHA); **DEPLOY_SMOKE** grava `deploy_report.json`. Na DarkFac o deploy
   pos-merge continua unico, pelo `fleet_dispatcher`/`scripts/dokploy_redeploy.py`, entao o relatorio sai com
   `deployed=false` e `smoke_ok=null`: `require_artifact` aceita isso SO como "deploy pendente" e o ticket e
   reportado como `merged_awaiting_deploy` (`[MERGE] ... aguardando deploy`), nunca como entregue. Um
   `smoke_ok=null` com `deployed=true` e invalido. Sem plano ou veredito nao ha merge; sem relatorio de deploy
   a execucao falha.
   O fechamento pos-merge remove apenas a worktree da propria entrega (responsabilidade do ciclo Git) e nao
   executa varredura global de worktrees. O launcher grava o resumo final com URL do PR, SHA de merge,
   estado do deploy e caminho de `deploy_report.json`; `merge.json` e o relatorio ficam no state root, fora
   do checkout removivel. Deploy ainda pendente encerra como `merged_awaiting_deploy`, sem declarar entrega
   completa.
   **DOCS** (`_stage_docs_sync`, so projetos internos) roda `core/harness/docs_sync.py` sobre o diff
   `origin/main...HEAD` depois do portao e antes do REVIEW: se uma pagina do manual ficou desatualizada, o
   agente de desenvolvimento e reinvocado uma vez com a mensagem (mesmo padrao da autocorrecao do portao);
   persistindo a violacao, a entrega e recusada (`stage_docs_refused`). Falha de infraestrutura do
   controle de versao apenas registra aviso. Alem do docs-sync, o mesmo passo roda o ratchet de
   complexidade e o `lint-imports` e devolve ao agente uma vez com a saida combinada (USR-398); cada checagem so roda se a ferramenta existir no repositorio do ticket (`scripts/complexity_ratchet.py`, `.importlinter`). **LEARNING**: o learning pack
   (`learning_pack.delivery.build_for_delivery`) e calculado antes do merge e, depois do merge
   bem-sucedido, gravado em `learning_pack.json` e no `LearningPackStore` (`.factory/learning_packs/`,
   `packs_index.json`), de onde o Hub (`/api/learning-packs/<id>/html`) e o resumo diario o leem; falha na
   geracao ou na gravacao so e registrada e nao derruba a entrega.
6. **Sem rota elegivel** (nenhum modelo de nivel alto para PLAN, nenhum revisor de outra familia para REVIEW):
   o resultado do `RouteWaiter` (`no_route_available not_before=...`) vira espera, nao falha. O `run_ticket`
   sai com o codigo 75 (`EXIT_ROUTE_WAIT`, de `fleet_dispatcher.INFRA_EXIT_CODES`), com o marcador
   `harness_infra_error: route_wait`; o despachante reenfileira com cooldown e o ticket continua `planned`
   (no REVIEW a worktree e preservada para o `--resume-delivery`).

Os artefatos do ticket ficam fora da worktree (que o merge remove), em
`<state_root>/artifacts/ticket_runs/<ID>/.darkfac/runs/<ID>/` (`core/line/ticket_engine.py`). Uma execucao
nova do launcher normalmente apaga os artefatos anteriores. A entrega apaga `merge.json` e o relatorio de
deploy do attempt anterior mesmo quando `validation.json` e reaproveitado. `validation.json` `passed` com
o mesmo `candidate_digest` fica, inclusive se so a politica de revisao mudou. Digest vazio fica so quando
a ultima rodada de `review_state` e `approve`, `approval_binding_valid` aceita o candidato e a politica, e
`forced` e false. Digest diferente, `passed=false`, veredito diferente de `approve` ou digest vazio sem
esse binding apagam a validacao e o portao roda de novo. Veredito diferente de `approve` apaga so o
digest vazio; digest coincidente fica. Aprovacao vigente nao chama o revisor
(`review_verdict.json` approve e mantido ou reescrito). Mudanca de `candidate_digest` roda o portao e a
revisao de novo.
Excecao de retomada (USR-426): se a worktree preservada estiver limpa, PLAN, RESEARCH, BUILD e VALIDATE
forem validos, VALIDATE estiver verde e `review_state.json` registrar resposta JSON invalida para o mesmo
digest de candidato e politica de REVIEW, o launcher preserva esses artefatos e retoma sem reinvocar BUILD.
A entrega reexecuta a validacao quando o `candidate_digest` nao cobre o candidato atual, quando
`passed` e falso, ou quando o digest esta vazio e a revisao nao aprova esse candidato. Com digest
coincidente, o portao nao roda de novo. Quando o portao roda, o runner so reutiliza o veredito cacheado
se o hash da arvore, a configuracao e as etapas selecionadas ainda correspondem. Worktree suja,
digest/politica diferentes ou artefato ausente/invalido seguem pelo fluxo normal de planejamento e desenvolvimento.
Um `--resume-delivery` digitado pelo operador numa worktree anterior ao S4 (sem nenhum artefato) registra
plano/build minimos e segue por VALIDATE, REVIEW e MERGE.

Na linha, `bindings.IntegrationStageAdapter` recusa a integracao (`stage_artifact_missing:<ESTAGIO>`) sem
`SPEC.md`/`tickets.json` e sem `review_verdict.json`, que o `ReviewStage` passou a gravar ao aprovar.

### Transporte do veredito independente

O estágio REVIEW envia ao Claude Code o schema JSON de `ReviewVerdict` com `--json-schema` e consome
o campo `structured_output` do envelope JSON da CLI. Chamadas sem schema mantêm o tratamento legado
do campo `result`. O parser da revisão exige exatamente um objeto JSON compatível (sem prosa, cercas,
chaves duplicadas ou campos extras); uma resposta inválida retorna `review_invalid_json` e registra
`review_invalid_response.json` com harness, motivo, tamanho em bytes, SHA-256 e trecho sanitizado limitado.
O motivo distingue erro de sintaxe JSON, chave duplicada e falha de schema.
Esse diagnóstico não é um veredito e não cria `review_verdict.json`. A aprovação exige `verdict=approve`
sem itens bloqueantes e continua vinculada ao digest do candidato e à política de revisão.

Uma falha de formato registra o harness como inelegível para a combinação atual de `candidate_digest` e
`policy_digest`. O retry pede outra rota ao roteador, mantendo o modo `read`, o limite de cota e a família
independente já aplicados por `pick()`. Se não houver outra rota elegível, REVIEW aguarda a próxima rota;
nenhum veredito é criado. Uma alteração no candidato ou na política descarta a exclusão anterior e exige
uma nova revisão independente.

### Payload verificável da revisão (USR-474 T1)

`core.line.review_payload.build_review_payload` lê, sem rede e sem agente, o `SPEC.md`, os critérios de `tickets.json` (`acceptance`, `acceptance_criteria` ou `criteria`) e os bytes dos arquivos alterados dentro da worktree. Os arquivos entram em ordem de caminho, independente da ordem recebida. Cada item do manifesto traz o caminho relativo, o sha256 da fonte, os bytes incluídos depois de `redact_secrets` e se houve corte. A redação ocorre antes do corte. O limite por item e o total contam bytes UTF-8 e param num caractere inteiro. SPEC ou tickets ausentes, vazios, inválidos ou ilegíveis deixam `complete` falso e registram o motivo. Se o corte deixar a SPEC só com espaço em branco, o motivo é `spec_not_included`. Contexto vazio não sai pronto. Caminho absoluto, travessia para fora da árvore e duplicata não são lidos fora da raiz.

### Revisão paga sem filesystem (USR-474 T2)

O revisor com filesystem continua recebendo o diff integral no prompt e os mesmos bytes em `candidate.diff` na worktree. O OpenRouter não lê disco: `provider.generate` recebe no prompt os critérios, o trecho sanitizado e os sha256 do manifesto. A SPEC sanitizada entra uma vez; `review_context` deixa de repetir o `SPEC.md` cru nesse transporte. SPEC ou tickets ausentes, vazios ou ilegíveis devolvem `review_context_unavailable`, sem chamar o modelo e sem gravar `review_verdict.json` com approve. A revisão continua recusando a mesma família do implementador e não escreve nem apaga `validation.json`. O `ticket_id` do `generate` é o `run_id` quando ele é um ticket `USR-N`; senão vem de `DARKFAC_TICKET_ID`. Sem um desses dois, o evento de uso fica nulo. O texto do prompt e o corpo da resposta não são lidos para descobrir o ticket. A resposta normalizada guarda `finish_reason`, a contagem `completion_tokens` e `truncated`. `truncated` fica verdadeiro quando `finish_reason` é `length` ou quando `completion_tokens` chega a 4096. Esse caso devolve `review_output_truncated` e nunca aprova. Não trate uma resposta cortada como um veredito aproveitável; uma nova tentativa precisa receber saída completa.


### Negação ao alterar o motor (USR-494 T2)

Self-Modification, ou `permission denied` em `core/line`, `core/harness`, `run_ticket.py` ou `scripts/line_validate.py`, grava a causa `self_modification` e a tag `line-block` no control store. A mesma gravação não duplica tag nem causa. O launcher imprime `[ERRO NA ENTREGA] causa: self_modification` e o run não relança essa edição. `protected_path` e `permission denied` de banco ou ACL seguem nas causas deles. O classificador lê a causa na linha oficial, não na cauda do log. A parada abre uma ação do owner com um único comando, `python -m core.line.express_lane run --ticket <ID>` (`core/line/express_lane.py`). Registrar de novo devolve a ação aberta. O comando lista todas as falhas do host, inclusive o piso de cota, e não lança o ticket enquanto faltar alguma. Com o host válido, lança `run_ticket.py <ID> --fleet --host-id <host>`. Ticket já em voo ou terminal termina sem outro processo.

## Planejamento

Rotas de planejamento: Claude (role `planning.claude`) ou Codex (role `planning.codex`), ambos com modelo tier `high` no registro.
O Codex roda com esforco `high` (pin do estagio) e o slug do CLI vem do `gateway_ids` do modelo no registro.

O estagio `planning` so roteia para modelos de nivel alto (`tier` `high` ou `frontier` em
`.factory/config/model_registry.json`, lido por `core.models.registry`). Um harness sem modelo
explicito (padrao do CLI) nao tem nivel declarado e nao e elegivel. O fallback do OpenRouter
(modelo barato) tambem e desligado para este estagio (`routing.HIGH_TIER_ONLY_STAGES`).

Sem modelo alto disponivel (cota, cooldown ou capacidade do host), `pick()` devolve `None` e o estagio
espera pela semantica de `core/line/route_wait.py` (`retry` com `not_before`, e `waiting_human` somente
apos o orcamento de relogio). Nunca ha queda para modelo barato e nenhum agente e invocado.

## Contrato Python antes de PLAN (USR-489)

`_stage_plan` chama `enforce_manual_host_contract` (`core/orchestrator/manual_host_preflight.py`) antes de INTAKE, GRILL e PLAN. Python anterior a 3.12, modulo ausente ou `requirements.txt` ilegivel encerra com exit 2 e nao chama PLAN. As dependencias sao as linhas desse arquivo, o mesmo que o portao instala; `import-linter` e conferido como o modulo `importlinter`. A mensagem traz `host_preflight_failed` e o comando de correcao (`py -3.12 -m pip install -r requirements.txt` no Windows, ou `pip install` da distribuicao ausente no interpretador selecionado).

## Estrutura do run_ticket (USR-375 S3)

`run_ticket.py` e o launcher headless; `_run` agora so orquestra estagios, cada um com no maximo 150 linhas:
`_stage_line_lock`, `_stage_check_harness`, `_stage_intake`, `_stage_preflight` (sonda do grokbox, tamanho,
admissão de rotas e do gate, `_stage_route`, dry run), `_stage_workspace`, `_stage_build` (agente + retry), `_stage_deliver` (subprocesso
`--resume-delivery`). O portao de autocorrecao e dividido em `_gate_focal_step`, `_gate_official_step`
(`_gate_run_official`, `_gate_classify_failure`, `_gate_reinvoke_for_gate`) e `_gate_exhausted_result`, e a
entrega em `_deliver_sync`, `_deliver_external_project` e `_deliver_worktree`.

Antes da rota e da criacao de worktree, `_run` adquire a guarda canonica do ticket e consulta os PRs abertos
do repositorio. Se encontrar um PR que mencione o ticket exato no branch, titulo ou corpo, a execucao termina
com recusa estruturada `active_pull_request` e referencia ao PR existente. Se a consulta ao GitHub falhar,
tambem recusa com `pull_request_lookup_unavailable`; nao inicia uma segunda implementacao sem confirmar o estado.
O intake da linha aplica a mesma verificacao e consulta a propriedade manual persistida antes de aceitar uma nova
tentativa. Assim, o PR continua protegendo o ticket mesmo depois que o processo que abriu o PR encerra.

`core/line/ticket_stages.py` guarda os helpers sem dependencia de globais substituidas pelos testes
(construtores de prompt, comando/ambiente da entrega, payloads e relatorios de falha, `GateState`).
O fluxo que chama `pick`, `inspect_quotas`, `_run_bounded` etc. fica em `run_ticket.py` para que os
`monkeypatch` existentes continuem valendo. Refatoracao pura: CLI, codigos de saida e mensagens inalterados.

## Conflito de rebase na entrega (USR-412)

Em `_deliver_sync` o rebase em origin/main deixa o conflito em andamento (`keep_conflict=True`) e `resolve_rebase_conflict`
o trata: conflito so em ledger (`demands.json`, `owner_actions.json`) fica com a versao de origin/main e o rebase
continua; qualquer outro arquivo volta UMA vez ao agente de desenvolvimento (`_invoke_fix_agent`, estagio
`rebase-conflito`). Se seguir em conflito, o rebase e abortado, o ticket permanece em `validating`, com evidencia de falha tecnica, e o run sai com rc 1
(causa `rebase_conflict`), sem rc 2 e sem redespacho do despachante.

## Harness de Operacao Interativo e Restricao Headless (USR-433)

`run_ticket.py` detecta o harness de operacao ativo (`DARKFAC_OPERATING_HARNESS` ou contexto do terminal). Se for passado `--fleet` com `--harness antigravity`, a execucao e recusada na inicializacao (`_stage_check_harness`) antes de qualquer efeito colateral (`[ERRO] Antigravity nao e elegivel para desenvolvimento headless/frota.`). O briefing agnostico de sessao `python -m core.ops.briefing` e o handoff versionado `docs/operacao/HANDOFF.md` sincronizam o estado entre sessoes.

## Rotacao de workers remotos com hosts desabilitados (USR-430)

Durante a validacao pelo portao oficial, `run_ticket.py` rotaciona os workers remotos quando uma tentativa falha (`_rotate_gate_workers`). Essa rotacao filtra estritamente hosts listados em `DARKFAC_FLEET_DISABLED_HOSTS` (ex.: `desktop` ocupado com indexacao), garantindo que nos desabilitados nao sejam reinseridos no pool ativo de validacao.


## Recuperacao de CI sem repetir BUILD (USR-473)

CI vermelho permite uma correcao tecnica na mesma worktree. O orcamento fica em
`ci_recovery.json` e sobrevive a retomadas. Uma alteracao retorna codigo 75 e
retoma a entrega pelo portao completo e pela revisao independente do novo candidato;
nenhum push ou merge da correcao acontece antes dessas provas. Sem rota, a worktree
fica preservada para retomada automatica. Esgotar essa tentativa registra falha
tecnica, reabre o ticket em `planned` com causa `ci_recovery_budget_exhausted` e
comenta o PR aberto com a causa e o ID do ticket. A falha ao comentar nao impede
a reabertura; a espera de CI termina sem exigir evidencia de deploy.

USR-473: espera de deploy exige SHA do recibo de merge e evidencia coincidente
do candidato validado e aprovado. Recibo inconsistente permanece visivel como
falha de verificacao. O smoke registra o SHA do ticket e a versao saudavel que
o contem, sem substituir identidade do candidato.

USR-473: identidade de implementacao ausente e provas de deploy ausentes ou
inconsistentes ficam bloqueadas com causa persistida. Contencao do mutex de
retomada impede novas admissoes naquele ciclo; nenhuma cota e consumida em espera.
## Recovery serial do dispatcher (USR-468 T3)

Falhas determinísticas repetidas acima de `recovery_failure_threshold` persistem o estado `recovery` no SQLite de admission, incluindo causa e fingerprint. Nesse modo o dispatcher mantém uma única reserva global durável por `AdmissionStore.reserve_recovery()`, transmite `run_id` e `candidate_digest` ao `run_ticket.py` e limita recovery a uma entrega em voo na frota. A expiração da lease libera o slot pelo reconciliador; tokens de fencing antigos não liberam uma lease substituta. Antes de iniciar um agente, recovery tenta adquirir e liberar o `suite_lock` oficial: gate ocupado retorna espera estruturada `gate_busy`. Só um `[RECOVERY_RECEIPT]` válido, recente e vinculado ao mesmo candidato e `run_id` limpa recovery; `[DELIVERY_MERGED]` é evidência de merge para agendar deploy, não prova de sucesso de recovery. Espera, idle, falha ou receipt recusado mantém o latch ativo.

As familias de todas as fatias ficam em implementation_families.json. A revisao exclui todos esses provedores, recebe DEMAND, GRILL e SPEC no payload e grava as familias efetivas. Apos autocorrecao, o digest do gate e recalculado. O learning pack e gerado depois da revisao e seu indice preserva o digest; um pack de outro candidato nao libera recovery.

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

USR-477: o workaround que desativa raciocinio fica restrito ao DeepSeek.
Endpoints Anthropic exigem raciocinio; o adaptador usa effort low,
opcao confirmada no catalogo do provedor, dentro dos 4096 tokens totais. HTTP400 e resposta vazia nao sao
aprovacao nem entrega. Contrato: https://openrouter.ai/docs/guides/best-practices/reasoning-tokens

USR-477: saida zero sem entrega em recovery registra falha e causa persistida.
Retomada de deploy exige prova da familia independente. Se a validacao for
corrompida apos merge, o recibo preserva SHA/PR reais sem digest autorizado;
a ausencia desse digest bloqueia release ate a recuperacao da evidencia.

USR-477: quando o revisor pago configurado pertence a uma familia que escreveu
o candidato, a rota tenta o alias distill.openrouter ja registrado e exige
independencia novamente. Se nenhuma familia independente estiver disponivel,
a revisao continua fechada, sem autorizar merge.

## Encerramento sem entrega e recuperacao de CI (USR-479)

Retorno zero sem prova de merge gera causa persistida e cooldown nos modos
normal e recovery. O ticket nao ganha uma nova execucao imediatamente.
A correcao de CI e unica por tentativa: o contador permanece consumido ao
retomar, inclusive depois de gate e revisao verdes. Retomar a entrega segue
gate, revisao independente, novo CI e merge; BUILD nao e repetido. O teste
comportamental cobre esse ciclo inteiro na mesma worktree. Revisoes recusadas
de candidatos anteriores permanecem preservadas; o novo candidato exige suas
proprias provas de portao, revisao e producao.

Na admissao, "mesma familia" compara a familia do modelo, nao o nome do harness: escritor e revisor via OpenRouter com modelos de familias diferentes sao independentes.
