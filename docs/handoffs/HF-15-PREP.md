# HF-15-PREP — Preparação de Ambiente, Dados de Teste, Rollback e Observabilidade

Data: 12/09/2026. Origem: `user-demand` (Precursor operacional do encerramento da Onda 1).
Governança: `HYBRID_WORKFLOW_PLAN_2026-09-08` (Seção 11, linhas 267 e 316–333) e `HYBRID_AUTONOMY_REQUIREMENTS` (Seção 7, Cenários G1 a G8).

---

## 1. Identidade e Escopo

- `branch`: `codex/hf-15-acceptance-environment`
- `ticket_id`: `HF-15-PREP`
- `parent_id`: `HF-15`
- `status`: `READY_FOR_EXECUTION`
- `módulos`: `core.acceptance`, `core.acceptance.models`, `core.acceptance.environment`, `core.acceptance.test_data`, `core.acceptance.rollback`, `core.acceptance.observability`, `core.acceptance.cli`, `hub.backend.api`, `hub.backend.service`, `tests/test_hf15_acceptance_environment.py`

---

## 2. Entregas da Preparação

### A. Fencing Git e Linha de Base
- Commit limpo e auditado do **HF-14** na árvore de trabalho (`feat(hf-14): finalize n8n community client, dokploy compose and telegram integration`).
- Branch isolada `codex/hf-15-acceptance-environment` criada com árvore limpa para receber os módulos de aceitação.

### B. Ambiente Isolado Sandbox (`core/acceptance/environment.py`)
- Provisionamento automatizado do diretório de sandbox em `.factory/hf15/workspace/` com subpastas `db/`, `backups/`, `telemetry/`, `fixtures/` e `reports/`.
- Preflight fail-closed com verificação de 6 oráculos:
  1. `python_runtime`: Python >= 3.12 (Python 3.12.10 verificado).
  2. `sandbox_filesystem`: Permissões de escrita na sandbox.
  3. `worker_concurrency_slots`: Capacidade mínima de 9 slots (exigência estrita do Cenário G4: 4 dev + 5 testes).
  4. `database_connectivity`: Sonda em SQLite sandbox ou PostgreSQL de produção.
  5. `n8n_automation_engine`: Validação do endpoint e distinção de links genéricos via `N8nProbe`.
  6. `telegram_gateway_auth`: Verificação de usuários autorizados do owner.

### C. Massa de Dados de Teste Determinística (`core/acceptance/test_data.py`)
- Geradores e fixtures JSON persistidos cobrindo exaustivamente os 8 cenários obrigatórios:
  - **G1**: Demanda ambígua (ativa Grill e requer opções) vs Demanda clara (avança direto sem pergunta redundante).
  - **G2**: Chaves ausentes com substituto aprovado (DeepSeek -> Qwen/Ollama local) vs Chave insubstituível (bloqueio fail-closed se inválida).
  - **G3**: Unitários verdes com worker real bloqueado por rede/firewall (portão bloqueado até correção comprovada).
  - **G4**: 10 projetos de portfólio concorrentes com 4 jobs de dev e 5 jobs de testes em 9 slots (e fila justa sem starvation em 2 slots).
  - **G5**: Idempotência de `update_id=9001` duplicado e recuperação durável de evento perdido em outbox.
  - **G6**: Esgotamento de cota roteando para fallback de Pareto sem espera por recarga de crédito.
  - **G7**: Proveniência de pesquisa com link canônico de arXiv, persistência de memória pós-restart e Learning Pack do owner com tópicos opcionais.
  - **G8**: Projeto comercial pagante com bloqueio estrito de release até `ClientAcceptanceReceipt` assinado; falha injetada aciona rollback automático sem afetar projeto independente.

### D. Mecanismo de Rollback Verificável (`core/acceptance/rollback.py`)
- Coordenador `HF15RollbackCoordinator` acoplado ao `CloudBackupService`.
- Snapshots imutáveis pré-release (`create_pre_release_checkpoint`).
- Reversão atômica sob falha com medição precisa de:
  - **RTO** (Recovery Time Objective): tempo de recuperação em segundos.
  - **RPO** (Recovery Point Objective): idade do snapshot restaurado.
- Emissão de `RollbackExecutionRecord` com hash criptográfico SHA-256 da evidência de restauração.
- Garantia de isolamento estrito de projetos independentes (Cenário G8).

### E. Observabilidade Estruturada e SLAs (`core/acceptance/observability.py`)
- Ledger append-only de eventos de auditoria em `.factory/hf15/workspace/telemetry/observability_ledger.jsonl`.
- Sanitização determinística de segredos (chaves `sk-*`, `ghp_*`, tokens de bot e senhas) antes da gravação.
- Rastreamento dos SLAs obrigatórios:
  - **Latência de Despacho**: <= 30 segundos.
  - **Latência de Reconciliação**: <= 60 segundos.
  - **Taxa de Falso-Positivo**: 0% (zero avanço indevido).

### F. Interface CLI Headless e Endpoints no DarkHub
- CLI headless: `python core/acceptance/cli.py [preflight | seed-data | rollback-drill | status | metrics] --json`
- Endpoints HTTP REST:
  - `GET /api/hf15/status`
  - `GET /api/hf15/metrics`
  - `POST /api/hf15/rollback/drill` (com proteção por sessão de owner `X-Hub-Session`)

---

## 3. Comandos de Validação e Verificação

### Validação dos Testes Unitários e de Integração
```powershell
python -m pytest tests/test_hf15_acceptance_environment.py -v
```

### Validação do Portão Rápido
```powershell
python core/harness/runner.py --quick
```

### Exercício dos Comandos da CLI Headless
```powershell
python core/acceptance/cli.py preflight --json
python core/acceptance/cli.py seed-data --json
python core/acceptance/cli.py rollback-drill --json
python core/acceptance/cli.py status --json
python core/acceptance/cli.py metrics --json
```
