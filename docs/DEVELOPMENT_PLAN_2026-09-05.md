# Plano de desenvolvimento da Dark Factory — 05/09/2026

Procedimento permanente definido pelo owner em 08/09/2026: [planejamento de alta inteligência e handoff para implementação/testes econômicos](HANDOFF_POLICY.md). Nenhum item deste backlog deve ser despachado para implementação apenas com a descrição de pacote; exige plano específico, vigente e aprovado pelo planejador qualificado. Handoffs iniciais da extensão: [HF-01](handoffs/HF-01.md) e [HF-02](handoffs/HF-02.md).

Extensão de planejamento em 08/09/2026: o [plano integrado de workflow híbrido](HYBRID_WORKFLOW_PLAN_2026-09-08.md) reutiliza os componentes DF, explicita complementos de integração e conecta a infraestrutura ao ciclo até produção. Os novos IDs HF preservam este backlog e suas evidências. A extensão propõe ampliação explícita do escopo anterior de produção/operação; não altera por si só políticas executáveis, status de entrega ou configurações externas.

Status: **proposta executável; não implementada nesta revisão**.
Base: [auditoria e evidências](C:/dev/DarkFac/docs/REVIEW_2026-09-05.md).
Horizonte inicial: **6–8 semanas**, estimativa de planejamento para um responsável técnico com assistência de agentes. Reestimar após os primeiros tickets; gates de saída prevalecem sobre datas.

## 1. Objetivo e produto

**Problema:** a fábrica possui várias capacidades úteis, mas ainda não liga intenção, patch, validação independente e entrega por um caminho durável e auditável. Métricas atuais não distinguem suficientemente simulação de execução real.

**Objetivo do ciclo:** uma tarefa delimitada entra com critérios de aceitação; um executor trabalha em checkout isolado; o supervisor valida resultado, custo e permissões; a tarefa pode retomar após interrupção; a entrega inclui patch e evidências do mesmo commit.

**Usuário principal:** operador técnico que define intenção e limites, acompanha exceções e recebe software verificável sem precisar dirigir cada comando.

**Jornada principal:**

1. Receber issue/TaskSpec com escopo, critérios, caminhos permitidos e orçamento.
2. Resolver contexto mínimo, dependências, classificação de risco e runtime disponível.
3. Preparar checkout isolado e confirmar baseline de testes.
4. Implementar patch com um agente, preservando logs e checkpoints.
5. Executar testes independentes e inspeção de segurança aplicáveis.
6. Corrigir falhas dentro do teto de tentativas; caso contrário, entregar diagnóstico acionável.
7. Produzir patch/PR e pacote de evidência; integração segue autorização vigente e política de risco.
8. Registrar resultado real e propor melhorias de skill apenas quando houver evidência de ganho.

**Fora do escopo deste ciclo:** Canaletto; deploy de produção/billing; expansão para novos produtos de conteúdo/mídia; reescrita integral dos módulos; treinamento/fine-tuning próprio; plataforma distribuída multi-tenant; torneio de todos os provedores a cada execução. Extensões futuras exigem decisão de escopo própria.

## 2. Arquitetura proposta

Manter monólito modular Python e FastAPI na borda. Separar três responsabilidades: supervisor que decide, ambiente que executa e armazenamento que registra.

~~~mermaid
flowchart TD
  A[Issue ou TaskSpec] --> B[Supervisor: estado, risco e orçamento]
  B --> C[Contexto e seleção de executor]
  C --> D[Worker em checkout isolado]
  D --> E[Validador independente]
  E -->|falhou, há orçamento| D
  E -->|aprovou| F[Patch ou PR com evidência]
  F --> G[Integração conforme política]
  B <--> H[(Estado, eventos e checkpoints)]
  D --> H
  E --> H
  H --> I[DarkHub: tarefas, custos e exceções]
  D --> J[Adaptador de ferramentas com permissões]
~~~

**Supervisor:** transições determinísticas, reserva de recursos, leases, deadlines e política. Não aceita declaração textual do agente como autorização.

**Executor:** interface de alto nível start/resume/cancel para harnesses disponíveis; reaproveita suas ferramentas de edição/teste. Um worktree separa alterações Git, mas não é um sandbox de segurança. Execução não confiável precisa também de contenção de arquivos, rede e processos.

**Adaptadores:** separar ModelProvider (inferência) de AgentExecutor (ciclo de ferramentas). Uma assinatura acessível na interface de um produto não implica API intercambiável. Só rotear para adaptador capaz de executar a tarefa.

**Estado:** SQLite transacional como hipótese inicial para operação local; tabelas de tasks, runs, attempts, events, leases, budgets e idempotency_keys. Artefatos em diretório por run fora do checkout de trabalho. Retenção de logs não apaga identidade de operações nem resultados agregados.

**Validação:** configuração confiável, schema fechado, argv explícito e processo com ambiente reduzido. Testes de aceitação/holdout fora da área gravável pelo implementador. Resultado contém base_sha, candidate_sha, policy_hash e artefatos vinculados. Assinatura/atestado só agrega confiança se emitido fora do ambiente que o agente pode modificar.

**Contexto:** resumo operacional pequeno + referências a arquivos/fontes + progresso durável. Histórico completo pode ficar persistido sem ser sempre enviado ao modelo. As interfaces independentes são uma aplicação local do desenho discutido pela [Anthropic em Managed Agents](https://www.anthropic.com/engineering/managed-agents).

### Escolha de runtime

| Opção | Vantagem | Custo/limite | Decisão proposta |
| --- | --- | --- | --- |
| Supervisor Python pequeno + SQLite | Preserva stack e facilita offline/Windows; controle explícito do contrato. | Precisamos implementar e provar leases, retries e recuperação. | Baseline do spike. |
| LangGraph atrás da mesma interface | Checkpoints e composição de fluxos já disponíveis. | Dependência e semântica de retomada precisam caber no domínio. | Comparar se reduzir implementação sem perder limites. |
| Serviço gerenciado de agentes | Reduz operação de infraestrutura. | Portabilidade, credenciais, custos e dependência do fornecedor. | Adaptador futuro; não fazer dele requisito de clone limpo. |

O spike deve implementar a mesma sequência curta, interromper processo entre passos e comparar recuperação, número de dependências e facilidade de teste. Não criar dois runtimes completos. A documentação de [persistência do LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) serve de referência para checkpointing.

## 3. Contratos mínimos

| Contrato | Campos necessários |
| --- | --- |
| TaskSpec | schema_version, task_id, repo, base_sha, objective, acceptance_criteria, allowed_paths, non_goals, risk_class, dependencies, budget |
| Run | run_id, task_id, executor_id, executor_version, model_requested, model_returned, status, timestamps, checkpoint_ref, current_sha |
| Attempt | attempt_id, invocation_id, input_artifact_hash, output_artifact_hash, mode, tokens, measured_cost, estimated_cost, latency, outcome |
| Verification | verifier_version, candidate_sha, config_hash, required_steps, discovered_count, passed_count, skipped_count, exit_codes, artifact_refs |
| Budget | currency, ceiling, reserved, spent, unknown_cost_policy, max_attempts, deadline, concurrency_limit |
| Event | event_id, run_id, sequence, event_type, timestamp, payload_schema, redacted_payload, correlation_id |
| LearningCandidate | rule_id, origin, scope, supporting_runs, eval_version, before_after, status, rollback_ref |

Pydantic v2 nas fronteiras; domínio puro separado de HTTP, subprocesso e filesystem. Contratos versionados e migráveis; campos críticos não podem ser sobrescritos por metadata livre.

Efeitos externos exigem chave de idempotência estável e reconciliação com o provedor. Não prometer exactly-once universal: um processo pode cair após o serviço externo executar e antes do registro local.

## 4. Política de autonomia

A autonomia deve reduzir a necessidade de intervenções por comando. O operador define limites uma vez; execução prossegue dentro deles. Mudanças que ultrapassem o limite viram exceção concreta e revisável.

| Classe proposta | Execução | Entrega |
| --- | --- | --- |
| A — análise e tarefa local reversível | Automática no escopo autorizado. | Relatório/artefato local. |
| B — código de baixo risco | Implementação e testes automáticos em isolamento. | PR; merge automático só após critérios de promoção e autorização do repositório. |
| C — autenticação, dados, dependências críticas, governança | Implementação/proposta e validação; política específica limita efeitos. | Revisão reforçada antes de integração. |
| D — produção, credenciais amplas, gastos além do envelope | Fora do ciclo inicial. | Exceção explícita. |

O controle deve restringir capacidades e acesso, conforme a experiência de [contenção da Anthropic](https://www.anthropic.com/engineering/how-we-contain-claude). Diferentes famílias de modelo podem aumentar diversidade da revisão, mas não eliminam erro compartilhado; a aceitação depende de evidências.

## 5. Fases e critérios de saída

| Fase | Janela indicativa | Entregas | Gate de saída |
| --- | --- | --- | --- |
| 0 — confiança nos controles | Semanas 1–2 | F01–F09 essenciais, separação de métricas, CI dos controles e proteção do Hub. | Reproduções da auditoria rejeitadas corretamente; config inválida falha; nenhuma simulação altera métricas live. |
| 1 — execução recuperável | Semanas 3–4 | TaskSpec, store, executor, orçamento e vertical de bugfix. | Interromper e retomar sem perder tarefa, duplicar efeito ou declarar sucesso falso. |
| 2 — qualidade mensurável | Semanas 5–6 | Dataset real, telemetria consistente, benchmark com proveniência, contexto seletivo e navegador. | Resultados reproduzíveis por task/run/commit; baseline por tipo de tarefa e custos conhecidos. |
| 3 — autonomia seletiva | Semanas 7–8 | Integração GitHub, política de promoção, painel de tarefas e experimento de roteamento. | Baixo risco elegível com evidências atuais; rollback/exceções exercitados; melhora observada contra baseline. |

Fases 2 e 3 não devem mascarar pendências P1 da fase 0. As semanas são previsão, não promessa de entrega.

## 6. Backlog em tickets pequenos

Os nomes de arquivos novos e testes abaixo são **propostos**. Comandos de teste novos passam a existir ao implementar cada ticket; não foram executados nesta revisão. Cada ticket lista até quatro arquivos principais. Espelhar alteração de skill é artefato gerado obrigatório em ticket próprio quando necessário.

Todos os tickets também devem cumprir os dois comandos oficiais: python core/harness/runner.py --quick e python -m pytest tests -v --ignore=tests/test_canaletto.py. Criar primeiro a reprodução do comportamento esperado e confirmar que a versão anterior falha.

| ID | Escopo e arquivos principais | Depende | Critério e VALIDATE_CMD proposto |
| --- | --- | --- | --- |
| DF-01 | state.py; novo orchestrator/models.py; novo test_orchestrator_state.py | — | Transição ilegal, metadata de controle e MERGED sem evidência rejeitados. python -m pytest tests/test_orchestrator_state.py -v |
| DF-02 | guard.py; novo test_governance_guard.py | — | Git indisponível/ref inválida bloqueiam; detectar untracked, rename e diff contra base. python -m pytest tests/test_governance_guard.py -v |
| DF-03 | markers.py; runner.py; novo harness/models.py; novo test_harness_contract.py | — | Logs forjados/contagem zero/config inválida falham; resultado vincula SHA/config. python -m pytest tests/test_harness_contract.py -v |
| DF-04 | ci.yml; harness.config.json; novo test_ci_policy.py | 02,03 | Política executada sobre base correta; incluir arquivos de controle; não confiar em verificador alterado pelo candidato. python -m pytest tests/test_ci_policy.py -v |
| DF-05 | racing.py; benchmarks/models.py; test_speculative_racing.py | — | Simulação preservada separadamente; só tentativas live atualizam métricas live. python -m pytest tests/test_speculative_racing.py -v |
| DF-06 | learning/tracker.py; learning/models.py; test_learning_engine.py; test_self_learning_benchmark.py | — | Regra reprovada não ativa; sem evidência fica candidata; métricas sintéticas rotuladas. python -m pytest tests/test_learning_engine.py tests/test_self_learning_benchmark.py -v |
| DF-07 | frontend/app.js; backend/models.py; novo test_hub_browser.py; requirements.txt | — | Texto ativo permanece inerte; protocolos/cores/IDs inválidos rejeitados. python -m pytest tests/test_hub_browser.py -v |
| DF-08 | backend/main.py; backend/api.py; backend/service.py; novo test_hub_access.py | — | Host/origem/sessão inválidos bloqueados; health probe só destinos permitidos, inclusive redirects. python -m pytest tests/test_hub_access.py -v |
| DF-09 | arxiv_client.py; github_scout.py; novo research/transport.py; test_research_engine.py | — | TLS inválido nunca vira unverified; falha externa distinta de resultado vazio. python -m pytest tests/test_research_engine.py -v |
| DF-10 | usage/ledger.py; novo usage/store.py; test_usage_monitor.py | — | Replay após retenção não duplica; processo concorrente/corrupção não perde dados. python -m pytest tests/test_usage_monitor.py -v |
| DF-11 | novo orchestrator/store.py; novo orchestrator/runtime.py; novo test_runtime_recovery.py; novo docs/RUNTIME_DECISION.md | 01,10 | Spike escolhe runtime; store recupera após crash e impede dois claims válidos. python -m pytest tests/test_runtime_recovery.py -v |
| DF-12 | novo execution/contracts.py; novo execution/budget.py; novo test_execution_budget.py; router/token_budget.py | 10,11 | Reserva concorrente não excede teto; janelas longa/curta, custo desconhecido e deadline tratados. python -m pytest tests/test_execution_budget.py -v |
| DF-13 | novo execution/providers.py; novo execution/agent_executor.py; novo execution/sandbox.py; novo test_executor_contract.py | 11,12 | start/resume/cancel, timeout e kill de filhos; limites de rede/arquivos testados. python -m pytest tests/test_executor_contract.py -v |
| DF-14 | hub/backend/service.py; core/game/cli.py; novo test_provider_integration.py | 13 | Primeiro fluxo migra ao adaptador comum, orçamento/telemetria reais; core não depende de Hub. python -m pytest tests/test_provider_integration.py -v |
| DF-15 | novo orchestrator/cli.py; novo test_factory_vertical.py; novo fixture_factory_bug.py; novo factory_vertical.config.json | 03,11–14 | Issue gera patch real; teste holdout independente; crash/retomada preservam resultado. python -m pytest tests/test_factory_vertical.py -v |
| DF-16 | test_audio_transcriber.py; novo conftest.py; pytest.ini; ci.yml | 04 | Offline sem GPU/segredos/rede; ensaios opcionais separados; matriz Windows/Linux. Dois comandos oficiais em clone limpo. |
| DF-17 | benchmarks/fetcher.py; benchmarks/models.py; test_model_benchmark.py; data/benchmark_catalog.json | 05 | Fonte/data/unidade por campo; score heurístico não promovido a medição. python -m pytest tests/test_model_benchmark.py -v |
| DF-18 | novo evals/tasks.jsonl; novo evals/runner.py; novo evals/graders.py; novo test_agent_evals.py | 15,17 | Rodadas reais separadas de smoke/mock; reference patches passam e defeitos conhecidos falham. python -m pytest tests/test_agent_evals.py -v |
| DF-19 | novo orchestrator/context.py; novo learning/promotion.py; novo test_context_policy.py; docs/HARNESS_INTEROP.md | 06,18 | Contexto seleciona fatos pertinentes; candidato só promove após eval e pode reverter. python -m pytest tests/test_context_policy.py -v |
| DF-20 | novo integrations/github.py; novo orchestrator/delivery.py; novo test_delivery_policy.py; novo docs/AUTONOMY_POLICY.md | 04,15,18 | Checks antigos não liberam SHA novo; política de risco/merge queue e idempotência validadas. python -m pytest tests/test_delivery_policy.py -v |
| DF-21 | backend/api.py; backend/service.py; frontend/index.html; novo frontend/tasks.js | 11,18 | Painel exibe fila, run, etapa, custo, evidência e exceções; checagem manual guiada da jornada + suíte existente. |
| DF-22 | content/engine.py; visual/cloud_engine.py; test_anti_slop_engine.py; test_visual_studio.py | 12–14 | Qualidade reprovada/fallback/provedor trocado são explícitos; download limitado. python -m pytest tests/test_anti_slop_engine.py tests/test_visual_studio.py -v |
| DF-23 | .agents/skills/04-autonomous-piv-loop/; novo orchestrator/worktree.py; novo test_worktree_isolation.py; scripts/sync_skills.py | 02,04,11 | Checkpoint limpo antes do fan-out; cada frente recebe worktree, branch, owner e lease exclusivos; manifesto bloqueia sobreposição, ambiente inválido, ID temporário e checkout compartilhado; heartbeat detecta task travado; handoff prova SHA/arquivos/testes e recupera falha parcial; integração topológica e limpeza só após commits alcançáveis e gates verdes. python -m pytest tests/test_worktree_isolation.py -v |

**Manutenção editorial associada:** abrir tickets separados de até quatro arquivos para corrigir alegações do README/arquitetura/Learning Packs; simplificar skills por evidência; espelhar com sync_skills.py; adicionar verificação de drift. Não misturar mudanças de política com correções funcionais para escondê-las na revisão.

**Caminho crítico:** DF-01/02/03 → DF-04; DF-10 → DF-11 → DF-12 → DF-13 → DF-14 → DF-15 → DF-18 → DF-20. DF-05/06/07/08/09 podem avançar independentemente por responsáveis distintos, se desejado.

**Trilha operacional de paralelismo:** DF-23 consolida o contrato de desenvolvimento concorrente com worktrees. O ticket deve tornar fail-closed a criação de duas frentes no mesmo diretório, exigir checkpoint limpo e backup recuperável, reservar ownership por lease, detectar sobreposição antes da escrita, verificar Python/pytest/basetemp, persistir IDs/títulos/cwd, emitir heartbeat observável e provar a cadeia baseline → commit seletivo → integração → limpeza, inclusive após handoff parcial.

## 7. Evals e métricas

Começar com **24 tarefas versionadas**: 8 bugfixes, 6 pequenas funcionalidades, 4 refactors, 3 correções de segurança e 3 recuperações/falhas de integração. Incluir falhas reais desta revisão. Separar conjunto de desenvolvimento e holdout protegido por permissões. Um diretório com nome holdout dentro do checkout gravável não cria independência.

Cada tarefa contém fixture/base, critérios, referência válida, defeito conhecido e limite de recursos. Rodar smoke offline a cada PR e experimentos live explicitamente identificados dentro do orçamento de avaliação.

| Métrica | Definição operacional |
| --- | --- |
| Taxa de aceitação autônoma | Tarefas elegíveis aceitas por verificação independente / tarefas elegíveis iniciadas; registrar também bloqueios e mudanças de escopo. |
| Pass@1 | Sucesso na primeira tentativa de implementação; distinguir retries de transporte. |
| Consistência em 3 execuções | Fração de tarefas que passam em todas as três execuções independentes. |
| Custo por tarefa aceita | Custo de todas as tentativas/avaliações dividido por tarefas aceitas; custos estimados e ausentes separados. |
| Tempo de ciclo p50/p95 | Início até entrega aceita; discriminar espera por provedor e por operador. |
| Intervenção do operador | Minutos e motivo da intervenção; não contar automaticamente toda resposta como falha do agente. |
| Regressão escapada | Defeito confirmado após entrega por categoria e janela de acompanhamento. |
| Recuperação | Fração de cenários de crash retomados sem perda/efeito duplicado. |

A [Anthropic distingue Pass@k de consistência Pass^k e recomenda separar capability de regressão](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents). Aqui, o alvo é desempenho verificável do conjunto tarefa + modelo + harness, não ranking abstrato de modelos.

**Metas provisórias, não resultados atuais:** todos os controles de integridade devem passar; zero falso verde no corpus adversarial conhecido; toda chamada tem identidade/modo/custo medido ou desconhecido explícito; todas as tarefas possuem recuperação testada. Para propor auto-merge de baixo risco, observar ao menos 30 tarefas elegíveis com ≥90% de aceitação independente e nenhuma regressão grave conhecida. A amostra é piloto, não prova estatística de risco zero; publicar intervalo de confiança e revisar o limiar após a baseline.

## 8. Otimização posterior e melhoria contínua

Após a baseline, comparar um modelo forte único com roteamento econômico em tarefas equivalentes. Medir custo incluindo revisão e retrabalho; escolher qualidade mínima antes de reduzir preço. Candidato cancelado/não executado não recebe derrota no Elo.

Comparar separadamente: contexto seletivo, revisão por risco, modelo econômico e paralelismo. Não mudar tudo no mesmo experimento. A revisão de componentes conforme capacidade dos modelos segue a experiência de [Harness design for long-running applications](https://www.anthropic.com/engineering/harness-design-long-running-apps).

Melhorias de skills geram proposta com origem, escopo, antes/depois e rollback; só regras úteis e avaliadas são ativadas. AGENTS deve funcionar como índice de conhecimento, seguindo o padrão relatado pela [OpenAI](https://openai.com/index/harness-engineering/). O aprendizado não deve criar um manual crescente de promessas absolutas.

## 9. Migração e primeira entrega

1. Capturar baseline e isolar este plano das alterações locais já existentes.
2. Corrigir controles em pequenos commits, mantendo interfaces antigas quando seguro.
3. Migrar dados para store versionado com backup e ferramenta de importação; preservar ledgers históricos como evidência.
4. Executar supervisor novo em modo observação, sem poder de merge, comparando decisões.
5. Habilitar vertical local; depois PRs; só depois propor integração automática de baixo risco.
6. Se os resultados piorarem, desabilitar o novo executor/roteamento por configuração e manter dados para diagnóstico.

**Primeira entrega recomendada:** DF-01, DF-02, DF-03, DF-05 e DF-06, acompanhados de DF-07/08/09 para fechar segurança. Isso elimina falsos avanços, estatísticas enganosas e promoção de regras reprovadas antes de investir em novos agentes.
