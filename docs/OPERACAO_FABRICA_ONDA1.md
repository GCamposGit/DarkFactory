# Manual de Operação da Fábrica — Onda 1 (Dark Factory)

Documento normativo e operacional de referência da Dark Factory para a Onda 1.
Governança: [HYBRID_WORKFLOW_PLAN_2026-09-08](../docs/HYBRID_WORKFLOW_PLAN_2026-09-08.md), [HYBRID_AUTONOMY_REQUIREMENTS](../docs/HYBRID_AUTONOMY_REQUIREMENTS.md) e [HF-15.md](../docs/handoffs/HF-15.md).

---

## 1. Arquitetura Operacional e Componentes

A Dark Factory opera como uma linha de montagem e engenharia autônoma desacoplada em três camadas:

1. **Camada de Comando e Observabilidade**:
   - **DarkHub** (`hub/backend/`, `hub/frontend/`): Interface central headless e web para acompanhamento canônico de tickets, filas de execução, health checks de workers, telemetria e cartões de status do HF-15.
   - **Gateway de Comunicação** (`core/integrations/telegram.py`, `core/integrations/n8n.py`): Ingestão e aprovações interativas do Owner via Telegram com deduplicação de mensagens (`update_id`) e outbox persistente.

2. **Camada de Inteligência e Roteamento de Modelos**:
   - **Roteador com Dynamic Headroom** (`core/line/routing.py`, `core/router/model_router.py`): Prioriza assinaturas saudáveis, bloqueia contas com cota <= 15% (*fail-closed*), utiliza fallback de Pareto e fixa versões de modelos durante o ciclo de vida do job.
   - **Memória e Auto-Evolução** (`core/memory/`, `core/knowledge/`): Persistência de contexto entre reinícios, auditoria de fontes com links canônicos (arXiv) e geração não-bloqueante do Learning Pack do Owner.

3. **Camada de Execução, Qualidade e Resiliência**:
   - **Motor de Aceitação** (`core/acceptance/engine.py`): Orquestrador dos 8 Portões (G1–G8) e 10 Cenários de Ciclo de Vida da Onda 1.
   - **Coordenador de Rollback e Resiliência** (`core/acceptance/rollback.py`, `core/infra/backup_cron.py`): Criação atômica de checkpoints pré-release, reversão automática em caso de falha de smoke com medição rigorosa de RTO e RPO, mantendo 100% de isolamento entre projetos independentes.
   - **Deploy e Autonomia Git** (`scripts/dokploy_redeploy.py`, `core/git/autonomy.py`): Promoção atômica e idêntica de artefatos (`staging_digest == production_digest`) via Dokploy na VPS Hetzner CX23.

---

## 2. Portões Operacionais Determinísticos (Gates G1 a G8)

Todo fluxo operacional passa pelos seguintes portões antes de qualquer release:

| Gate | Propósito | Comportamento Fail-Closed | Evidência Gerada |
|---|---|---|---|
| **G1** | Demanda e Grill | Demanda ambígua pausa em `WAITING_HUMAN` com 3 opções claras; demanda clara avança sem perguntas redundantes. | `evidence/gate_G1.json` |
| **G2** | Chaves e Dependências | Chave substituível roteia para fallback $0 (Ollama); chave crítica inválida bloqueia até fornecimento seguro. | `evidence/gate_G2.json` |
| **G3** | Ambiente Real vs Testes | Testes unitários verdes NÃO liberam release se o worker real ou firewall falhar na sonda de rede. | `evidence/gate_G3.json` |
| **G4** | Concorrência e Justiça | 9 slots simultâneos (4 dev + 5 testes) operam sem barreira global, starvation ou bloqueio mútuo. | `evidence/gate_G4.json` |
| **G5** | Idempotência e Reinício | `update_id` duplicado é executado exatamente 1 vez; reinício do coordenador recupera jobs e outbox pendente. | `evidence/gate_G5.json` |
| **G6** | Cota e Roteamento Pareto | Cota esgotada ativa fallback qualificado sem compra de crédito não autorizada; versão do modelo é imutável no run. | `evidence/gate_G6.json` |
| **G7** | Pesquisa e Learning Pack | Citações exigem links canônicos (arXiv); memória sobrevive a reboot; Learning Pack de 3 níveis Feynman não bloqueia a produção. | `evidence/gate_G7.json` |
| **G8** | Aceite Comercial e Rollback | Projeto comercial bloqueia produção sem `OwnerAcceptanceReceipt`; rollback atômico medido em sandbox com isolamento de projetos. | `evidence/gate_G8.json` |

---

## 3. Procedimentos Operacionais Padrão (SOPs)

### SOP-01: Pré-Voo e Diagnóstico de Prontidão da Fábrica
Antes de iniciar qualquer turno operacional ou pipeline crítico, execute a validação de pré-voo dos 6 subsistemas:

```powershell
python core/acceptance/cli.py preflight --json
```

**Critérios de Sucesso**:
- `python_runtime`: Versão >= 3.12 (verificado Python 3.12.10).
- `sandbox_filesystem`: Diretório `.factory/hf15/workspace/` com permissão de escrita.
- `worker_concurrency_slots`: Mínimo de 9 slots disponíveis.
- `database_connectivity`: Conexão SQLite Sandbox ou PostgreSQL operacional.
- `n8n_automation_engine`: Endpoint válido e responsivo (rejeita link genérico `n8n.io`).
- `telegram_gateway_auth`: Ao menos 1 ID numérico de Owner configurado na lista de autorização.

### SOP-02: Execução Canônica do Ensaio de Aceitação (HF-15 Runner)
Para executar a suíte canônica consolidada e emitir o pacote auditável de evidências:

```powershell
python -m core.harness.hf15_acceptance --run-id hf15_acceptance_wave1 --json
```

**Estrutura de Saída**:
- Relatório consolidado: `.factory/reports/hf-15-<run_id>/report.json`.
- Recibos individuais por Gate: `.factory/reports/hf-15-<run_id>/evidence/gate_G*.json`.
- Recibos individuais por Cenário: `.factory/reports/hf-15-<run_id>/evidence/scenario_*.json`.

### SOP-03: Simulação e Drill de Rollback de Emergência
Para auditar a saúde da restauração de backups em sandbox e aferir os SLAs de RPO/RTO:

```powershell
python core/acceptance/cli.py rollback-drill --project-id client-corp-payments --json
```

**Métricas e Limites**:
- **RTO (Recovery Time Objective)**: Tempo total de restauração <= 5.0 segundos (medido em ~0.016s).
- **RPO (Recovery Point Objective)**: Idade máxima do checkpoint restaurado = 0.0s (snapshot imutável pré-release).
- **Garantia de Isolamento**: O workspace de drill restaura exclusivamente os dados do projeto afetado, sem impactos em projetos adjacentes.

### SOP-04: Consulta de Métricas de SLA e Observabilidade
Para inspecionar latências acumuladas e cumprimento de SLAs em tempo real:

```powershell
python core/acceptance/cli.py metrics --json
```

**SLAs Contratuais da Onda 1**:
- **Latência Média de Despacho**: <= 30.000 ms (medido em ~64 ms).
- **Latência Média de Reconciliação**: <= 60.000 ms (medido em ~92 ms).
- **Taxa de Falso-Positivo**: 0.0% (proibido avanço silencioso em falha).

---

## 4. Governança de Aceite do Owner

O Owner nunca é sobrecarregado com revisões técnicas ou configurações de infraestrutura. A única intervenção necessária para projetos comerciais (`COMMERCIAL_PAID`) ocorre na liberação de staging para produção:

1. **Inspeção no DarkHub**:
   - Acessar `DarkHub -> Projects -> Releases`.
   - Verificar que o `artifact_digest` de staging coincide com o pacote aprovado.
   - Confirmar que todos os 8 Gates estão verdes e que o drill de rollback foi bem-sucedido.
2. **Aprovação**:
   - Clicar em `Approve Release` no DarkHub, ou enviar comando autenticado via Telegram:
     `/approve <project_id> <artifact_digest>`
   - O sistema gera atomicamente o `OwnerAcceptanceReceipt` e promove o mesmo SHA para produção sem recompilação.

---

## 5. Transição para a Linha de Produção Contínua (HF-27 / Onda 2)

Com a estabilização e validação formal dos motores do HF-15:
- O protocolo pontual de 24h de ensaios sintéticos é sucedido pelos **Canários Contínuos E2E (HF-27-10)**.
- O roteamento e cota dinâmica operam permanentemente com base na skill `19-run-ticket` e `core/line/routing.py`.
- O ciclo de vida de tickets internos da fábrica opera com **Autonomia Git 100% (Zero Toque Humano)** via `core.git.autonomy`.
