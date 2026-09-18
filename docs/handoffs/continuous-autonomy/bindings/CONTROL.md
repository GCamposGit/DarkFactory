# Binding do Controle Cloud e Ownership (HF-05-02)

Versão 1.0 · 18/09/2026 · Ticket de Alta Arquitetura `HF-05-02` · Handoff Normativo de Infraestrutura.  
Baseline: `83e5298eb231599076811802dceac8575c7f6feb` · Parent: `HF-05` · Predecessores: `HF-26-02`, `HF-26-03` · Sucessores: `HF-05-03`, `HF-03-07`.

---

## 1. Contexto e Declaração de Missão

O ticket `HF-05-02` estabelece o binding canônico de arquitetura para a camada de controle em nuvem, persistência transacional e governança de ownership de workflows da Dark Factory.

Como definido em `CONTRACTS.md` e em conformidade estrita com o `ADR-HF-001`, a fábrica opera sob uma **arquitetura híbrida local-first**:
1. **Ambiente Local e CI ($0 de custo, offline)**: Execução autônoma standalone e suíte de testes rápidos baseados em SQLite em modo WAL (`hf05_sqlite`), sem exigência de daemons externos.
2. **Ambiente de Produção e VPS (Cloud Durável)**: Execução contínua, filas distribuídas, esperas assíncronas duráveis (`durable_wait`) e isolamento de versão suportados por PostgreSQL 16+ transacional e a engine DBOS Python (`cloud_dbos_postgres`).
3. **Preservação Histórica Legada**: Runs e checkpoints originados na baseline DF-11 (`df11_legacy`) permanecem imutáveis e legíveis para auditoria, sem qualquer rotina de migração ou conversão destrutiva.

Este documento, em conjunto com o arquivo formal de especificação `.factory/planning/continuous-autonomy/bindings/control.json`, fixa de forma vinculante as assinaturas do protocolo `ControlStore`, as estruturas de dados canônicas, o schema DDL PostgreSQL, a mecânica de concorrência com leases e fencing monotônico, o padrão transacional de Outbox para despacho DBOS e os procedimentos de recuperação de falhas.

A implementação concreta desta especificação (em SQLite e PostgreSQL) é de responsabilidade do ticket econômico sucessor `HF-05-03`.

---

## 2. Modelagem dos Tipos Canônicos de Controle

Todos os tipos canônicos residem formalmente no módulo `core/workflow/control_contracts.py` (a ser implementado no HF-05-03), herdando de `ContractModel` com validação estrita Pydantic v2 (`extra="forbid"`, `validate_assignment=True`, `str_strip_whitespace=True`).

### 2.1. `IntakeCommand`
Comando unificado de ingresso submetido por canais externos (Telegram, DarkHub, CLI, Webhook):
```python
class IntakeCommand(ContractModel):
    project_id: Annotated[str, StringConstraints(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")]
    channel: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    payload: dict[str, Any]
    mode: Literal["autonomous", "documentary"]
    policy_ref: Annotated[str, StringConstraints(min_length=1, max_length=160)]
```
* **Estrutura do Payload**: Deve conter obrigatoriamente as chaves `title`, `problem`, `journey`, `non_goals` e `criteria`.
* **Invariantes de Serialização**: Serialização em JSON canônico UTF-8 com chaves ordenadas (`sort_keys=True`), separadores exatos `(',', ':')` e sem valores `NaN`/`Infinity`. O digest do payload é computado como o SHA-256 desses bytes brutos. Arrays mantêm a ordem exata de envio (não são ordenados artificialmente) e textos de intenção não sofrem normalização agressiva.

### 2.2. `IntakeReceipt`
Recibo determinístico emitido após o aceite transacional do comando:
```python
class IntakeReceipt(ContractModel):
    demand_id: str
    demand_version: str
    run_id: str | None
    initial_job_id: str | None
    mode: Literal["autonomous", "documentary"]
    committed_at: str  # ISO 8601 UTC
```
* **Invariante de Modo**: No modo `autonomous`, `run_id` e `initial_job_id` são strings preenchidas e válidas. No modo `documentary`, os três campos de execução (`run_id`, `initial_job_id`, etc.) são obrigatoriamente `None` (a demanda é persistida para catalogação sem acionar a esteira de execução).

### 2.3. `JobKey`
Identificador composto de uma etapa atômica de trabalho. **Proibido expressar chaves de banco como strings concatenadas de forma ambígua**:
```python
class JobKey(ContractModel):
    run_id: str
    ticket_id: str
    plan_version: str
    stage: str
    iteration: Annotated[int, Field(ge=0)]
```
* No banco de dados, `JobKey` mapeia para a chave primária composta de 5 colunas: `(run_id, ticket_id, plan_version, stage, iteration)`.

### 2.4. `Claim`
Contrato de reserva exclusiva de um job por um worker ativo:
```python
class Claim(ContractModel):
    job_key: JobKey
    lease_id: str
    owner: str  # Identidade do worker (ex: 'worker-cloud-01:pid-1044')
    fencing_token: Annotated[int, Field(gt=0)]
    expires_at: str  # ISO 8601 UTC (now + lease_duration_sec)
    reservation_id: str  # Reserva de capacidade no portfólio
    route_ref: str  # Perfil ou rota de modelo autorizado
```
* **Fencing Monotônico**: O `fencing_token` é estritamente incremental por job. Ele nunca é reiniciado ou decrementado após release, expiração ou falha.
* O token não substitui a identidade do worker: ambos são checados em operações subsequentes.

### 2.5. `StageContext`
Contexto completo e imutável entregue ao `StageHandler.handle()`:
```python
class StageContext(ContractModel):
    claim: Claim
    plan_ref: str
    plan_digest: str
    candidate_digest: str | None
    config_version: str
    environment_ref: str
    identity: str
    route_ref: str
    memory_version: str
    input_refs: list[str]
```

### 2.6. `StageResult`
Resultado emitido ao término da execução de uma etapa:
```python
class StageResult(ContractModel):
    outcome: Literal["success", "retry", "replan", "waiting_dependency", "waiting_human", "cancelled", "failed"]
    output_refs: list[str]
    evidence_refs: list[str]
    operation_refs: list[str]
    actual_cost: Annotated[float, Field(ge=0.0)]
    cause_code: str | None
```
* **Regra de Sucesso**: `outcome == "success"` exige obrigatoriamente `output_refs` não vazio.
* **Proibição de Falso Atestado**: Nenhum booleano sintético como `approved=true` tem validade probatória; evidências reais exigem referências em `evidence_refs`.

### 2.7. `ExternalOperation`
Registro de controle de idempotência para operações com efeitos externos (envio ao Telegram, criação de PR no GitHub, disparo de build no Dokploy, requisições pagas a LLMs):
```python
class ExternalOperation(ContractModel):
    operation_key: str  # Chave determinística unívoca
    request_digest: str  # SHA-256 do payload enviado
    provider: str  # 'telegram', 'github', 'dokploy', 'openrouter'
    external_id: str | None  # ID retornado pelo provedor (message_id, pr_number, deploy_id)
    status: Literal["prepared", "sent", "unknown", "succeeded", "failed"]
    observed_at: str  # ISO 8601 UTC
```

### 2.8. `ReconcilePage`
Página de resultados retornada pelo ciclo de varredura do reconciliador:
```python
class ReconcilePage(ContractModel):
    cursor: str | None
    visited_projects: list[str]
    repaired_keys: list[dict[str, Any]]
    next_cursor: str | None
    cycle_id: str
```

---

## 3. Protocolo Unificado `ControlStore`

O protocolo `ControlStore` define o contrato obrigatório que qualquer backend de persistência (SQLite local ou PostgreSQL cloud) deve implementar:

```python
from typing import Protocol
from datetime import datetime

class ControlStore(Protocol):
    def accept(self, command: IntakeCommand, now: datetime) -> IntakeReceipt:
        """Ingere um comando com garantia de idempotência estrita.
        - Se o external_id já foi aceito com o mesmo payload hash, retorna o recibo existente.
        - Se o payload divergir para a mesma chave de canal/external_id, lança IdempotencyConflict.
        - Em modo autônomo, cria run, job inicial e evento de outbox na mesma transação.
        """
        ...

    def claim(self, worker: str, capabilities: list[str], now: datetime) -> Claim | None:
        """Seleciona e aloca o próximo job pendente compatível com as capacidades do worker.
        - Utiliza SELECT FOR UPDATE SKIP LOCKED no PostgreSQL para alta concorrência sem bloqueio.
        - Incrementa monotonicamente o fencing_token (+1).
        - Define expires_at = now + 45s e retorna o Claim ativo.
        - Retorna None se não houver trabalho disponível.
        """
        ...

    def heartbeat(self, claim: Claim, now: datetime) -> Claim:
        """Renova a concessão do lease para now + 45s.
        - Revalida se fencing_token e lease_id coincidem com o estado atual no banco.
        - Lança StaleLeaseError caso o lease tenha expirado ou sido revogado/reivindicado por outro worker.
        """
        ...

    def finish(self, claim: Claim, result: StageResult, now: datetime) -> None:
        """Finaliza atomicamente a etapa de trabalho.
        - Exige fencing_token vigente; caso contrário, aborta com StaleLeaseError sem aplicar alterações.
        - Persiste o StageResult, libera o claim e atualiza o status do job.
        - Se resultado for 'success', agenda os jobs sucessores e grava os eventos no Outbox.
        - Se resultado for 'retry'/'replan', aplica a respectiva política de re-enfileiramento.
        """
        ...

    def materialize(self, event: dict[str, Any], now: datetime) -> None:
        """Confirma o processamento e despacho de um evento do Outbox.
        - Atualiza o registro do outbox para 'published' com timestamp de conclusão.
        """
        ...

    def reconcile(self, now: datetime, cursor: str | None = None, limit: int = 100) -> ReconcilePage:
        """Varredura de auto-cura de concorrência e falhas de nós.
        - Identifica claims com expires_at < now e status 'active', marcando-os como 'expired'.
        - Re-enfileira jobs órfãos (caso retry_count < max_retries) ou marca como falhos.
        - Identifica eventos de Outbox pendentes há mais de 30s para re-tentativa.
        """
        ...

    def get_operation(self, key: str) -> ExternalOperation | None:
        """Recupera o registro de uma operação externa pelo seu identificador unívoco.
        - Permite checagem prévia obrigatória antes de qualquer efeito colateral.
        """
        ...

    def record_operation(self, operation: ExternalOperation, claim: Claim) -> None:
        """Registra ou atualiza o estado de uma operação externa sob a guarda do lease ativo.
        - Rejeita com StaleLeaseError se o claim for inválido ou obsoleto.
        """
        ...
```

---

## 4. DDL Schema PostgreSQL Transacional

O schema para PostgreSQL 16+ garante integridade referencial, bloqueios atômicos sem deadlocks e trilha completa de auditoria.

```sql
-- ============================================================================
-- 1. TABELA: runs (Instâncias mestres de execução autônoma ou documental)
-- ============================================================================
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(160) PRIMARY KEY,
    project_id VARCHAR(160) NOT NULL,
    demand_id VARCHAR(160) NOT NULL,
    demand_version VARCHAR(64) NOT NULL,
    runtime_owner VARCHAR(32) NOT NULL CHECK (runtime_owner IN ('hf05_sqlite', 'df11_legacy', 'cloud_dbos_postgres')),
    mode VARCHAR(32) NOT NULL CHECK (mode IN ('autonomous', 'documentary')),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    plan_digest VARCHAR(64) NOT NULL,
    config_version VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_runs_project_demand UNIQUE (project_id, demand_id, demand_version)
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status);

-- ============================================================================
-- 2. TABELA: jobs (Etapas atômicas com chave primária composta)
-- ============================================================================
CREATE TABLE IF NOT EXISTS jobs (
    run_id VARCHAR(160) NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    ticket_id VARCHAR(64) NOT NULL,
    plan_version VARCHAR(64) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    iteration INTEGER NOT NULL CHECK (iteration >= 0),
    status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'succeeded', 'retry', 'replan', 'waiting_dependency', 'waiting_human', 'cancelled', 'failed')
    ),
    role VARCHAR(64) NOT NULL,
    required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    current_lease_id VARCHAR(160),
    timeout_seconds INTEGER NOT NULL DEFAULT 1800,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    cause_code VARCHAR(64),
    actual_cost NUMERIC(10, 4) NOT NULL DEFAULT 0.0000,
    output_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, ticket_id, plan_version, stage, iteration)
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim_lookup ON jobs (status, role) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON jobs (run_id);

-- ============================================================================
-- 3. TABELA: claims (Leases de exclusão mútua e fencing monotônico)
-- ============================================================================
CREATE TABLE IF NOT EXISTS claims (
    lease_id VARCHAR(160) PRIMARY KEY,
    run_id VARCHAR(160) NOT NULL,
    ticket_id VARCHAR(64) NOT NULL,
    plan_version VARCHAR(64) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    iteration INTEGER NOT NULL,
    owner VARCHAR(160) NOT NULL,
    fencing_token INTEGER NOT NULL,
    reservation_id VARCHAR(160) NOT NULL,
    route_ref VARCHAR(160) NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'stolen')),
    FOREIGN KEY (run_id, ticket_id, plan_version, stage, iteration) 
        REFERENCES jobs(run_id, ticket_id, plan_version, stage, iteration) ON DELETE RESTRICT,
    CONSTRAINT uq_claims_job_fencing UNIQUE (run_id, ticket_id, plan_version, stage, iteration, fencing_token)
);

CREATE INDEX IF NOT EXISTS idx_claims_expiry_sweep ON claims (expires_at, status) WHERE status = 'active';

-- ============================================================================
-- 4. TABELA: outbox (Fila transacional para despacho desacoplado ao DBOS)
-- ============================================================================
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    target_system VARCHAR(64) NOT NULL DEFAULT 'cloud_dbos',
    status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'published', 'failed', 'dead_letter')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending_dispatch ON outbox (status, created_at) WHERE status = 'pending';

-- ============================================================================
-- 5. TABELA: external_operations (Idempotência de efeitos colaterais externos)
-- ============================================================================
CREATE TABLE IF NOT EXISTS external_operations (
    operation_key VARCHAR(256) PRIMARY KEY,
    request_digest VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    external_id VARCHAR(256),
    status VARCHAR(32) NOT NULL CHECK (status IN ('prepared', 'sent', 'unknown', 'succeeded', 'failed')),
    claim_lease_id VARCHAR(160),
    fencing_token INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    response_digest VARCHAR(64),
    error_details TEXT
);

CREATE INDEX IF NOT EXISTS idx_ext_ops_provider_status ON external_operations (provider, status);

-- ============================================================================
-- 6. TABELA: reconciliation_ledger (Auditoria das correções automáticas)
-- ============================================================================
CREATE TABLE IF NOT EXISTS reconciliation_ledger (
    entry_id BIGSERIAL PRIMARY KEY,
    cycle_id VARCHAR(160) NOT NULL,
    action_type VARCHAR(64) NOT NULL,
    target_key JSONB NOT NULL,
    reason VARCHAR(256) NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recon_cycle_time ON reconciliation_ledger (cycle_id, recorded_at);
```

### Mapeamento Equivalente para SQLite (`hf05_sqlite`)
No backend SQLite para desenvolvimento local:
- Tipos `TIMESTAMPTZ` são representados como strings ISO 8601 em UTC (`TEXT`).
- Tipos `JSONB` são representados como `TEXT` com validação de payload via `json_valid()`.
- O modo WAL (`PRAGMA journal_mode=WAL;`) e `PRAGMA busy_timeout=5000;` são ativados obrigatoriamente.
- A concorrência transacional utiliza `BEGIN IMMEDIATE` para serialização estrita de claims e mutações.

---

## 5. Modelo de Ownership Imutável e Regra Anti-Conversão

Para honrar a garantia de isolamento do **ADR-HF-001**:
1. **Ownership Fixo por Run**: Cada `run` gravado na tabela `runs` possui uma coluna imutável `runtime_owner`:
   - `"hf05_sqlite"`: Runs executados pelo runtime local nativo.
   - `"df11_legacy"`: Runs legados da baseline histórica (DF-11), mantidos apenas para leitura e preservação de histórico.
   - `"cloud_dbos_postgres"`: Runs cloud executados sob a supervisão do DBOS na VPS.
2. **Proibição Estrita de Migração de Checkpoints**: É terminantemente vedada a criação de scripts de tradução ou migração de banco para converter checkpoints legados em linhas de PostgreSQL ou vice-versa.
3. **Isolamento de Vida Útil**:
   - Um run iniciado sob `hf05_sqlite` conclui sob `hf05_sqlite`.
   - Um run iniciado sob `cloud_dbos_postgres` conclui sob `cloud_dbos_postgres`.
   - Caso a nuvem fique indisponível e ocorra failover para o runtime local, apenas **novas demandas** passam a rodar sob o motor local; os runs em voo na nuvem aguardam o restabelecimento da conectividade com o PostgreSQL para serem reconciliados normalmente.

---

## 6. Ciclo de Vida de Leases e Fencing Monotônico

Para evitar condições de corrida em workers distribuídos e o fenômeno de "zombie workers" (workers que perdem temporariamente a conexão com a rede ou travam em garbage collection/I/O e ressurgem tentando sobrescrever dados), o sistema adota as seguintes regras temporais e lógicas:

| Parâmetro | Valor | Descrição |
| --- | :---: | --- |
| `heartbeat_interval_sec` | **10 s** | Intervalo periódico em que o worker ativo invoca `ControlStore.heartbeat()` para estender sua concessão. |
| `lease_duration_sec` | **45 s** | Tempo de expiração de um lease concedido (`expires_at = now + 45s`). Tolerância a 3 falhas seguidas de heartbeat. |
| `sweep_interval_sec` | **30 s** | Frequência com que o processo de background executa `ControlStore.reconcile()` para colher leases abandonados. |
| `idle_wait_sec` | **15 s** | Tempo de espera quando a fila de jobs elegíveis está vazia antes de nova consulta. |

### Mecanismo de Fencing Monotônico
1. Todo job inicia com `fencing_token = 0`.
2. Ao ser reivindicado com sucesso via `claim()`, o banco incrementa atomicamente `fencing_token = fencing_token + 1`.
3. O `Claim` retornado carrega esse token unívoco.
4. Qualquer operação de mutação (`heartbeat`, `record_operation`, `finish`) impõe a cláusula de guarda no SQL:
   ```sql
   UPDATE jobs SET status = :new_status, ...
   WHERE run_id = :run_id AND ticket_id = :ticket_id 
     AND plan_version = :plan_version AND stage = :stage 
     AND iteration = :iteration AND fencing_token = :claim_fencing_token;
   ```
5. Se nenhuma linha for alterada, o sistema detecta imediatamente que o lease foi roubado ou sofreu timeout, abortando a transação com `StaleLeaseError`. O worker zumbi é forçado a encerrar a execução local sem gerar outputs corruptos ou poluir a esteira.

---

## 7. Padrão Transacional de Outbox para Despacho DBOS

A integração com o runtime DBOS Python (v2.31.1) em ambiente cloud **nunca é realizada diretamente dentro de transações de negócio**. Invocar chamadas de rede ou APIs de orquestradores externos com locks de banco abertos causa esgotamento de conexões e deadlocks distribuídos.

Adota-se o padrão **Transactional Outbox**:
1. **Gravação Atômica**: Quando uma demanda é aceita (`accept`) ou quando um job conclui com sucesso gerando sucessores (`finish`), a mutação do job/run e a inserção do evento na tabela `outbox` ocorrem rigorosamente dentro do mesmo bloco transacional (`BEGIN ... COMMIT`).
2. **Despacho em Background**: O despachante de outbox seleciona os registros pendentes:
   ```sql
   SELECT outbox_id, event_type, payload 
   FROM outbox 
   WHERE status = 'pending' 
   ORDER BY outbox_id ASC 
   LIMIT 50 
   FOR UPDATE SKIP LOCKED;
   ```
3. **Idempotência no Despacho DBOS**:
   - Cada workflow DBOS é lançado com um `workflow_id` determinístico, derivado diretamente da tupla `JobKey`:
     `workflow_id = f"{run_id}:{ticket_id}:{stage}:{iteration}"`
   - O DBOS possui deduplicação nativa baseada em sua tabela de sistema (`dbos_workflow_status`). Se o despachante falhar logo após acionar o DBOS mas antes de atualizar o Outbox para `published`, a re-tentativa reenviará o mesmo `workflow_id`, garantindo execução exatamente-uma-vez (*effectively once*).
4. **Confirmação**: Após o retorno positivo do launch do DBOS, o registro do outbox é marcado como `published` via `ControlStore.materialize()`.

---

## 8. Matriz de Recuperação de Crash e Tolerância a Falhas

| Ponto de Falha | Detecção no Sistema | Impacto de Estado | Ação de Recuperação |
| --- | --- | --- | --- |
| **Crash antes do commit** | Abort/Rollback da transação no PostgreSQL/SQLite | Zero mutação no banco; nenhum lock retido; outbox limpo | O cliente recebe erro de conexão; reenvia o comando com o mesmo `external_id`, que será aceito normalmente sem duplicação. |
| **Crash durante execução (worker morre)** | Cessam os heartbeats de 10s; tempo atinge `now > expires_at` (45s decorridos) | Job permanece em `status='running'`, mas com lease expirado | O reconciliador (a cada 30s) detecta o lease expirado, marca o claim como `expired`, incrementa `retry_count`, avança o `fencing_token` e coloca o job de volta em `pending`. |
| **Ressurreição de worker zumbi** | Worker acorda e tenta chamar `finish()` ou `heartbeat()` com token antigo | O banco rejeita o update porque o `fencing_token` do job já foi incrementado | `ControlStore` lança `StaleLeaseError`. A transação sofre rollback imediato. Nenhum artefato ou evidência do zumbi é admitido. |
| **Crash durante operação externa** | `ExternalOperation` permanece em status `prepared` ou `sent` sem confirmação | Status da operação externa fica registrado como `unknown` | O worker em retry ou o reconciliador invoca `get_operation(key)`, consulta a API do provedor externo e reconcilia o estado real antes de qualquer nova tentativa de emissão de side-effect. |
| **Crash durante publicação do outbox** | Registro em `outbox` permanece `pending`, mas workflow DBOS já foi criado | Eventual tentativa duplicada de despacho | O despachante reenviará o evento com o mesmo `workflow_id` determinístico; a engine DBOS deduplica a chamada transparentemente. |

---

## 9. Conformidade Estrita com ADR-HF-001

A arquitetura formalizada neste binding atende 100% dos requisitos de governança e arquitetura estabelecidos no `ADR-HF-001`:
1. **Desacoplamento de Domínio**: Nenhum decorator DBOS (`@DBOS.workflow`, `@DBOS.step`) é importado ou utilizado na camada de domínio (`core/workflow/`). O domínio opera exclusivamente sobre contratos puros e o protocolo `ControlStore`.
2. **Isolamento de Infraestrutura**: O cliente DBOS é confinado exclusivamente ao adaptador de infraestrutura `core/orchestrator/adapters/dbos_adapter.py` e ao módulo cloud `core/orchestrator/adapters/control_postgres.py` (a serem desenvolvidos nos tickets sucessores).
3. **Importação Lazy**: O ambiente local e as suítes de testes unitários (`pytest tests/`) continuam rodando sem a biblioteca `dbos` instalada e sem requerer instância PostgreSQL, operando a custo $0 sob SQLite.
4. **Isolamento e Segurança de Credenciais**: A DSN do PostgreSQL nunca é trafegada via CLI, logs ou arquivos JSON. O acesso ocorre exclusivamente via variável de ambiente sob o padrão `SecretReference("DARKFAC_HF02_DATABASE_URL")`.

---

## 10. Validação e Critérios de Aceite para Handoff Sucessor

A conformidade estrutural deste binding foi validada deterministicamente pelo harness oficial da fábrica:

```powershell
python C:\dev\DarkFac\.factory\planning\continuous-autonomy\verify.py --binding HF-05-02
```

### Entregáveis Concluídos
1. `.factory/planning/continuous-autonomy/bindings/control.json` — Especificação legível por máquina do schema DDL, tipos, protocolo, outbox e ownership.
2. `docs/handoffs/continuous-autonomy/bindings/CONTROL.md` — Especificação normativa de alta arquitetura.

### Desbloqueios Sucessores
Com a aprovação deste binding, o DAG de autonomia contínua desbloqueia:
* **`HF-05-03`** (*Persistência canônica e adaptadores*): Implementação do protocolo `ControlStore`, dos adaptadores SQLite e PostgreSQL e da suíte de testes unitários/integração `tests/test_control_store.py`.
* **`HF-03-07`** (*Preflight por host e projeto*): Validação de conectividade e credenciais da VPS e instâncias PostgreSQL.
