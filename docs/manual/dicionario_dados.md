<!-- covers: docs/manual/dicionario_dados.md -->
<!-- paths: .factory/config/*.json, .factory/projects.json, .factory/demands/*.json, .factory/owner_actions/*.json, .factory/roadmap/*.json, .factory/infra/*.json, .factory/learning/*.json, .factory/telegram/*.json, .factory/n8n/workflows/*.json, .factory/governance/authorizations/*.json, .factory/worktrees/*.json, .factory/planning/*.json, .factory/research/*/ledger.json, .factory/baselines/*.json, .factory/experiments/*.json, .factory/reviews/*.json, .factory/reports/*.json, .factory/runs/*.json -->
# Dicionario de dados dos `.factory/**/*.json`

Todo JSON versionado em `.factory/` aparece aqui. A checagem fora do portao `python C:\dev\DarkFac\core\harness\docs_sync.py --dictionary` sai com 1 se algum `.json` versionado nao casar com um padrao da linha `paths:` acima. Ao criar um JSON novo em `.factory/`, acrescente o padrao e a entrada.

## Estado vivo (lido e escrito pela fabrica)

| Arquivo | Proposito | Le | Escreve | Campos principais |
|---|---|---|---|---|
| `.factory/demands/demands.json` | Ledger de tickets (lista de demandas). | `core/demands/`, `core/line/`, `hub/backend/service.py`, `core/git/autonomy.py` | `core/demands/store.py`, `core/git/queue_registration.py`, `core/line/owner_intake.py` | `id`, `project_id`, `title`, `origin`, `status`, `item_type`, `lifecycle_stage`, `horizon` |
| `.factory/owner_actions/owner_actions.json` | Export de OAs do control store; fonte local apenas com modo file explicito. | `core/owner_actions/`, `hub/backend/service.py`, `scripts/snapshot_queue.py` | `core/owner_actions/store.py`, `core/owner_actions/merge.py` | `schema_version`, `description`, `actions[]` (titulo, prioridade, passos, status) |
| `.factory/projects.json` | Registro de projetos gerenciados. | `core/projects/registry.py`, `core/line/stage_release.py`, `hub/backend/service.py` | `core/adoption/bootstrap.py` | `id`, `name`, `path`, `kind`, `prefix`, `domain`, `deploy_target` |
| `.factory/roadmap/darkfac.json` | Roadmap do projeto darkfac. | `core/roadmap/service.py`, `core/line/dogfood.py`, `core/line/acceptance.py` | `core/roadmap/service.py` | `schema_version`, `project_id`, `title`, `items[]` |
| `.factory/infra/inventory.json` | Inventario de nos, ADRs e orcamento mensal. | `core/infra/inventory.py`, `hub/backend/service.py` | `core/infra/inventory.py` | `nodes`, `adrs`, `total_monthly_budget_usd`, `last_updated` |
| `.factory/infra/roadmap.json` | Roadmap de infraestrutura (fases e itens). | `core/roadmap/` | edicao via ticket | `phases`, `items`, `project_id` |
| `.factory/learning/learning_ledger.json` | Semente rastreada do registro de aprendizado (nao e mais mutada em runtime). | `core/learning/`, `core/learning_pack/analyzer.py`, `core/line/retrospective.py` | versionada por PR | `session_id`, `turns`, `preferences`, `mistakes`, `transfers`, `verifications` |
| `.factory/learning/learning_ledger.live.json` | Overlay vivo (gitignored, EC-03): `record-turn`, preferencias e RCAs gravam aqui com lock entre processos e escrita atomica; o leitor usa o overlay quando existe, senao a semente. | `core/learning/tracker.py` | `core/learning/tracker.py` | mesmos campos da semente |
| `.factory/worktrees/manifest.json` | Leases de worktrees ativas. | `core/git/autonomy.py` | `core/git/autonomy.py` | `base_sha`, `leases`, `updated_at` |
| `.factory/worktrees/autonomy-plan.json` | Plano de autonomia de uma worktree (ticket, branch, fencing). | `core/demands/specifier.py`, `core/roadmap/sources.py` | agente do ticket | `ticket_id`, `branch`, `cwd`, `base_sha`, `fencing_token` |

## Configuracao (`.factory/config/*.json`, 12 arquivos)

| Arquivo | Proposito | Le | Escreve | Campos principais |
|---|---|---|---|---|
| `model_registry.json` | Unica fonte de ids de modelos, papeis e limiares. | `core/models/registry.py`, `scripts/generate_model_docs.py`, `core/models/gateway_probe.py` | edicao via ticket | `models`, `roles`, `quota_floor_percent`, `grill_thresholds`, `rollback_thresholds`, `gateway_ids`, `cli_ids` |
| `openrouter_catalog.json` | Snapshot do catalogo OpenRouter (2026-11-10) usado pela sonda offline de gateways. | `core/models/gateway_probe.py` | `python -m core.models.gateway_probe --write-snapshot` | `collected_at`, `source`, `generated_by`, `model_ids` |
| `line_routing.json` | Roteamento por estagio, pressao de cota e tetos de run. | `core/line/routing.py`, `core/line/bindings.py` | edicao via ticket | `stages`, `pressure_thresholds`, `cooldown_default_minutes`, `run_caps` |
| `line_fleet.json` | Hosts e slots da frota de workers. | `core/line/fleet_dispatcher.py`, `core/line/fleet_policy.py`, `scripts/start_fleet_worker.py` | edicao via ticket | `max_fleet_slots`, `hosts` |
| `subagent_model_routing.json` | Politica de modelo por tipo de tarefa de subagente. | `core/line/subagent_routing.py`, `core/models/docgen.py` | edicao via ticket | `limits`, `eligible_task_types`, `forbidden_tags`, `forbidden_stages`, `opus_stages` |
| `agent_env_policy.json` | Allowlist/denylist de variaveis de ambiente dos agentes. | `core/line/agent_env.py` | edicao via ticket | `keep_names`, `deny_names`, `deny_patterns`, `allow_names`, `network_allowlist` |
| `secrets_required.json` | Chaves exigidas no cofre SOPS, inclusive as de piloto (prefixo por projeto, ex. `MANSAODOLAGO_`). | `core/infra/secrets_vault.py`, `scripts/bootstrap_secrets.py`, `core/line/pilot_secrets.py` | edicao via ticket | `version`, `required` |
| `subscription_prices.json` | Preco das assinaturas para custo por ticket. | `core/usage/ticket_cost.py`, `hub/backend/service_usage.py` | edicao via ticket | `status`, `plans` |
| `complexity_baseline.json` | Baseline do ratchet de complexidade. | `scripts/complexity_ratchet.py` | `scripts/complexity_ratchet.py` | `functions`, `limit` |
| `model_id_baseline.json` | Baseline do censo de ids de modelo fora do registry. | `scripts/model_id_census.py` | `scripts/model_id_census.py` | `total`, `files` |


## Governanca (`.factory/governance/authorizations/*.json`, 5 arquivos)

Autorizacoes assinadas pelo owner para alterar paths protegidos. Le: `core/governance/authorization.py`, `core/orchestrator/guard.py`. Escreve: o owner via `python -m core.governance.keys sign <arquivo>`. Campos: `id`, `ticket_id`, `approver`, `paths`, `candidate_kind`, `candidate`, `expires_at`, `signature`.

## Integracoes

| Padrao | Proposito | Le | Escreve | Campos principais |
|---|---|---|---|---|
| `.factory/telegram/config.example.json` | Modelo de config do bot (sem valores reais). | `core/git/secret_scan.py` (garante que nao ha segredo) | owner copia para o config real | `bot_token`, `authorized_user_ids`, `authorized_chat_ids`, `webhook_secret_token` |
| `.factory/n8n/workflows/*.json` (2) | Workflows n8n exportados (alertas de pipeline, gateway Telegram). | `hub/backend/api.py` | export manual do n8n | `name`, `nodes`, `connections`, `active`, `settings` |

## Pesquisa e evidencias historicas (somente leitura na pratica)

| Padrao | Proposito | Le | Escreve | Campos principais |
|---|---|---|---|---|
| `.factory/research/*/ledger.json` (17) | Knowledge Ledger por pesquisa (skill 10). | `core/research/ledger.py`, `hub/backend/service_integrations.py` | `core/research/cli.py` | `ledger_id`, `query`, `topic_type`, `summary_executive`, `sources`, `insights` |
| `.factory/baselines/**/*.json` (2) | Snapshot e fontes do baseline HF-01. | historico | motor de baseline HF-01 (encerrado) | `base_sha`, `claims`, `items`, `issues`, `hf02_readiness` |
| `.factory/runs/**/evidence.json` (1) | Evidencia da run HF-01. | historico | motor HF-01 (encerrado) | `ticket_id`, `run_id`, `base_sha`, `snapshot_ref`, `item_count` |
| `.factory/experiments/**/*.json` (~22) | Resultados do laboratorio HF-02 (`lab-acceptance-20260911`, configs `run_R*`) e do roteamento `jev-routing-20260919`. | historico | execucoes do laboratorio | `schema_version`, `results`, `capability_matrix`, `eligibility`, `operational_metrics` |
| `.factory/reviews/**/*.json` (11) | Pacotes de revisao `hf-critical-20260909` e `hf-wave1-luna` (validacao, dispositions, delta). | historico; `scripts/run_deepseek_review.py` grava em `.factory/reviews` | revisores e `scripts/run_deepseek_review.py` | `schema_version`, `scope`, `files`, `harness`, `count_delta`, `head` |
| `.factory/reports/**/*.json` (2) | Relatorio de aceite da linha e evidencias de revisao de projeto. | `core/line/acceptance.py` | `core/line/acceptance.py` | `review_date`, `base_commit`, `validation`, `probes`, `inventory` |
| `.factory/planning/**/*.json` (12) | Pacote de planejamento `continuous-autonomy` (plan, bindings, handoff, integridade, validacao, recibos) e `hf01-hf02-handoffs.json`. | `core/demands/specifier.py`, `core/roadmap/sources.py`, `core/workflow/*_handler.py`, `core/learning/research_dispatch.py` | planejador (modelo `opus`) | `schema_version`, `ticket_id`, `baseline_sha`, `environment_profile`, `depends_on`, `artifacts` |

## Outbox do Telegram (runtime, nao versionado)

O arquivo vive em `.factory/telegram/outbox.json`, `outbox_owner.json` ou `outbox_ops.json` (ou `<state>_outbox.json` quando o gateway recebe `state_file`). `.gitignore` ignora `.factory/telegram/*.json` fora de `config.example.json`, entao este JSON nao entra na linha `paths:`. Quem le e escreve e `core/integrations/telegram.py` (`TelegramGateway`). A gravacao escreve um temporario no mesmo diretorio e troca com `os.replace`. Uma falha no meio deixa o arquivo anterior inteiro.

O conteudo e uma lista. A chave estavel de cada item e `notification_id|role|chat_id` (`key`). Um enqueue repetido da mesma chave atualiza a linha; nao cria outra. `role` e `owner`, `ops` ou `all`. `chat_id` `0` nao e destino.

| Campo | Significado |
|---|---|
| `notification_id` | Id original do alerta (`notif_` + hex). O `NotificationService` repassa esse id e nao gera outro no envio. |
| `role` | Papel do bot que deve entregar. |
| `chat_id` | Destino numerico. |
| `key` | `notification_id\|role\|chat_id`. |
| `text` | Corpo ja redatado. Nao guarda token. |
| `buttons` | Teclado inline, ou nulo. |
| `parse_mode` | `HTML` ou vazio depois de uma tentativa sem formatacao. |
| `created_at` | ISO-8601 da primeira entrada. |
| `updated_at` | ISO-8601 da ultima mudanca. |
| `status` | `pending`, `sending`, `delivered`, `uncertain` ou `permanent`. |
| `failure_reason` | Categoria sanitizada, por exemplo `category=transient`. Sem corpo HTTP e sem segredo. |
| `message_id` | Id da mensagem no Telegram depois do recibo. Nulo antes disso. |
| `delivered_at` | ISO-8601 do recibo. |
| `fingerprint` | SHA-256 truncado (16 hex) do texto redatado. Nao e o texto nem o token. |
| `delivered` | Espelho de `status=delivered`, para leitura antiga. |
| `claim_token` | Preenchido so em `sending`. |
| `claim_pid` | PID do worker que reivindicou. |
| `claimed_at` | ISO-8601 da reivindicacao. |

Estados:

- `pending`: falha transitoria ou ausencia de token. O `replay_outbox` pode enviar. Reiniciar o processo nao apaga a linha.
- `sending`: um replay reivindicou a linha. Outro replay concorrente nao pega a mesma linha. Se o PID morre ou a reivindicacao passa de 120 s, ela volta a `pending`.
- `delivered`: recibo confirmado. Guarda `message_id`, `delivered_at` e `fingerprint`. A mesma chave nao chama `sendMessage` de novo.
- `uncertain`: timeout ambiguo (`TimeoutError` / `socket.timeout` durante a leitura). A requisicao pode ter chegado. Nao e reenviada por `send_message` nem por `replay_outbox`.
- `permanent`: erro HTTP definitivo encontrado no replay. Nao volta para a fila.

`NotificationService` so acrescenta `telegram` em `delivered_channels` depois do recibo. Falha do envio, ou ausencia do bot, acrescenta `telegram_outbox` e nao marca `telegram`. Destino ausente nao entra no outbox e nao usa esse canal.
