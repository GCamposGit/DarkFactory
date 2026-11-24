<!-- covers: core/line/routing.py, core/line/operating_harness.py, core/router/token_budget.py, core/line/operational_context.py, core/models/gateway_probe.py -->
# Roteamento do estagio de desenvolvimento (USR-378)

Os modelos/efforts por complexidade vivem no registro e na configuração de roteamento.
O auditor `python -m core.models.audit --input <amostras.json> --receipt <recibo.json>`
resolve a rota efetiva e reporta divergência entre stages do mesmo harness. O recibo
inclui contexto configurado, modelo reportado, latência, schema e entrega real; quota
desconhecida ou no piso, revisão da mesma família, timeout e teto de gasto reprovam.

Essas amostras não precisam mais ser montadas manualmente (USR-471 T1): toda chamada real de
executor em `core/line/stage_build.py` (stage `development`) e `core/line/stage_review.py`
(stage `review`) grava um `ExecutionSample` via `core.models.audit.record_execution_sample`
em `state_root()/runs/model_audit/<ticket_id>.jsonl` (append-only, uma linha por chamada, sem
prompt/token/credencial). `quota_remaining_percent` vem de `core.line.routing.quota_remaining_percent`,
a mesma leitura que `pick()` usa — nunca uma sonda nova. Falha ao gravar a amostra é só logada;
nunca derruba o estágio.

Regra operacional confirmada pelo owner em05/11/2026 (USR-473): esgotada a cota das
assinaturas elegíveis, usar OpenRouter também para desenvolvimento, planejamento e
revisão, respeitando saldo, orçamento, capacidades e independência do revisor.
`development.openrouter_ok=true` permite a contingência também quando a telemetria
das assinaturas está desconhecida; não autoriza consumir a assinatura abaixo do piso.
Se não houver crédito ou rota, persistir a espera e manter o dispatcher ativo para
retomar automaticamente. Um `not_before` no log, sozinho, não é um agendamento.

O recibo de auditoria do ledger (USR-471 T2) e recomputado a cada leitura, no mesmo
contrato do snapshot de politica de frota (`core.line.fleet_policy.effective_policy_snapshot`):
nem a CLI nem o endpoint leem o arquivo persistido para decidir, so o recalculam.

```powershell
python -m core.models.audit --from-runs --ticket USR-471 --latest
```

`--ticket` e opcional (sem ele, audita todas as ledgers `state_root()/runs/model_audit/*.jsonl`).
Sai `0` quando o recibo passa e `1` quando ha divergencia/violacao; o recibo recomputado e
persistido em `state_root()/runs/model_audit_receipt.json`. `--input`/`--receipt` continuam
funcionando para auditar um lote de amostras manual, fora do ledger. `GET /line/model-audit`
(query opcional `?ticket=`) devolve exatamente o mesmo recibo no Hub (`HubService.get_model_audit`,
ao lado de `get_fleet_policy`); sem amostras gravadas, o recibo volta vazio com `passed=true`,
nunca erro 500.

`core/line/routing.pick` e o unico roteador de producao. Para o estagio `development`:

- **Cascata**: `claude/sonnet`, `codex` e `grok` (`development.grok` no registro de modelos).
  `.factory/config/line_routing.json` e o default embutido `_default_routing_config` ficam alinhados.
- **Harness do chat so em ticket de complexidade alta**: a preferencia pelo harness de operacao
  (sinal vivo da sessao, `core/line/operating_harness.py`; a variavel persistida `DARKFAC_OPERATING_HARNESS` nao vale) so vale com
  `complexity == "high"`. Tickets `low` e `medium` ignoram o chat e vao para quem tiver mais folga. O `run_ticket.py` repassa o `estimated_complexity` do ticket (`low|medium|high`; ausente ou invalido nao e repassado) ao `pick` (USR-399).
- **Folga semanal**: para `development`, o ranking usa `remaining% da janela semanal / horas ate o reset`
  (maior primeiro), calculado por `_default_weekly_slack` (ou injetado por `slack_lookup`). Se algum
  candidato nao tiver janela semanal com reset conhecido, vale o headroom atual (maior primeiro).
- **Limiar critico unico**: `QUOTA_FLOOR_MIN` (15%) de `core/models/registry.py`. O roteador
  (`pressure_thresholds.critical`), a pressao de tokens e o gatilho de API paga em
  `core/router/token_budget.py` usam esse mesmo valor; cota desconhecida ou `<=` o piso nunca e eleita.

O probe de cota tem cache negativo de 10 min (`probe_negative.json` ao lado dos snapshots): probe que expira ou volta sem leitura utilizavel nao e repetido por 10 min. O probe do Google tenta no maximo 2 portas por fase, com timeout de 1 s (USR-404).

## Política de dados por projeto e divergência entre hosts (USR-487)

`.factory/config/data_policies.json` é a referência compartilhada e versionada. O arquivo legado
`.factory/enterprise/configs.json` fica no estado local do host e continua efetivo por compatibilidade
quando sua configuração difere; essa divergência gera o aviso estruturado `data_policy_host_divergence`.
Uma política local que bloqueia todas as rotas elegíveis aparece como causa `data_policy` na admissão,
com projeto, provedores, restrição e caminho do arquivo que decidiu a rota. Quando houver divergência,
`policy_cause.host_diagnostics` inclui `data_policy_host_divergence` e os caminhos compartilhado,
local e efetivo. O JSON do launcher mantém
`cause: "route_wait"` para a fila e expõe o diagnóstico em `policy_cause`, sem incluir metadados
privados da configuração.

Para diagnosticar uma espera, confira `policy_cause.source_file` e compare o arquivo local com a
referência compartilhada. Testes devem gravar configurações em `tmp_path`/`DARKFAC_STATE_ROOT` temporário;
a sentinela da suíte compara toda a árvore `.factory/enterprise` do checkout antes e depois dos testes.

## Cota do Claude Code

A fonte de quota do Claude e o payload `rate_limits` fornecido pelo statusLine de uma sessão
autenticada do Claude Code Desktop. O comando versionado `scripts/claude_statusline.py` recebe o
payload por stdin, conserva apenas `five_hour` e `seven_day` (`used_percentage`, `resets_at`) e
grava atomicamente `anthropic.json` em `state_root()/usage/providers/` (por padrão
`.factory/usage/providers/`; a variável `DARKFAC_STATE_ROOT` pode mudar a raiz). Prompt, sessão,
workspace, email, tokens e o payload bruto não são persistidos.

O adaptador considera o snapshot fresco por 120 segundos, medidos desde `checked_at`. Payload
inválido ou sem janelas válidas não substitui a última captura boa. Sem leitura fresca, a CLI
`claude auth status --json` e as credenciais locais podem indicar conexão/plano, mas não quota;
quota desconhecida deixa Claude inelegível no roteador. Nenhuma chamada HTTP ou User-Agent
falsificado é usado para ler quota.

Para conferir no Notebook, abra uma sessão autenticada do Claude Code Desktop neste projeto,
aguarde o statusLine informar a sincronização e execute:

```powershell
python -m core.usage.cli accounts --refresh
```

Confirme que Anthropic apresenta `adapter=claude_statusline`, `quota_supported=true` e as janelas
`claude:5h` e `claude:weekly`, com percentuais e horários de reset. Se a captura não aparecer,
confira se o statusLine está habilitado para o projeto, se Python consegue executar
`scripts/claude_statusline.py` e se o arquivo sanitizado foi gravado sob `state_root()/usage/`.
Uma captura acima de 120 segundos deixa de ser elegível; uma nova chamada à CLI, sem `rate_limits`,
não estende a validade da quota antiga.

## Cota do Claude entre sessoes (USR-416)

O snapshot `anthropic` so e gravado pelo statusline enquanto ha sessao do Claude Code aberta. Sem leitura nova, `accounts --refresh` tenta uma chamada minima `claude -p` (stream-json, modelo haiku, diretorio vazio) e le o `rate_limit_event`. Se nao houver leitura, o roteador mantem o ultimo snapshot valido ate o `resets_at` da janela de 5h, somando 10 pontos ao uso (so anthropic; piso de 15 0nalterado). Depois do reset a cota volta a ser desconhecida.

## Operador Interativo Antigravity (USR-433 / USR-436)

**Reserva do coordenador (USR-490 / USR-528):** em contexto `headless` (frota, despachante, launcher), `pick()` não elege `claude` quando a cota restante conhecida é `<=` `coordinator_reserve_pct` (padrão 35, em `.factory/config/line_routing.json`; `0` desliga) **e** há sessão interativa ativa do operador (`state_root()/usage/coordinator_heartbeat.json` dentro do TTL `coordinator_heartbeat_ttl_minutes`, padrão 30 min, renovado por `scripts/claude_statusline.py` e `scripts/live_run.py`). Sem sessão interativa ativa (ou com heartbeat expirado), `claude` volta a ser elegível normalmente acima do piso de 15%. Contexto `interactive` não é afetado. Substitui a entrada manual `reason: coordinator_reserve` em `cooldowns.json`.

O Antigravity é elegível para o estágio `development` **exclusivamente** quando atua como harness de operação interativo (autodetectado pelo sinal vivo da sessao (`ANTIGRAVITY_SESSION_ID`)), adotando **Gemini 4 Neon** (idealmente **Orchestrator (Neon)**) para coordenação, planejamento e alta complexidade (`planning.antigravity`, `development.high.antigravity`) e **Gemini 4 Hydrogen High** / **Gemini 4 Argon High** para codificação simples e tarefas específicas em subagentes (`development.antigravity`). Em modo headless ou na frota (`--fleet`, `fleet_dispatcher`), Antigravity é estritamente somente-leitura e nunca é selecionado para escrita; quando `pick()` e `admit()` são chamados sem `execution_context` explícito, herdam o contexto real do processo (`resolve_process_identity().execution_context`), preservando falha fechada apenas quando um contexto conflitante é declarado explicitamente. Se sua quota for desconhecida ou `STALE`, permanece elegível na interface de chat interativa em modo degradado com aviso explícito e causa estruturada, respeitando o piso de 15% para cotas conhecidas do pool principal (`metric != "model_specific_pool"`). A capacidade de escrita (`write`) é declarada dinamicamente via `agent_cli.supports()` quando `operating_harness == 'antigravity'` em contexto interativo (fora do modo headless), viabilizando sua eleição direta antes do ranking de folga.

## Fallback e Otimizacao OpenRouter (USR-440 / USR-444)

O OpenRouter atua na fabrica com duplo papel: **otimizacao continua de custo-beneficio (Fronteira de Pareto)** e **garantia de operacao continua (fallback zero-touch)** quando as cotas de assinatura estiverem no piso critico (<= 15%) ou zeradas:

1. **Planejamento com Alta Inteligencia**: O estagio `planning` usa o papel `planning.openrouter` apontando para modelos de fronteira de alta capacidade (`tier: high`), garantindo que o fatiamento e especificacao mantenham qualidade Staff+ mesmo sem Claude Opus ou Codex Sol.
2. **Desenvolvimento com Escrita Autonoma**: OpenRouter declara suporte a `write` (`agent_cli.supports("openrouter", "write") is True`) atraves do motor de aplicacao cirurgica de patches e criacao de arquivos (`_apply_openrouter_write_blocks`). No estagio `development`, quando todas as assinaturas locais estao esgotadas ou em cooldown, o OpenRouter e eleito com o papel barato `review.openrouter`, permitindo que a esteira continue implementando e validando sem depender de recargas manuais. Desde 16/11/2026 a escrita roda o agente do Codex CLI com o OpenRouter como provedor (`_run_openrouter_agent`): o agente le a worktree e aplica os patches, cobrando do credito do OpenRouter e nao da assinatura. A resposta unica com blocos `file:`/`patch:` fica so como reserva quando o Codex CLI ou a chave nao existem no host; ela nao enxerga o repositorio e em 450 execucoes nao editou nenhum arquivo. O gasto dessa rota conta no teto mensal lido da propria conta e tambem no teto por execucao: ao fim de cada execucao o custo estimado (tokens de entrada e saida do Codex vezes o preco do modelo no registro de benchmark, o mesmo usado pela resposta unica; tokens em cache contam como entrada normal) vai em `cost_usd` para o ledger de uso com o ticket, e o roteador soma esse total ao decidir. Modelo sem preco conhecido grava custo nulo e um aviso no log, entao esse gasto fica invisivel ate o preco existir.
3. **Avaliacao por Fronteira de Pareto**: Modelos de micro-custo (papel `distill.openrouter` a $0.002/tarefa) sao avaliados para estagios de menor complexidade (`distill`, `review`, fatias mecanicas), poupando as janelas de assinatura para tarefas arquiteturais densas.
4. **Enforcement de Orcamento e Alertas no Telegram**: O roteador e a telemetria de creditos (`ApiCreditsMonitor`) respeitam o teto por execucao (`run_caps.openrouter_usd`, default US$ 2.00) e o teto mensal (`run_caps.openrouter_monthly_cap_usd`, US$ 28.13 neste mes e US$ 10.00 a partir de 01/12/2026). Ao atingir o limiar de alerta (`openrouter_alert_threshold`, default 80% do teto), uma notificacao critica com categoria `AlertCategory.EXECUTION_BUDGET` e enviada via Telegram ao Owner.
5. **Consulta Dinamica a Fronteira de Pareto sem Modelos Hard-coded (USR-444)**: Nenhum modelo de inferencia fica hard-coded em codigo ou instrucoes estaticas. O roteador invoca `resolve_pareto_openrouter_model`, consultando diariamente o ledger de benchmarks (`core.benchmarks.fetcher.ensure_daily_benchmark()`) gerado a partir de Artificial Analysis e OpenRouter. Para planejamento e arquitetura, a esteira exige alta inteligencia (score >= 85.0, classe Claude Opus `anthropic/claude-opus-5` e GPT Sol `openai/gpt-5-6-sol` / `gpt-sol`). Modelos proibidos para autonomia (`fable`, `astra`) sao excluidos por contrato. <!-- census: wire-id -->
6. **Resolucao Automatica de Wire IDs e Dynamic Headroom (USR-440)**: Todo alias interno (`gpt-sol`, `opus`, `sonnet`) e traduzido transparentemente para o wire ID oficial do provedor (`gateway_id`, ex.: `openai/gpt-5.6-sol`), prevenindo erros HTTP 400 de API e garantindo despacho imediato sem atrito. <!-- census: wire-id -->

## Sonda offline de gateways (USR-505)

`python -m core.models.gateway_probe` confere toda declaracao explicita de `gateway_ids` contra o snapshot versionado `.factory/config/openrouter_catalog.json` (coleta de 2026-11-10), em qualquer status, e nao abre rede. Sem declaracao, o id canonico entra somente para modelo `active` quando ja e um id `provedor/modelo`. Candidate ou deprecated sem `gateway_ids` nao alega o id canonico. Um id canonico sem barra nao e gateway OpenRouter. `cli_ids` fica de fora. Saida 0: todas as alegacoes estao no snapshot. Saida 1: achados, cada um com o modelo e o comando de atualizacao. Saida 2: catalogo vazio ou ilegivel (`empty_catalog` ou `catalog_unavailable`); isso nunca e sucesso com lista vazia. `--json` imprime o relatorio. `--write-snapshot` baixa o catalogo vivo e regrava o snapshot. A checagem viva no teste e opt-in (`DARKFAC_LIVE_OPENROUTER=1`).

## Auditoria das execucoes dos modelos (USR-471 T4)

Cada chamada de development ou review grava uma amostra em
`state_root()/runs/model_audit/<ticket>.jsonl`, com ticket, estágio, harness,
complexidade declarada, modelo e esforço reportados, hash/tamanho do contexto,
latência, timeout, custo, validade do schema e entrega real. O prompt não é
gravado. A revisão aprovada também grava `review_execution.json` ligado ao
digest do candidato, com latência medida, saída validada e evidência de família.

`python -m core.models.audit --from-runs --latest --ticket USR-471` recompõe o
recibo atual em `state_root()/runs/model_audit_receipt.json`. O auditor resolve
modelo e esforço pela configuração de estágio e complexidade. A diferença entre
planning e development só é informativa quando ambas correspondem aos pins
configurados. Modelo divergente, esforço divergente, rota ausente, cota no piso,
revisão da mesma família, teto excedido, timeout, schema inválido ou falta de
entrega reprovam a amostra. Sem amostras, o recibo vazio é reprovado como
`no_execution_samples`: ausência não comprova latência ou entrega.




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

USR-477: quando o revisor pago configurado pertence a uma familia que escreveu
o candidato, a rota tenta o alias distill.openrouter ja registrado e exige
independencia novamente. Se nenhuma familia independente estiver disponivel,
a revisao segue a contingencia do USR-488 abaixo, sem autorizar merge por si so.

## Contingencia de revisao USR-488

A revisao nao trava a entrega por falta de outra familia. A cascata escolhe a
primeira rota disponivel, sempre acima do piso de cota e dentro dos tetos
`run_caps.openrouter_*`:

1. outra familia por harness (`pick` de `review`);
2. outra familia pelo OpenRouter: para implementador Claude, o papel
   `review.openrouter` do registro (modelo OpenAI barato); nos demais, o alias
   `sonnet`; o papel `distill.openrouter` segue como ultimo recurso;
3. mesma familia, outro modelo (papeis `review.<harness>` ou `planning.<harness>`
   diferentes do modelo que implementou);
4. mesmo modelo com persona adversarial distinta e temperatura diferente. Os CLIs
   de agente nao aceitam temperatura: fica registrada como `applied: false`, sem
   bloquear.

O OpenRouter tambem vale no nivel 3 (mesma familia, outro modelo): havendo saldo, a revisao nunca termina sem rota.
Para rotas locais, o modelo precisa pertencer ao provedor do harness (`claude`/
Anthropic, `codex`/OpenAI, `grok`/xAI, `antigravity`/Google). Modelo ausente e
permitido; modelo desconhecido ou de outro provedor e descartado, e a cascata
continua consultando `pick` com as exclusoes e capacidades vigentes. OpenRouter
mantem o contrato cross-provider. Nivel 3 so descreve outro modelo nativo do
mesmo harness e familia, distinto do modelo de implementacao; quando essa
distincao nao pode ser demonstrada, uma rota local compativel usa nivel 4 com
persona adversarial.

A admissao nao exige mais rota de revisao nem de portao. Revisao e retomada usam o mesmo `select_review_route`
(`core/line/review_cascade.py`). A revisao e sempre uma chamada de agente nova,
somente leitura, sobre diff e contexto da tarefa, sem a sessao do implementador.

Evidencia: cada rodada de `review_state.json` traz `route` (nivel, harness, modelo,
persona, temperatura, motivo); `review_route.json` e `review_execution.json`
guardam a mesma rota e, nos niveis 3 e 4, o bloco `contingency`. O recibo de
recuperacao aceita revisor da mesma familia somente com `contingency` valido
(nivel 3 ou 4, persona e motivo); sem nivel registrado continua recusado
(`receipt_review_not_independent`). A aprovacao segue ligada ao digest do
candidato e nunca e marcada `forced`.

## Degradar, nao recusar (USR-504)

Ordem de rota para escrever codigo: (1) assinatura com cota conhecida acima do piso de 15%;
(2) assinatura com cota desconhecida, tentada em contingencia (log `route_degraded=unknown_quota`,
qualquer complexidade); se o CLI devolver rate limit, o cooldown existente cuida; (3) OpenRouter barato
dentro dos tetos; (4) so entao esperar. Cota conhecida abaixo do piso continua excluida.

- **Hold de leitura velha**: leitura velha, ausente ou com timeout de qualquer provedor (Anthropic,
  OpenAI, xAI) usa a ultima leitura valida com margem pessimista de 10 pontos, valida ate o reset da janela
  mais proxima ou no maximo 6 h (`_stale_until_reset_headroom`). O cache negativo de 10 min aplica o hold
  antes de devolver desconhecido. `pending_delivery._cached_quota` reutiliza o mesmo hold.
- **Status degradado**: leitura `degraded` com janelas preenchidas e menos de 6 h entra com margem de 10
  pontos (`token_budget._quota_headroom`), nao e ignorada.
- **Leitura suspeita** (`core/usage/history.py`): ao se repetir igual em duas leituras seguidas ela e aceita
  (autoconfirmacao); enquanto nao confirmada vale o maior `used` entre a anterior e a nova, nunca
  desconhecido. O arquivo de confirmacao manual continua valendo.
- **Planejamento**: sem modelo de alto nivel elegivel, uma assinatura de nivel medio (ex.: Grok) planeja antes
  do OpenRouter (log `planning_degraded=true`); `planning.openrouter` segue de alto nivel.
- **OpenRouter**: `development` usa o papel barato `review.openrouter` do registro. O teto mensal
  (`run_caps.openrouter_monthly_cap_usd`) bloqueia de verdade quando o gasto do mes (`ApiCreditsMonitor`,
  cache de 2 min) o alcanca; gasto desconhecido nao bloqueia com saldo conhecido e positivo. O teto por
  execucao le o gasto do ticket no ledger de uso (`ModelUsageLedger.spent_usd`). Duas execucoes de escrita
  seguidas com 0 arquivos aplicados colocam o `openrouter` em cooldown (`record_openrouter_edits`).
- **Verba temporaria**: o teto mensal esta em US$ 28,13 por decisao do owner (OA-090, verba adicional deste
  mes) e volta a US$ 10,00 em 01/12/2026.

## Espera retomavel da revisao USR-512

Falha de infraestrutura ou JSON invalido exclui a rota que acabou de falhar para o mesmo
`candidate_digest` e a mesma `policy_digest` antes da proxima rota compativel. JSON invalido exclui o
harness inteiro. Falha de infraestrutura com modelo exclui o par harness/modelo; timeout tambem tira o
harness de `timed_out_harnesses`. Cancelamento, caminho protegido e self-modification ficam na rota que
os produziu. Quando nenhuma rota valida resta, o estagio devolve a espera do `RouteWaiter`
(`no_route_available`), nao o encerramento `review_agent_exhausted`. A espera nao grava
`review_verdict.json` nem `review_execution.json` e nao altera `validation.json` nem o digest do
candidato. A retomada so publica com aprovacao ligada a esse mesmo digest.

O dispatcher reabre uma espera de REVIEW bloqueada quando a causa e retomavel
(`no_route_available`, `review_agent_exhausted` ou `route_wait`) e surge rota compativel.
O processo e `--resume-delivery` na mesma worktree, sem PLAN e sem BUILD. Evidencia ausente,
evidencia divergente e familia de implementacao desconhecida permanecem bloqueadas. O
`launcher_exit:1` preservado de USR-467 so entra nessa reabertura depois de candidato com
digest, gate `passed` nesse digest e revisao ainda ausente. Sem a revisao ligada ao digest,
nao ha publicacao, e o BUILD e o `validation.json` ficam como estavam.

## Recibo unico de contexto operacional (USR-471 T3)

`python -m core.line.operational_context --verify` compoe, so leitura, os tres
fatos que a auditoria de 05-06/11 encontrou espalhados entre chat, config de
host e registro de modelo: `resolve_process_identity` (harness, origin,
execution_context, project_root, state_root, mode deste processo),
`core.line.fleet_policy.effective_policy_snapshot` (elegibilidade e motivo por
host, sem decidir rota nem admissao) e `core.models.audit.audit_recorded`
(resumo passed/violacoes/divergencias e o modelo efetivo por rota stage.harness).
Nenhuma decisao de rota, admissao ou host nasce nesse modulo; o comando nao
registra fila, nao agenda tarefa e nao reinicia host — um host offline ou sem
leitura so aparece com `reason` no recibo.

O recibo tambem le, via `core.demands` (`build_default_demands_service().get_ticket`),
o status atual de USR-452/454/464/466/456: confirma que esses subproblemas do
intake do USR-471 ja tem ticket proprio, sem criar um novo que duplique escopo
e sem reabrir ou alterar o status de um ticket concluido (leitura pura, nenhum
`save_ticket`/`update_status` e chamado). `--ticket <ID>` limita a auditoria de
modelo ao ledger desse ticket; `--out <caminho>` so troca o destino do recibo
persistido (`docs/evidence/USR-471-operational-context.json` por padrao).
# Reamostragem e reparo de quota desconhecida

Quando a sondagem de um provedor resulta em quota desconhecida por timeout, amostra antiga ou inválida, o roteador solicita uma nova amostra persistente. Chamadas concorrentes para o mesmo provedor reutilizam a solicitação ainda pendente. O registro só é considerado concluído quando o publicador envia uma medição mais nova que o pedido. Se essa nova leitura continuar sem uso, o roteador registra um único ticket de reparo em `darkfac`, deduplicado por provedor e causa; a quota permanece inelegível até haver evidência utilizável.
