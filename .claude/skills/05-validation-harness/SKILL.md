---
name: validation-harness
description: Constrói e opera validação determinística de sintaxe, tipos, unidade, integração, E2E headless e holdout; verifica oráculos, descoberta e vínculo das evidências ao candidato. Use para configurar ou diagnosticar o harness e avaliar a suficiência dos testes.
---

# 05 - Harness de Validação e Qualidade da Evidência

Esta skill opera a validação determinística da Dark Factory, assegurando que requisitos de produto sejam comprovados por oráculos independentes e emitam evidências auditáveis vinculadas ao candidato exato.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Candidato de Código**: SHA do commit, diff ou manifesto de arquivos com digest de build (`build_digest`).
- **Requisitos de Evidência**: Lista de `EvidenceRequirement` declarados no `WorkflowHandoff`.
- **Comandos de Teste**: `validate_commands` especificados no handoff do ticket.
- **Contexto Confiável**: `VerificationContext` fornecido pelo supervisor da fábrica.

### Ações e Procedimento Executável
1. **Escada de Validação em Quatro Níveis**:
   - **Nível 1 - Estático (Sintaxe e Tipos)**: Compilação AST e verificação de tipagem estrita (`compileall`, checagem de tipos Python 3.12+).
   - **Nível 2 - Unidade**: Cobertura focal de funções, regras de negócio e casos de borda com oráculos determinísticos.
   - **Nível 3 - Integração**: Testes de fronteira de persistência (SQLite, outbox, transações), subprocessos e concorrência.
   - **Nível 4 - E2E Headless**: Teste de ponta a ponta via biblioteca ou CLI com argumentos e arquivos reais, sem simulação in-process.
2. **Execução Oficial no DarkFac**:
   ```powershell
   python core/harness/runner.py --quick
   python -m pytest tests -v --ignore=tests/test_canaletto.py
   ```
3. **Emissão de Marcadores Determinísticos**:
   - Emita estritamente: `[STEP_START]`, `[STEP_PASS]`, `[STEP_FAIL]`, `[TEST_COUNT]`, `[HARNESS_PASS]` ou `[HARNESS_FAIL]`.
   - Capture contagens exatas de testes descobertos, passados e pulados.

### Outputs Estruturados
- **Evidências de Ambiente (`EnvironmentEvidence`)**: Instâncias tipadas contendo:
  - `evidence_id`: Identificador único do teste.
  - `requirement`: Requisito comprovado.
  - `environment_ref`: Referência do ambiente declarado no `EnvironmentManifest`.
  - `identity`: `SanitizedIdentity` do executor.
  - `build_digest` e `config_version`: Vinculados ao candidato.
  - `result`: `EvidenceResult.PASSED` ou `FAILED`.
  - `observed_at`: Timestamp com timezone explícito em UTC.
  - `evidence_ref`: Caminho do log auditável em `.factory/test_logs/`.
- **Recibo de Evidência (`EvidenceReceipt`)**: Registro para consumo do supervisor no `VerificationContext`.

### Portões, Política e Validação
- **Conformidade com ReadinessGate**: O portão `ReadinessGate.evaluate()` verifica se todas as evidências exigidas para o estágio atual (`stage_requirements`) foram satisfeitas.
- **Validade Temporal em UTC**: O supervisor calcula `0 <= now - observed_at <= max_age_seconds`. Evidências no futuro ou com idade superior ao TTL da política são rejeitadas.
- **Fail-Closed em Descoberta**: Zero testes descobertos ou zero checks executados configuram falha imediata.
- **Proibição de Bypass**: Flags de `freshness=current` autodeclaradas pelo candidato são ignoradas pelo supervisor; a validade é calculada exclusivamente pelo relógio confiável injetado.

---

## 2. Tratamento de Falhas e Regressões

- Quando um teste falhar, reproduza pelo menor caminho observável.
- Se a resolução exigir alteração de oráculo ou contrato público, devolva ao planejador qualificado (`WorkflowState.NEEDS_REPLAN`).
- Nunca enfraqueça uma asserção para forçar um resultado verde.
