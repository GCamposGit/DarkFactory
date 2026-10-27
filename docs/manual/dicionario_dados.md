<!-- covers: docs/manual/dicionario_dados.md -->
<!-- paths: .factory/config/*.json, .factory/context_intelligence.json, .factory/projects.json, .factory/demands/*.json, .factory/owner_actions/*.json, .factory/roadmap/*.json, .factory/infra/*.json, .factory/learning/*.json, .factory/telegram/*.json, .factory/n8n/workflows/*.json, .factory/governance/authorizations/*.json, .factory/worktrees/*.json, .factory/planning/*.json, .factory/research/*/ledger.json, .factory/baselines/*.json, .factory/experiments/*.json, .factory/reviews/*.json, .factory/reports/*.json, .factory/runs/*.json -->
# Dicionario de dados dos `.factory/**/*.json`

Todo JSON versionado em `.factory/` aparece aqui. A checagem fora do portao `python C:\dev\DarkFac\core\harness\docs_sync.py --dictionary` sai com 1 se algum `.json` versionado nao casar com um padrao da linha `paths:` acima. Ao criar um JSON novo em `.factory/`, acrescente o padrao e a entrada.

## Estado vivo (lido e escrito pela fabrica)

| Arquivo | Proposito | Le | Escreve | Campos principais |
|---|---|---|---|---|
| `.factory/demands/demands.json` | Ledger de tickets (lista de demandas). | `core/demands/`, `core/line/`, `hub/backend/service.py`, `core/git/autonomy.py` | `core/demands/store.py`, `core/git/queue_registration.py`, `core/line/owner_intake.py` | `id`, `project_id`, `title`, `origin`, `status`, `item_type`, `lifecycle_stage`, `horizon` |
| `.factory/owner_actions/owner_actions.json` | Acoes que so o owner executa (OA-NNN), exibidas no Hub. | `core/owner_actions/`, `hub/backend/service.py`, `scripts/snapshot_queue.py` | `core/owner_actions/store.py`, `core/owner_actions/merge.py` | `schema_version`, `description`, `actions[]` (titulo, prioridade, passos, status) |
| `.factory/projects.json` | Registro de projetos gerenciados. | `core/projects/registry.py`, `core/line/stage_release.py`, `hub/backend/service.py` | `core/adoption/bootstrap.py` | `id`, `name`, `path`, `kind`, `prefix`, `domain`, `deploy_target` |
| `.factory/roadmap/darkfac.json` | Roadmap do projeto darkfac. | `core/roadmap/service.py`, `core/line/dogfood.py`, `core/line/acceptance.py` | `core/roadmap/service.py` | `schema_version`, `project_id`, `title`, `items[]` |
| `.factory/infra/inventory.json` | Inventario de nos, ADRs e orcamento mensal. | `core/infra/inventory.py`, `hub/backend/service.py` | `core/infra/inventory.py` | `nodes`, `adrs`, `total_monthly_budget_usd`, `last_updated` |
| `.factory/infra/roadmap.json` | Roadmap de infraestrutura (fases e itens). | `core/roadmap/` | edicao via ticket | `phases`, `items`, `project_id` |
| `.factory/learning/learning_ledger.json` | Registro de aprendizado da sessao (preferencias, erros, verificacoes). | `core/learning/`, `core/learning_pack/analyzer.py`, `core/line/retrospective.py` | `core/learning/tracker.py` | `session_id`, `turns`, `preferences`, `mistakes`, `transfers`, `verifications` |
| `.factory/worktrees/manifest.json` | Leases de worktrees ativas. | `core/git/autonomy.py` | `core/git/autonomy.py` | `base_sha`, `leases`, `updated_at` |
| `.factory/worktrees/autonomy-plan.json` | Plano de autonomia de uma worktree (ticket, branch, fencing). | `core/demands/specifier.py`, `core/roadmap/sources.py` | agente do ticket | `ticket_id`, `branch`, `cwd`, `base_sha`, `fencing_token` |
| `.factory/context_intelligence.json` | Mapa de stack, entrypoints e camadas do repo (skill 01-prime-intelligence). | agentes (skill `01-prime-intelligence`) | skill `01-prime-intelligence` | `stack`, `entrypoints`, `domain_layers`, `reachability`, `governance`, `validation` |

## Configuracao (`.factory/config/*.json`, 12 arquivos)

| Arquivo | Proposito | Le | Escreve | Campos principais |
|---|---|---|---|---|
| `model_registry.json` | Unica fonte de ids de modelos, papeis e limiares. | `core/models/registry.py`, `scripts/generate_model_docs.py` | edicao via ticket | `models`, `roles`, `quota_floor_percent`, `grill_thresholds`, `rollback_thresholds` |
| `line_routing.json` | Roteamento por estagio, pressao de cota e tetos de run. | `core/line/routing.py`, `core/line/bindings.py` | edicao via ticket | `stages`, `pressure_thresholds`, `cooldown_default_minutes`, `run_caps` |
| `line_fleet.json` | Hosts e slots da frota de workers. | `core/line/fleet_dispatcher.py`, `core/line/fleet_policy.py`, `scripts/start_fleet_worker.py` | edicao via ticket | `max_fleet_slots`, `hosts` |
| `subagent_model_routing.json` | Politica de modelo por tipo de tarefa de subagente. | `core/line/subagent_routing.py`, `core/models/docgen.py` | edicao via ticket | `limits`, `eligible_task_types`, `forbidden_tags`, `forbidden_stages`, `opus_stages` |
| `agent_env_policy.json` | Allowlist/denylist de variaveis de ambiente dos agentes. | `core/line/agent_env.py` | edicao via ticket | `keep_names`, `deny_names`, `deny_patterns`, `allow_names`, `network_allowlist` |
| `secrets_required.json` | Chaves exigidas no cofre SOPS. | `core/infra/secrets_vault.py`, `scripts/bootstrap_secrets.py` | edicao via ticket | `version`, `required` |
| `subscription_prices.json` | Preco das assinaturas para custo por ticket. | `core/usage/ticket_cost.py`, `hub/backend/service_usage.py` | edicao via ticket | `status`, `plans` |
| `complexity_baseline.json` | Baseline do ratchet de complexidade. | `scripts/complexity_ratchet.py` | `scripts/complexity_ratchet.py` | `functions`, `limit` |
| `model_id_baseline.json` | Baseline do censo de ids de modelo fora do registry. | `scripts/model_id_census.py` | `scripts/model_id_census.py` | `total`, `files` |
| `model_discovery.json` | Fontes e teto de gasto da descoberta de modelos. | skill `12-daily-model-benchmark` | edicao via ticket | `spend_cap_usd`, `sources` |
| `qualification_battery.json` | Bateria de tarefas de referencia para qualificar modelos. | skill `14-speculative-model-racing` | edicao via ticket | `battery_version`, `default_budget`, `contract`, `tools`, `reference_tasks` |
| `qualification_spend.json` | Tetos diarios e por candidato do gasto de qualificacao. | skill `14-speculative-model-racing` | edicao via ticket | `provider`, `api_key_env`, `daily_cap_usd`, `per_candidate_cap_usd`, `max_output_tokens` |


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
