# DarkHub — revisão e roadmap de evolução

Versão 1.0 · 23/09/2026 · ticket `USR-42` · origem `user-demand`.

O DarkHub é a interface principal do owner. Este documento registra a revisão
completa feita em 23/09/2026, as correções já entregues e o roadmap para o Hub
voltar a refletir toda a fábrica. Os ids `DH-xx` são normativos: o gate de
cobertura (`hub/coverage.json` + `tests/test_hub_coverage.py`) só aceita
capacidades pendentes que apontem para um id existente aqui.

## Como o Hub passa a acompanhar a fábrica

1. **Gate no CI.** `tests/test_hub_coverage.py` roda na suíte offline do CI.
   Toda rota `/api/*` do schema OpenAPI e todo pacote de `core/` precisa estar
   em `hub/coverage.json` com exatamente um destes status:
   - `surface`: id de DOM do Hub que expõe a capacidade (o gate confere que o id
     existe e que o frontend chama a rota);
   - `machine`: rota só para máquinas (webhooks, sincronização, workers), com justificativa;
   - `internal`: pacote do core sem estado próprio para o owner, com justificativa;
   - `pending`: id `DH-xx` deste roadmap.
   Rota ou módulo novo sem entrada, entrada órfã, superfície inexistente ou
   `DH-xx` desconhecido fazem o PR falhar.
2. **Definition of Done.** A skill `04-autonomous-piv-loop` exige, antes de
   declarar uma entrega pronta, o reflexo no Hub ou a pendência registrada aqui.
3. **Diagnóstico local.** `python scripts/hub_coverage.py` mostra o placar e as
   lacunas; `--pending` lista o que falta por item do roadmap.

## Revisão de 23/09/2026 — achados

| # | Achado | Impacto | Tratamento |
| --- | --- | --- | --- |
| R1 | Painel de Tarefas lia `.factory/state.json` e `orchestrator.sqlite3`, que não existem mais | Painel vazio desde a migração para o control store HF-05 | **Corrigido** (USR-42) |
| R2 | No Dokploy o Hub e o coordenador não compartilham estado: coordenador/worker usam PostgreSQL; o Hub criava um `control.db` efêmero no container | Painel e intake cegos para a operação real | Leitura **corrigida** (USR-42); escrita do intake em DH-02 |
| R3 | `services.json` vive em volume Docker; mudanças no `default_services.json` nunca chegavam à nuvem | Catálogo congelado desde o primeiro deploy | **Corrigido** com `catalog_revision` (USR-42) |
| R4 | Catálogo citava modelos desatualizados e não listava Claude Code, Codex, Dokploy nem n8n | Descrições enganosas | **Corrigido** (USR-42) |
| R5 | 56 de 107 rotas `/api` sem nenhuma tela (49 relevantes para o owner, 7 só para máquinas) | Owner não vê notificações, HF-15, integrações, learning packs, evolução etc. | Roadmap DH-01…DH-11 |
| R6 | Módulos do core sem nenhuma presença no Hub (adoption, portfolio, projects, planning, pilots, research, knowledge, router, line, marketing, game, archetypes) | Capacidades invisíveis | Roadmap DH-06, DH-08, DH-12 |
| R7 | 23 tickets duplicados "CLI Pipeline de Testes" (USR-19…USR-41) no ledger versionado | Central de Demandas poluída | DH-15 |
| R8 | Tailwind via CDN em produção, `index.html` com 1.343 linhas e versões de cache inconsistentes por script | Aviso no console, sem build reproduzível, cache velho após deploy | DH-13 |
| R9 | Nenhum sinal de drift visível no próprio Hub | Owner não percebe quando o Hub fica para trás | **Corrigido** (USR-45 / DH-14) |
| R10 | No Dokploy, `darkhub-hub-data` monta `/app/hub/data` como volume, escondendo os `default_services.json`/`default_prompts.json` atualizados da imagem; e a Fila operacional mostrava `ticket_id`/`demand_id` (`darkfac`, `dem-xxxx`) em vez do título real da demanda | Catálogo/prompt seeds da nuvem congelados mesmo após deploys com `catalog_revision` novo; owner não reconhece as tarefas na fila | **Corrigido** (USR-44) |

## Entregue em USR-42

- `core/workflow/job_board.py`: projeção somente-leitura do control store canônico,
  PostgreSQL (`DARKHUB_CONTROL_DATABASE_URL`, com fallback para
  `DARKFAC_HF02_DATABASE_URL`) ou `control.db` local, sem criar arquivo nem rodar DDL.
- Painel de Tarefas: jobs reais por ticket e run, com `WAITING_HUMAN` no topo,
  custo acumulado, etapas percorridas e `cause_code` como exceção.
- Imagem do Hub com `psycopg` e variável nova no compose do Dokploy.
- Catálogo revisado e propagação única por `catalog_revision`, preservando URL,
  favoritos e pins do owner e sem reviver serviços excluídos.
- Gate de cobertura, manifesto, CLI de diagnóstico e regra no PIV loop.

## Roadmap

Prioridade: **Agora** = próximo ciclo; **Depois** = após os itens Agora;
**Futuro** = quando a capacidade do core amadurecer.

| Id | Horizonte | Entrega | Rotas/módulos que fecha | Critério de aceite |
| --- | --- | --- | --- | --- |
| DH-01 | Agora | **Saúde da Fábrica (somente leitura)**: notificações não lidas, Telegram, n8n (status e workflows), workers do harness, eventos de webhook, status/métricas do HF-15. Entregue em `USR-43`; ações mutáveis (reconhecer, checar cotas, sync/trigger n8n, drill HF-15) ficam em `DH-17` | `GET notifications`, `GET integrations/telegram/status`, `GET integrations/n8n/status`, `GET integrations/n8n/workflows`, `GET harness/workers`, `GET webhooks/events`, `GET hf15/status`, `GET hf15/metrics`; `core/notifications`, `core/integrations`, `core/acceptance` | Um painel mostra cada integração com estado, idade da última observação; nenhuma ação mutável é exposta nesta entrega |
| DH-02 | Agora | **Intake canônico na nuvem**: o intake do Hub grava no mesmo control store do coordenador | `POST /api/demands/intake` | Demanda criada no Hub da nuvem vira run visível no Painel de Tarefas; precisa de decisão do owner sobre ativação live |
| DH-03 | Agora | **Caixa de decisões do owner**: jobs `WAITING_HUMAN` com contexto, pergunta e resposta pelo Hub; mudança de status de tickets | `PATCH /api/demands/tickets/{id}/status`; `core/workflow` (manual_resolution) | Owner responde sem terminal; a resposta vira evento idempotente no control store |
| DH-04 | Depois | **Evolução e catálogo com ações**: propor, avaliar, promover e reverter; sincronizar catálogo | `evolution/*`, `catalog/sync`; `core/evolution`, `core/catalog` | Cada ação mostra diff, avaliação e rollback disponível |
| DH-05 | Depois | **Benchmarks e roteamento**: fronteira por domínio, proximidade, top-3 especulativo, corrida empírica, simulador de roteamento | `benchmarks/*`; `core/router`, `core/benchmarks` | Owner vê por que um modelo foi escolhido para uma tarefa |
| DH-06 | Depois | **Aprendizado e conhecimento**: Learning Packs (HTML, Anki, gerar), memória, pesquisa e Knowledge Ledger | `learning-packs/*`; `core/learning`, `core/knowledge`, `core/research` | Última sessão tem pack acessível em 1 clique; ledger de pesquisa pesquisável |
| DH-07 | Depois | **Estúdio de conteúdo e visual**: gerar e auditar conteúdo, presets, galeria e ilustração | `content/*`, `visual/*`; `core/content`, `core/visual`, `core/marketing` | Conteúdo gerado passa pelo lint anti-slop antes de exportar |
| DH-08 | Depois | **Portfólio multiprojeto**: projetos registrados, adoção (Skill 07), pilotos, arquétipos, linhas | `core/projects`, `core/portfolio`, `core/adoption`, `core/pilots`, `core/archetypes`, `core/line`, `core/game` | Cada projeto mostra estágio, saúde, roadmap e último deploy |
| DH-09 | Depois | **Governança enterprise e deploy**: trilha de auditoria, configuração, avaliação de deploy, disparo de deploy Dokploy com confirmação | `enterprise/audit-trail`, `enterprise/configure`, `enterprise/evaluate-deploy`, `cloud/deploy` | Deploy só dispara após avaliação verde e confirmação explícita |
| DH-10 | Depois | **Saúde e histórico do roadmap**: aba de saúde, linha do tempo e comparação de versões no drawer de roadmap | `projects/{id}/roadmap/health`, `history`, `history/compare` | Owner compara duas versões do roadmap e vê o que mudou |
| DH-11 | Depois | **Validação sob demanda**: rodar suíte/harness remoto a partir do Hub com relatório destilado | `harness/run-tests`, `harness/execute` | Resultado com `[HARNESS_PASS]` e link para logs isolados |
| DH-12 | Futuro | **Plano e DAG da autonomia contínua**: visualizar plano, gates de prontidão, rotas qualificadas e política efetiva | `core/planning`, `core/workflow` | DAG navegável com o estado de cada nó |
| DH-13 | Agora | **Fundação do frontend**: CSS compilado localmente no lugar do Tailwind CDN, `index.html` modular, versão de assets única servida pelo backend | frontend | Nenhum aviso de CDN no console; um deploy invalida todos os assets |
| DH-14 | Agora | **Cobertura visível no Hub**: endpoint e badge com o placar do gate e a lista de pendências | `hub/backend/coverage.py`, `hub/backend/api.py`, `hub/frontend/coverage.js` | Entregue em `USR-45`; badge mostra a % coberta e abre a lista de pendências por `DH-xx` em drawer slide-over |
| DH-15 | Agora | **Higiene do ledger de demandas**: remover USR-19…USR-41 e isolar o teste que grava no ledger real | `.factory/demands/demands.json`, testes da CLI | Suíte roda sem alterar arquivos versionados |
| DH-16 | Depois | **Aposentar fontes legadas do Painel de Tarefas** (`state.json`, `orchestrator.sqlite3`) depois de validar o painel na nuvem | `hub/backend/service.py` | Painel usa só o control store; testes legados migrados |
| DH-17 | Depois | **Ações operacionais de saúde**: reconhecer alerta, checar cotas (pode enviar Telegram), sync/trigger n8n (produção), drill HF-15 — cada uma com diálogo de confirmação | `POST notifications/{notification_id}/acknowledge`, `POST notifications/check-quotas`, `POST integrations/n8n/sync`, `POST integrations/n8n/trigger`, `POST hf15/rollback/drill` | Toda ação mostra o efeito antes, exige confirmação explícita e registra auditoria |

## Entregue em USR-43

- DH-01 (somente leitura): faixa de alertas não lidos no topo do Hub (`factory-alerts-strip`,
  `GET /api/notifications?unread_only=true`) e bloco "Saúde da Fábrica" na aba Infra
  (`factory-health-block`) com cards para Telegram, n8n (status e workflows), workers do
  harness, eventos de webhook e ambiente/métricas HF-15 — cada card com semáforo de estado,
  campos-chave e idade da observação. Atualização automática a cada 60s enquanto a aba está
  visível (pausa quando oculta, atualiza ao voltar) mais botão manual "Atualizar"; cada fonte é
  buscada de forma independente (`Promise.allSettled` + `AbortController` de 12s) para que uma
  falha isolada nunca apague as demais. Nenhuma ação mutável foi exposta nesta entrega; elas
  ficam registradas em DH-17.

## Entregue em USR-44

- **Seeds da nuvem chegam ao volume persistido (R10):** `deploy/dokploy/Dockerfile.hub`
  agora também copia `hub/data` para `/app/hub_seed` (fora do volume
  `darkhub-hub-data`, que só monta `/app/hub/data`) e define
  `DARKHUB_SEED_DIR=/app/hub_seed`. Em `HubService.__init__` (ou via o argumento
  opcional `seed_dir`, que tem prioridade sobre a variável de ambiente), antes de
  `_ensure_storage()` rodar, `default_services.json` e `default_prompts.json` do
  `data_dir` são sobrescritos pelo conteúdo do seed sempre que ele existir e
  diferir — apenas esses dois arquivos versionados, nunca `services.json` (edição
  do owner) nem `catalog_meta.json`. Falha de I/O é fail-open (log de aviso,
  Hub nunca deixa de subir por isso). Isso faz `_apply_catalog_revisions` (USR-42)
  enxergar revisões novas mesmo com o volume antigo montado, e `list_prompts` já
  lê `default_prompts.json` diretamente do `data_dir`, então o prompt atualizado é
  servido na mesma leitura.
- **Fila operacional mostra o título real da demanda:** `core/workflow/job_board.py`
  projeta o `payload` de `intake_commands` em cada linha de `jobs` via subconsulta
  correlacionada por `run_id` (`ORDER BY committed_at DESC LIMIT 1`) — um valor
  escalar por linha existente de `jobs`, então nunca multiplica ou duplica linhas.
  O título é extraído em Python (`_extract_title`), aceitando `str` (SQLite TEXT
  ou JSON serializado), `bytes` e `dict` (psycopg pode decodificar JSONB antes da
  leitura); JSON malformado ou sem `title` cai para `None` sem quebrar o painel.
  `JobBoardEntry.title` é o novo campo opcional. Em
  `hub/backend/service.py::_control_dashboard_row`, a prioridade de título passou a
  ser `entry.title` → título do ledger de demandas → `demand_id`; e quando
  `ticket_id == project_id` (linhas reais da nuvem, ex.: `"darkfac"`), o
  `task_id` exibido passa a ser o `demand_id` (único e legível), preservando o
  desempate `ticket@run` para colisões.

## Entregue em USR-45 (DH-14)

- **Cobertura visível no Hub e prevenção contínua de drift (R9):**
  - Adicionado endpoint `GET /api/hub/coverage` servindo `CoverageSummaryResponse` com percentual de cobertura, contagem de superfícies ativas, waivers e lista completa de pendências agrupadas por item de roadmap (`DH-xx`), com títulos e horizontes extraídos de `docs/DARKHUB_ROADMAP.md`.
  - Cache em memória com TTL de 30 segundos (`get_coverage_summary`) para respostas sub-milissegundo, suportando parâmetro `?force_refresh=true`.
  - Badge no cabeçalho do DarkHub (`#hub-coverage-badge`) com percentual em tempo real, semáforo de estado (verde para conformidade com o gate, vermelho se houver drift não registrado) e ponto pulsante.
  - Drawer lateral deslizante (`#hub-coverage-drawer`) com barra de progresso visual, cards de resumo (Entregues, Pendentes, Waivers), alertas de conformidade e acordião de pendências por item de roadmap com separação entre rotas `/api` e módulos `core/`.
  - Módulo frontend modular `hub/frontend/coverage.js` e cobertura 100% no gate `tests/test_hub_coverage.py` e testes de integração `tests/test_hub_coverage_api.py`.

## Configuração manual no Dokploy (para ativar R2 na nuvem)

Sem este passo o painel na nuvem mostra `control: sqlite:missing` e continua vazio.

1. Acesse `https://dokploy.ggcampos.com` e entre com sua conta de owner.
2. Menu lateral **Projects** → abra o projeto que contém o compose `darkhub` (o do `docker-compose.hub.yml`).
3. Clique no serviço **darkhub** → aba **Environment**.
4. Adicione uma linha:
   `DARKHUB_CONTROL_DATABASE_URL=<mesmo valor de DARKFAC_HF02_DATABASE_URL do compose darkfac-coordinator>`
   - Para copiar o valor: abra o compose do **darkfac-coordinator** → aba **Environment** → copie o valor de `DARKFAC_HF02_DATABASE_URL`.
   - Recomendado (menor privilégio): criar no PostgreSQL um papel só de leitura e usá-lo aqui:
     `CREATE ROLE darkhub_ro LOGIN PASSWORD '<senha forte>'; GRANT CONNECT ON DATABASE <db> TO darkhub_ro; GRANT USAGE ON SCHEMA public TO darkhub_ro; GRANT SELECT ON runs, jobs, intake_commands TO darkhub_ro;`
     (desde USR-44 o Painel de Tarefas também lê `intake_commands` para exibir o título real da demanda; sem esse `GRANT` a leitura falha com `control:postgres:error`)
5. Clique em **Save** e depois em **Deploy** (ou **Redeploy**) no serviço **darkhub**.
6. Validação: abra `https://darkhub.ggcampos.com`. Na **Fila operacional**, a faixa de status deve mostrar `control:postgres:ok`.
   Se aparecer `postgres:error`, confira se o host do banco é acessível pela rede `dokploy-network`.
