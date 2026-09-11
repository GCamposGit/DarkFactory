# Relatório de Conclusão — CR-02: Vincular Claims e Limitar Promoção no HF-01

O ticket **CR-02** (Trilha HF-01 Confiança da Baseline) foi concluído com sucesso, remediando em definitivo o achado impeditivo **F05** e os requisitos de vinculação e detecção de conflitos de claims formalizados em `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md` e `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`.

---

## 1. Escopo e Invariantes Atendidas

1. **Claims Documentais e Fontes Importadas**:
   - Claims documentais e de fontes importadas permanecem no máximo como `reported`.
   - Nenhuma claim remota `remote_git` não atestada por recibo confiável de merge/PR é promovida a `verified`, mesmo quando `candidate_sha == base_sha`.
2. **Probes de Simulação e Health Checks**:
   - Probes simulados (`ValidationMode.SIMULATION` ou ambiente de teste como `unit-test`) e verificações HTTP de disponibilidade atestam apenas disponibilidade ou simulação, resultando no máximo em `reported` (ou `unknown`), nunca `verified`.
3. **Preservação de Evidências Sanitizadas no Snapshot**:
   - O contrato [`BaselineSnapshot`](file:///c:/dev/DarkFac/core/planning/baseline_models.py) e [`CollectedBaseline`](file:///c:/dev/DarkFac/core/planning/baseline_models.py) passam a reter e serializar explicitamente a lista de [`ProbeObservation`](file:///c:/dev/DarkFac/core/planning/baseline_models.py) (`probe_observations`).
   - Todos os IDs de probes correspondentes são preservados no campo `evidence_ids` de [`CapabilityAssessment`](file:///c:/dev/DarkFac/core/planning/baseline_models.py).
4. **Resolução de Contradições e Detecção por Escopo**:
   - Claims positiva e negativa no *mesmo escopo* (`scope`) avaliam a dimensão para `AssessmentDimension.CONTRADICTED` e geram uma issue estruturada de código `claim_conflict`.
   - Claims positiva e negativa em *escopos distintos* (ex: declaração de manifesto vs relatório de execução) avaliam para `AssessmentDimension.PARTIAL` sem gerar conflito artificial.
5. **Validação Estrita de Integridade da Fonte e Locator**:
   - O reconciliador valida se `claim.source_id` existe nas observações coletadas, se o status é `READ`, se `claim.source_hash == obs.sha256` e se o arquivo referenciado em `claim.locator` confere com `obs.relative_path`.
   - Qualquer discrepância gera issues estruturadas (`stale_evidence`, `claim_source_missing`, `claim_source_unreadable`, `claim_locator_invalid`) e desqualifica a claim para uso positivo, mantendo a dimensão em `unknown`.

---

## 2. Arquivos Modificados

- [`core/planning/baseline_models.py`](file:///c:/dev/DarkFac/core/planning/baseline_models.py):
  - Definição canônica de [`ProbeStatus`](file:///c:/dev/DarkFac/core/planning/baseline_models.py) e [`ProbeObservation`](file:///c:/dev/DarkFac/core/planning/baseline_models.py).
  - Adição do campo `probe_observations: list[ProbeObservation] = Field(default_factory=list)` em `CollectedBaseline` e `BaselineSnapshot`.
- [`core/planning/baseline_probes.py`](file:///c:/dev/DarkFac/core/planning/baseline_probes.py):
  - Importação e re-exportação de `ProbeObservation` e `ProbeStatus` de `baseline_models.py`, preservando compatibilidade de API e evitando imports circulares.
- [`core/planning/baseline_reconcile.py`](file:///c:/dev/DarkFac/core/planning/baseline_reconcile.py):
  - Deduplicação determinística de probes (`_dedupe_probes`).
  - Funções de checagem e emissão de issues: `_claim_validation_issues`, `_claim_conflict_issues`, `_is_claim_valid`, `_is_locator_matching`.
  - Refatoração de `_assessment_for`: elimina promoções indevidas a `verified`, detecta contradições mesmo-escopo vs escopos distintos, avalia probes para `reported` e agrega IDs de probes em `evidence_ids`.
- [`tests/test_baseline_reconcile.py`](file:///c:/dev/DarkFac/tests/test_baseline_reconcile.py):
  - Inclusão de 5 novos testes de regressão e aceitação focais.

---

## 3. Evidências de Validação

### Script Oficial de Reprodução do Achado F05
Comando: `python .factory/reviews/hf-critical-20260909/reproduce_baseline_runtime.py`
Resultado:
- `unattested_remote_claim`: `"reported"` (era `"verified"`)
- `positive_negative_same_scope`: `{"assessment": "contradicted", "issues": ["HF02_RUNTIME_EVIDENCE_INCOMPLETE", "claim_conflict"]}` (era `"reported"`, sem issues de conflito)
- `simulation_probe`: `{"operation": "reported", "claims": 0, "serialized_probe_retained": true}` (era `"verified"`, probe não era retido no JSON)

### Suíte de Testes Focais do Módulo
Comando: `python -m pytest tests/test_baseline_reconcile.py tests/test_baseline_probes.py tests/test_baseline_catalog.py tests/test_baseline_cli.py tests/test_baseline_sources.py -v`
Resultado:
```
37 passed, 1 skipped in 0.63s (100% PASS)
```

### Quick Harness Oficial da Fábrica
Comando: `python core/harness/runner.py --quick`
Resultado:
```
[STEP_PASS] syntax_and_types
[STEP_PASS] unit_and_integration_tests
[TEST_COUNT] count=511
[HARNESS_PASS] 509 passed, 2 skipped in 116.18s
```
