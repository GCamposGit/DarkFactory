<!-- covers: run_ticket.py, core/line/stages.py, core/line/ticket_engine.py, core/line/ticket_stages.py, core/line/ticket_extras.py, core/line/route_wait.py -->
# Motor da esteira

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
   ou desconhecido descarta o pin e usa o pick fresco. `--force` e a autorização no prompt não iniciam um
   harness que esse pick recusa. Se o pick de uma fatia devolve `None`, essa fatia não chama o agente, a
   entrega não começa, o processo sai com 75 (`EXIT_ROUTE_WAIT`) e o JSON traz `cause` `route_wait`,
   `stage` `BUILD` e o caminho da worktree. O diretório permanece, com o arquivo que o PLAN gravou nele
   e com o que as fatias anteriores já gravaram. `_discard_untouched_workspace` e `_report_workspace_fate`
   não rodam nesse caminho e portanto não removem a worktree.
5. Na entrega (`--resume-delivery`, processo novo): exige PLAN, RESEARCH e BUILD; **VALIDATE** grava `validation.json`
   depois do portao; **REVIEW** roda `stage_review.ReviewStage` com familia diferente do implementador e grava
   `review_verdict.json` so na aprovacao (`changes_required` reinvoca o agente, comita e repete o portao);
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
nova do launcher normalmente apaga os artefatos anteriores; a entrega apaga validacao, merge e deploy anteriores.
Excecao de retomada (USR-426): se a worktree preservada estiver limpa, PLAN, RESEARCH, BUILD e VALIDATE
forem validos, VALIDATE estiver verde e `review_state.json` registrar resposta JSON invalida para o mesmo
digest de candidato e politica de REVIEW, o launcher preserva esses artefatos e retoma sem reinvocar BUILD.
A entrega executa a validacao novamente; o runner so reutiliza o veredito do portao quando o hash da arvore,
a configuracao e as etapas selecionadas ainda correspondem ao cache. Worktree suja, digest/politica diferentes
ou artefato ausente/invalido seguem pelo fluxo normal de planejamento e desenvolvimento.
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

## Estrutura do run_ticket (USR-375 S3)

`run_ticket.py` e o launcher headless; `_run` agora so orquestra estagios, cada um com no maximo 150 linhas:
`_stage_line_lock`, `_stage_check_harness`, `_stage_intake`, `_stage_preflight` (sonda do grokbox, tamanho,
`_stage_route`, dry run), `_stage_workspace`, `_stage_build` (agente + retry), `_stage_deliver` (subprocesso
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
`rebase-conflito`). Se seguir em conflito, o rebase e abortado, o ticket vai a `waiting_human` e o run sai com rc 1
(causa `rebase_conflict`), sem rc 2 e sem redespacho do despachante.

## Harness de Operacao Interativo e Restricao Headless (USR-433)

`run_ticket.py` detecta o harness de operacao ativo (`DARKFAC_OPERATING_HARNESS` ou contexto do terminal). Se for passado `--fleet` com `--harness antigravity`, a execucao e recusada na inicializacao (`_stage_check_harness`) antes de qualquer efeito colateral (`[ERRO] Antigravity nao e elegivel para desenvolvimento headless/frota.`). O briefing agnostico de sessao `python -m core.ops.briefing` e o handoff versionado `docs/operacao/HANDOFF.md` sincronizam o estado entre sessoes.

## Rotacao de workers remotos com hosts desabilitados (USR-430)

Durante a validacao pelo portao oficial, `run_ticket.py` rotaciona os workers remotos quando uma tentativa falha (`_rotate_gate_workers`). Essa rotacao filtra estritamente hosts listados em `DARKFAC_FLEET_DISABLED_HOSTS` (ex.: `desktop` ocupado com indexacao), garantindo que nos desabilitados nao sejam reinseridos no pool ativo de validacao.

