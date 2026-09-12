# ADR-HF-001: Seleção do Runtime de Workflows Duráveis para a Dark Factory

- **Status**: Aceito (Híbrido Local-First: DBOS Python para Cloud/VPS condicionado a gate de banco no HF-03; Native SQLite preservado para Local/Offline)
- **Data**: 2026-09-11
- **Decisor**: Astra (Papel de Planejamento e Alta Inteligência)
- **Ticket de Origem**: HF-02-08 (Plano Híbrido 2026-09-08, Seção 12)
- **Documento Complementar**: [docs/RUNTIME_DECISION.md](../RUNTIME_DECISION.md) (preservando o histórico DF-11)
- **Evidências Fáticas**: Laboratório HF-02-07 em `.factory/experiments/HF-02/lab-acceptance-20260911/` e relatório em `.factory/reports/hf-02-spike-report.md`
- **Knowledge Ledger**: [Pesquisa de Arquitetura de Workflow Híbrido](../../.factory/research/20260908_hybrid_workflow/INSIGHTS.md)

---

## 1. Contexto e Declaração do Problema

A Dark Factory opera um pipeline autônomo de software com múltiplos agentes e papéis (Econômico, Alta Inteligência, Auditoria, Evals). À medida que a fábrica evolui para o modelo operacional contínuo (HF-03 a HF-15), seus fluxos de trabalho ultrapassam a execução síncrona de minutos no terminal local, demandando:

1. **Suspensão e Retomada Duráveis (`durable_wait`)**: Workflows devem aguardar decisões humanas (Grill, aprovações de release via Telegram ou DarkHub) por horas ou dias sem prender threads no sistema operacional nem consumir recursos ociosos.
2. **Idempotência no Ingresso (`deduplicated_intake`)**: Comandos repetidos de usuários ou webhooks de mensageria não podem disparar pipelines duplicados ou emitir efeitos colaterais redundantes.
3. **Cancelamento Cooperativo (`cancel_before_next_step`)**: Capacidade de abortar um workflow em voo antes de invocar a próxima etapa cara de IA ou publicação de release.
4. **Isolamento de Versões (`version_isolation`)**: Workflows iniciados em uma versão de código (`v1`) devem concluir com suas definições e schemas originais, mesmo após o deploy de uma nova versão (`v2`), sem corrupção de estado.
5. **Concorrência Bounded (`bounded_concurrency`)**: Fila transacional com limite estrito de jobs concorrentes para respeitar limites de hardware da VPS (4 GB RAM) e cotas financeiras de LLM.
6. **Portabilidade e Execução Offline**: Preservação inegociável da capacidade de qualquer desenvolvedor clonar o repositório e rodar a suíte inteira de forma local, offline e a custo $0.

No ticket DF-11, optou-se por um supervisor embutido sobre SQLite (`OrchestratorRuntime` e `OrchestratorStore`). O spike HF-02 submeteu essa baseline e o candidato DBOS Python a 12 cenários normativos de estresse e falha (R01–R12).

---

## 2. Alternativas Avaliadas

### A. Baseline: Supervisor Python + Native SQLite (DF-11)
- **Descrição**: Runtime atual do repositório em `core/orchestrator/runtime.py` sobre `.factory/orchestrator.sqlite3` com locking via `BEGIN IMMEDIATE` e leases de exclusão mútua.
- **Vantagens**: Zero dependências externas; $0 de custo; roda perfeitamente em Windows/Linux/macOS sem nenhum daemon; determinístico e trivial de inspecionar.
- **Desvantagens**: Não possui suspensão assíncrona durável (threads precisariam dormir em loop bloqueante); não possui fila distribuída com controle de concorrência; não suporta isolamento de versões sem refatoração profunda; trava sob concorrência multi-host.

### B. Candidato: DBOS Python (v2.31.1) sobre PostgreSQL
- **Descrição**: Biblioteca durável leve para Python que utiliza transações e tabelas de sistema no PostgreSQL para garantir execução determinística de workflows, steps checkpointados, filas duráveis e comunicação assíncrona (`DBOS.recv`).
- **Vantagens**: Não exige cluster de orquestração nem daemons separados (usa apenas PostgreSQL já existente na VPS); API nativa em Python 3.12 com decorators (`@DBOS.workflow()`, `@DBOS.step()`); suporte nativo a versionamento, filas com concorrência limitada, cancelamento e timeouts; código aberto permissivo (Apache 2.0).
- **Desvantagens**: Exige conexão com instância PostgreSQL (inviabilizando clones 100% offline se for colocado como dependência obrigatória do núcleo); requer disciplina rigorosa para manter workflows determinísticos.

### C. Alternativa Externa: Temporal (Self-Hosted)
- **Descrição**: Plataforma madura de orquestração com Temporal Server, Temporal Web UI e persistência em Cassandra/PostgreSQL.
- **Desvantagens Rejeitadas**: Superfície operacional excessiva para a Dark Factory nesta fase. Exige múltiplos serviços gerenciados, consome >2 GiB de memória apenas para infraestrutura de suporte e inviabilizaria a VPS de 4 GB RAM.

### D. Alternativa Externa: Prefect 3.0 / LangGraph
- **Descrição**: Frameworks agênticos e de data pipeline.
- **Desvantagens Rejeitadas**: Prefect incentiva dependência de cloud comercial ou servidor pesado. LangGraph é focado em ciclos de raciocínio de LLM no nível de aplicação, não substituindo a garantia de persistência e faturamento do orchestrator do sistema.

---

## 3. Rubrica de Avaliação e Pontuação Empírica

Conforme a Seção 12 de `docs/handoffs/HF-02.md`, os critérios ponderados foram calculados:

| Dimensão | Peso | Native SQLite | DBOS PostgreSQL | Justificativa Fática |
| :--- | :---: | :---: | :---: | :--- |
| **1. Correção / Recuperação** | 30% | 3.0 / 5.0 | 4.0 / 5.0 | Native SQLite passou em R01, R02, R03, R09, R11, R12 (100% dos que suporta), mas falhou por incapacidade estrutural em R04–R08 e R10. DBOS cobre nativamente todos os 12 cenários na especificação técnica; no laboratório local registrou `waiting_access` por ausência de credenciais locais. |
| **2. Operação / Custo** | 25% | 4.5 / 5.0 | 4.5 / 5.0 | SQLite tem custo $0 e zero daemons. DBOS tem custo de licença $0 e reutiliza o PostgreSQL já existente na VPS sem adicionar daemons orquestradores (diferente do Temporal). |
| **3. Reaproveitamento / Portabilidade** | 20% | 5.0 / 5.0 | 4.0 / 5.0 | SQLite preserva 100% dos contratos e o clone offline imediato. DBOS requer venv/extra opcional e import lazy para não contaminar a execução standalone. |
| **4. Versionamento / Auditoria** | 15% | 2.0 / 5.0 | 5.0 / 5.0 | SQLite não isola versões sem reescrita. DBOS implementa `application_version` de forma nativa e comprovada. |
| **5. Integração / Saída** | 10% | 4.0 / 5.0 | 4.5 / 5.0 | Ambos possuem schemas transparentes e saída desimpedida sem lock-in proprietário. |
| **Nota Final Ponderada** | **100%** | **3.725 / 5.00** | **4.325 / 5.00** | **Vantagem técnica e funcional de DBOS para ambiente compartilhado/cloud.** |

---

## 4. Decisão Arquitetural: Arquitetura Híbrida Local-First

Decide-se pela **Arquitetura Híbrida Local-First**:

1. **Coordenador e Workers Cloud (VPS / Produção)**:
   - Adota-se **DBOS Python (v2.31.1) sobre PostgreSQL** como a engine de execução de workflows duráveis para despachos assíncronos, filas de agentes, esperas de aprovação e concorrência limitada.
   - **Condição Operacional de Entrada (Gate HF-03)**: A ativação formal do DBOS em produção fica estritamente condicionada ao cumprimento do gate de prontidão em HF-03, onde o banco descartável/isolado `darkfac_hf02_` será provisionado pelo owner e o probe de conectividade executará com sucesso (eliminando o status `waiting_access`).

2. **Ambiente Local e Execução Offline (Desenvolvedores / CI Rápido)**:
   - Mantém-se o **Native SQLite (DF-11)** como o runtime padrão do repositório para clones limpos, testes unitários instantâneos, execução standalone em computadores sem PostgreSQL e modo desconectado a custo $0.
   - O núcleo `core/orchestrator/runtime.py` permanece intacto e funcional.

3. **Inviolabilidade de Domínio e Contratos**:
   - Nenhuma lógica de negócios ou verificação da Dark Factory dependerá de decorators do DBOS no código de domínio. A camada de aplicação comunicará através dos contratos do HF-04 (`StepContract`, `WorkflowContext`).
   - O adaptador DBOS permanecerá isolado em módulo de infraestrutura (`core/orchestrator/adapters/dbos_adapter.py`), importado dinamicamente apenas quando explicitamente solicitado por configuração de ambiente.

---

## 5. Estratégia de Transição, Migração e Rollback

### 5.1. Regra de Não-Conversão de Checkpoints
- Não haverá script de tradução ou migração binária de checkpoints SQLite existentes para tabelas DBOS.
- Workflows iniciados sob Native SQLite concluem seu ciclo de vida sob Native SQLite.
- Novos workflows criados na nuvem iniciam diretamente sob DBOS PostgreSQL.

### 5.2. Procedimento de Rollback
Caso a operação em DBOS apresente instabilidade na VPS ou o banco PostgreSQL torne-se indisponível:
1. **Drenagem**: Suspender o recebimento de novos jobs na fila DBOS.
2. **Fallback**: Alternar a configuração de runtime para Native SQLite (`DARKFAC_RUNTIME=native_sqlite`).
3. **Preservação de Histórico**: Tabelas do DBOS permanecem legíveis para auditoria; nenhum histórico será sobrescrito.
4. **Ausência de Efeitos Duplicados**: O serviço de efeitos e os endpoints externos continuam verificando chaves de idempotência unívocas por workflow e step.

---

## 6. Governança, Segredos e Envelope Operacional

- **Segredos**: Proibido trafegar DSNs de banco em argumentos de CLI, variáveis de workflow ou serialização JSON/logs. Acesso exclusivamente por variável de ambiente segura (`DARKFAC_HF02_DATABASE_URL`).
- **Orçamento Operacional**: Envelope delimitado de US$ 15–35/mês para infraestrutura cloud; custo de licenciamento de software do runtime é US$ 0.
- **Capacidade do Scheduler**: Concorrência configurada para 2 slots simultâneos na VPS atual (4 GB RAM), escalável via configuração declarativa no HF-05.

---

## 7. Referências

- [Handoff HF-02](../../docs/handoffs/HF-02.md)
- [Plano Híbrido 2026-09-08](../../docs/HYBRID_WORKFLOW_PLAN_2026-09-08.md)
- [Relatório Técnico HF-02-07](../../.factory/reports/hf-02-spike-report.md)
- [Knowledge Ledger: Workflow Híbrido](../../.factory/research/20260908_hybrid_workflow/INSIGHTS.md)
- [DBOS Python Documentation](https://docs.dbos.dev/python/)
- [PostgreSQL Transaction Isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
