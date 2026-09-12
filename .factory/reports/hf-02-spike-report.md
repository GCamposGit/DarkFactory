# HF-02-07 — Relatório Técnico do Experimento de Runtimes

Plano Híbrido 2026-09-08 — Seção 12 / Handoff [HF-02](../../docs/handoffs/HF-02.md).  
Papel de Execução: Econômico (Implementação, Execução Real e Oráculos).  
Decisão Arquitetural: Reservada ao planejador de alta inteligência em **HF-02-08**.

---

## 1. Proveniência e Metadados do Experimento

| Parâmetro | Valor Registrado | Observações |
| :--- | :--- | :--- |
| **Lab ID** | `lab-acceptance-20260911` | Diretório: `.factory/experiments/HF-02/lab-acceptance-20260911/` |
| **Git Code SHA** | `3ba5bdb3e291e4bcc7b0fbb1c807a85cc6174e12` | Base após remediações críticas CR-01 a CR-16 |
| **Baseline Snapshot Hash** | `1f931fcccc2f4a54cde1f0ba09825094dbe429af` | Hash imutável da baseline HF-01 |
| **Scenario Catalog Hash** | `daba0f9e6304c3c84ad653b0e0a2011b36b4749caf6eb5c2f69537300bd39ce8` | Catálogo R01–R12 congelado em `scenarios.json` |
| **Data / Hora UTC** | `2026-09-11T21:43:06Z` | Coleta automatizada via CLI do laboratório |
| **Ambiente Host** | Python 3.12.10 / Windows (win32) | Execução local isolada, sem rede externa |
| **Referência de Ambiente** | `docs/handoffs/HF-02-ENVIRONMENT.md` | Declaração de isolamento e variáveis |
| **Status da Decisão** | `pending_architect_review` | Nenhum runtime pré-selecionado pelo executor |

---

## 2. Comandos Executados e Códigos de Saída

Conforme a convenção estrita de códigos de saída da CLI (`CliExitCode`):
- `0`: Sucesso total (todos os gates obrigatórios da variante satisfeitos).
- `1`: Falha de asserção ou capacidade obrigatória `unsupported`.
- `2`: Ambiente / dependência externa bloqueante (`waiting_access`).
- `3`: Erro de contrato ou corrupção de manifest / runner.

### 2.1. Preflight
```powershell
python -m spikes.runtime_choice.cli preflight
```
- **Exit Code**: `0` (SUCCESS).
- **Diagnóstico**: Catálogo íntegro (`matches_frozen_digest: true`), runtime `native_sqlite` pronto, runtime `dbos_postgres` em `waiting_access` por ausência de `DARKFAC_HF02_DATABASE_URL`.

### 2.2. Execução da Suíte de Aceitação
```powershell
python -m spikes.runtime_choice.cli run --runtime both --suite acceptance --out .factory/experiments/HF-02/lab-acceptance-20260911 --lab-id lab-acceptance-20260911 --lease-seconds 1.0
```
- **Exit Code**: `2` (`CliExitCode.ENVIRONMENT_BLOCKED`).
- **Comportamento**: 
  - `native_sqlite` executou os 12 cenários R01–R12 de forma determinística em subprocessos com JSONL.
  - `dbos_postgres` registrou `status: blocked` com código estruturado `STORE_UNAVAILABLE` para todos os 12 cenários, sem fabricação de mocks.
  - O comparador registrou `has_blocked = True` e emitiu código de saída 2, preservando a honestidade do ambiente.

### 2.3. Verificação Formal do Manifesto
```powershell
python -m spikes.runtime_choice.cli verify --manifest .factory/experiments/HF-02/lab-acceptance-20260911/manifest.json
```
- **Exit Code**: `2` (`CliExitCode.ENVIRONMENT_BLOCKED`).
- **Stderr**: `[BLOCKED] 12 scenarios blocked on environment/access`.
- **Integridade**: Zero falhas de asserção (`fail_count: 0`), esquema Pydantic v2 validado, catálogo conferido, ausência de contaminação por `mock_only`.

### 2.4. Resumo Tabular
```powershell
python -m spikes.runtime_choice.cli summarize --manifest .factory/experiments/HF-02/lab-acceptance-20260911/manifest.json
```
- **Exit Code**: `0` (SUCCESS).

---

## 3. Matriz de Capacidades

A matriz de capacidades foi avaliada diretamente a partir dos contratos do adaptador (`AdapterCapabilities`):

| Capacidade | Native SQLite | DBOS PostgreSQL | Descrição Técnica |
| :--- | :---: | :---: | :--- |
| **Durable Steps** | **True** | **True** | Checkpoints persistentes após cada etapa do workflow |
| **Resume After Crash** | **True** | **True** | Retomada em novo processo a partir do último checkpoint confirmado |
| **Durable Wait** | **False** | **True** | Suspensão durável sem bloqueio de thread à espera de aprovação humana |
| **Deduplicated Intake** | **False** | **True** | Rejeição/idempotência de workflows duplicados no ponto de ingresso |
| **Cancel Before Next Step** | **False** | **True** | Interrupção cooperativa do workflow antes do início do próximo step |
| **Version Isolation** | **False** | **True** | Convivência de v1 e v2 sem contaminação mútua de execuções |
| **Bounded Concurrency** | **False** | **True** | Filas duráveis com limite determinístico de concorrência ativa |

---

## 4. Resultados Detalhados por Cenário (R01–R12)

### 4.1. Baseline: `native_sqlite`
| Cenário | Status | Duração (ms) | Retomada (ms) | RSS Pico (MiB) | Efeitos | Código / Detalhe |
| :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| **R01** (Execução padrão S0–S4) | **PASS** | 390.0 | - | 34.2 | 1 | Sucesso nominal, 4 invocações de etapa, 1 efeito confirmado |
| **R02** (Crash e reinício) | **PASS** | 1657.0 | 1391.0 | 34.1 | 1 | Reinício comprovado em PID distinto, lease respeitado |
| **R03** (Desconexão no commit) | **PASS** | 1750.0 | 1375.0 | 34.1 | 1 | Commit remoto confirmado, reconexão sem duplicação de efeito |
| **R04** (Espera de aprovação) | **UNSUPPORTED** | 218.0 | - | 23.7 | 0 | `CAPABILITY_UNSUPPORTED:durable_wait` |
| **R05** (Ingresso duplicado) | **UNSUPPORTED** | 454.0 | - | 31.3 | 1 | `CAPABILITY_UNSUPPORTED:deduplicated_intake` |
| **R06** (Digest de aprovação) | **UNSUPPORTED** | 218.0 | - | 24.4 | 0 | `CAPABILITY_UNSUPPORTED:durable_wait` |
| **R07** (Cancelamento pré-step) | **UNSUPPORTED** | 219.0 | - | 24.9 | 0 | `CAPABILITY_UNSUPPORTED:durable_wait` |
| **R08** (Isolamento v1/v2) | **UNSUPPORTED** | 203.0 | - | 24.2 | 0 | `CAPABILITY_UNSUPPORTED:version_isolation` |
| **R09** (Store indisponível) | **PASS** | 188.0 | - | - | 0 | `STORE_UNAVAILABLE` detectado no startup sem fallback silencioso |
| **R10** (Concorrência limitada) | **UNSUPPORTED** | 218.0 | - | 23.9 | 0 | `CAPABILITY_UNSUPPORTED:bounded_concurrency` |
| **R11** (Corrupção de relatório) | **PASS** | 1.0 | - | - | 0 | `integrity_mismatch_rejected` pelo verificador |
| **R12** (Consulta pós-sucesso) | **PASS** | 375.0 | - | 34.2 | 1 | Estado terminal retido sem re-execução de efeitos |

- **Taxa de Sucesso nos Suportados**: 6/6 (100% de aprovação nos cenários que a engine SQLite nativa suporta).
- **Gaps Conhecidos da Baseline**: 6 cenários retornaram `unsupported` estrito, sem mascaramento nem implementação de engine paralela no adaptador.

### 4.2. Candidato: `dbos_postgres`
| Cenário | Status | Duração (ms) | Efeitos | Código / Detalhe |
| :--- | :---: | :---: | :---: | :--- |
| **R01–R12** | **BLOCKED** | 0.0 | 0 | `STORE_UNAVAILABLE` (`waiting_access`), `assertions.database_available = False` |

- **Elegibilidade**: Atualmente `eligible: false` devido a bloqueio de acesso ao PostgreSQL descartável.
- **Conformidade de Segurança**: Nenhuma senha ou DSN foi impressa em logs, stdout ou JSON. O erro é puramente estruturado.

---

## 5. Diferenças entre Laboratório e Ambiente Alvo

1. **Topologia de Rede**:
   - O laboratório opera em loopback local (`127.0.0.1`) com `ThreadingHTTPServer` e `NativeEffectStore` em SQLite dedicado em `<lab-root>/effects/`.
   - O ambiente alvo da produção requer PostgreSQL 16+ na VPS privada com TLS mútuo e firewalls configurados.
2. **Ciclo de Vida de Processos**:
   - No laboratório, o `ScenarioController` inicia e monitora PIDs individuais via subprocessos Python no Windows.
   - Na VPS de destino, workers serão orquestrados via Dokploy/Docker em Linux, exigindo gerenciamento de sinais POSIX (`SIGTERM`).
3. **Persistência de Efeitos**:
   - No laboratório, os efeitos são validados contra o serviço de efeito local com endpoint `/effects` idempotente por `operation_key`.
   - Na produção, os efeitos reais serão despachados para GitHub, Dokploy e Telegram.

---

## 6. Insumos e Decisões para HF-02-08

Este relatório e os artefatos em `.factory/experiments/HF-02/lab-acceptance-20260911/` (`manifest.json`, `results.json`, `COMPARISON.md`) transferem para o papel de **Alta Inteligência (HF-02-08)** a base fática necessária para:
1. Redigir o Architecture Decision Record (`docs/decisions/ADR-HF-001-runtime.md`).
2. Avaliar se o acesso ao PostgreSQL de laboratório deve ser provisionado pelo owner antes de fechar a decisão, ou se a decisão de contingência (manter baseline SQLite com wrappers de scheduler no HF-05) deve ser ativada.
3. Produzir `docs/handoffs/HF-03-INPUTS.md` definindo os requisitos de banco, credenciais, workers e rotas.
