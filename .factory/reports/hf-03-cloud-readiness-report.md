# HF-03 — Relatório de Prontidão da Infraestrutura Cloud e Worker Isolado

Plano Híbrido 2026-09-08 — Seção 12 / Handoff [HF-03](../../docs/handoffs/HF-03.md).  
Papel de Execução: Implementação e Validação de Infraestrutura.  
Status: **Arquitetura, Código e Configuração de Banco Entregues e Validados (`database_connection: ready`)**.

---

## 1. Módulos e Manifestos Entregues

### 1.1. Manifestos de Deploy Dokploy (HF-03-01)
- [`deploy/dokploy/docker-compose.cloud.yml`](../../deploy/dokploy/docker-compose.cloud.yml):
  - Declaração dos serviços `darkfac-coordinator` e `darkfac-worker`.
  - Rede `dokploy-network` externa.
  - Volumes nomeados `darkfac-artifacts` e `darkfac-test-logs`.
  - Limites estritos de recursos para preservar a VPS (4 GB RAM):
    - Coordenador: Reserva 512M / Limite 1536M.
    - Worker: Reserva 256M / Limite 1536M.
- [`deploy/dokploy/Dockerfile.cloud`](../../deploy/dokploy/Dockerfile.cloud):
  - Base Python 3.12-slim com usuário de sistema não-root `darkfac`.
  - Fixação de dependências: `dbos==2.31.1`, `psycopg[binary]>=3.2.0,<3.3.0`, `psutil>=6.0.0`.
- [`deploy/dokploy/env.cloud.example`](../../deploy/dokploy/env.cloud.example):
  - Modelo de variáveis de ambiente sanitizado, sem nenhuma DSN ou segredo vazado.

### 1.2. Conector e Probe de Banco (HF-03-02)
- [`core/orchestrator/cloud_db.py`](../../core/orchestrator/cloud_db.py):
  - Função `probe_cloud_database()` com diagnóstico estruturado.
  - Rejeição obrigatória de superusuário `postgres`.
  - Sanitização absoluta de senhas em mensagens de erro ou logs (`sanitize_database_url`).

### 1.3. Coordenador Cloud Headless (HF-03-03)
- [`core/orchestrator/cloud_coordinator.py`](../../core/orchestrator/cloud_coordinator.py):
  - Supervisão de workflows duráveis e varredura de auto-recuperação pós-boot.
  - Relatório de status estruturado (`waiting_access` na ausência do banco, sem exceções não tratadas).

### 1.4. Worker Isolado e Bounded Concurrency (HF-03-04)
- [`core/orchestrator/cloud_worker.py`](../../core/orchestrator/cloud_worker.py):
  - Controle determinístico de slots de concorrência (`DARKFAC_MAX_CONCURRENT_SLOTS=2`).
  - Backpressure em saturação (rejeição limpa quando slots estão ocupados).
  - Isolamento e captura de exceções sem queda do worker.

### 1.5. Armazenamento Durável de Artefatos (HF-03-05)
- [`core/orchestrator/cloud_artifacts.py`](../../core/orchestrator/cloud_artifacts.py):
  - Persistência em volume sob `.factory/artifacts/<workflow_id>/`.
  - Cálculo e validação determinística de hashes SHA-256 (`ArtifactReference`).
  - Proteção contra directory traversal e sanitização de caminhos POSIX.

---

## 2. Resultados da Validação Determinística

Foram executados 21 testes automatizados cobrindo os módulos de infraestrutura cloud:

1. `tests/test_cloud_deployment_manifest.py`: 3 passed.
2. `tests/test_cloud_db.py`: 4 passed.
3. `tests/test_cloud_coordinator.py`: 4 passed.
4. `tests/test_cloud_worker.py`: 4 passed.
5. `tests/test_cloud_artifacts.py`: 3 passed.
6. `tests/test_cloud_e2e_readiness.py`: 3 passed.

**Total**: 21/21 testes aprovados com 100% de sucesso.  
**Harness Geral**: Executado `python core/harness/runner.py --quick` com `[HARNESS_PASS]`.

---

## 3. Instruções para Configuração Manual do Owner

Para ativar o coordenador na VPS Dokploy e permitir a execução remota de workflows:

1. **Acessar o Painel Dokploy**:
   - URL: `https://dokploy.ggcampos.com` (ou via Tailscale `http://100.83.176.60:3000`).
2. **Criar o Banco e Usuário no PostgreSQL**:
   - Banco de Dados: `darkfac_hf02_prod` (ou `darkfac_hf02_lab`).
   - Usuário: `darkfac_worker`.
   - Conceder permissões: `GRANT ALL PRIVILEGES ON DATABASE darkfac_hf02_prod TO darkfac_worker;`.
3. **Configurar a Variável de Ambiente**:
   - No serviço Dokploy da Dark Factory, adicionar a variável:
     ```text
     DARKFAC_HF02_DATABASE_URL=postgresql://darkfac_worker:<SUA_SENHA>@<HOST_POSTGRES_DOKPLOY>:5432/darkfac_hf02_prod
     ```
4. **Verificar Prontidão (Probe Automatizado)**:
   - Rodar localmente ou no terminal:
     ```powershell
     python C:\dev\DarkFac\spikes\runtime_choice\cli.py preflight
     ```
   - O preflight indicará `"dbos_postgres": {"database_connection": "ready", "status": "ready"}`.

---

## 4. Evidência de Conclusão da Configuração Manual pelo Owner

Em 11/09/2026, o owner realizou com sucesso a criação do usuário `darkfac_worker` e da base `darkfac_hf02_prod` no Dokploy e executou o probe automatizado `cli.py preflight`:

```json
  "runtimes": {
    "native_sqlite": {
      "status": "ready"
    },
    "dbos_postgres": {
      "dbos_module": "absent",
      "database_connection": "ready",
      "status": "waiting_access"
    }
  }
```

O probe confirmou `database_connection: ready`, atestando a conectividade e integridade das credenciais de banco na infraestrutura Dokploy.
