# Relatório de Conclusão — CR-16: Qualificar Resultados e Oráculos do Experimento

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-16 (Trilha HF-02 Transporte / Oráculos HF-01, depende de CR-01/02/03/14)  
**Achados endereçados:** F13 (rejeição como extras de `environment_ref`, `validation_mode` e `target_differences` em `ScenarioResult`) e F15 (oráculos de catálogo verificando apenas comprimento 64 ou apenas constantes estáticas sem executar o pipeline de reconciliação com oráculo de mutação)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`, `spikes/runtime_choice/contracts.py`, `tests/test_runtime_spike_contracts.py`, `tests/test_runtime_spike_effects.py`, `tests/test_baseline_catalog.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Qualificação Estrita de Resultados (`ScenarioResult`)**:
   - Adicionado o enum `ValidationMode(StrEnum)` com os modos permitidos: `real_lab`, `target_environment`, `mock_only`.
   - Adicionados os campos obrigatórios `environment_ref: str` e `validation_mode: ValidationMode` (sem valores default, garantindo fail-closed para payloads antigos ou sem proveniência clara), além de `target_differences: list[str] = Field(default_factory=list)`.
   - Modos desconhecidos/inválidos são rejeitados com `ValidationError`.
   - Agregação em `RuntimeComparison`: a propriedade `all_target_differences` preserva e agrega todas as diferenças observadas entre o teste e o ambiente alvo; a propriedade `has_operational_evidence` exige que 100% dos resultados sejam do modo `target_environment` (resultados `mock_only` ou mistos nunca são promovidos a evidência operacional).

2. **Vínculo Fixo dos Catálogos por SHA-256 Explícito**:
   - Em `spikes/runtime_choice/effect_store.py`, `load_scenario_catalog` verifica o digest exato contra `EXPECTED_SCENARIO_CATALOG_SHA256 = "daba0f9e6304c3c84ad653b0e0a2011b36b4749caf6eb5c2f69537300bd39ce8"`; mutações não aprovadas no catálogo levantam `ValueError` de mismatch.
   - Em `tests/test_baseline_catalog.py`, o catálogo de fontes (`hf01-sources.json`), claims (`hf01-claims.json`) e fixtures (`baseline_cases.json`) são verificados contra seus hashes SHA-256 criptográficos aprovados:
     - `EXPECTED_CATALOG_SHA256`: `31c95fb515955af9e9710b501bdb59c38191e66bf474712156f43df288bb635d`
     - `EXPECTED_CLAIMS_SHA256`: `cd8c3f08d216def1a3bedfdc06d12ebf38349a06e4418c773210d59775c0cc03`
     - `EXPECTED_FIXTURES_SHA256`: `fbb1a3f444ad0c6e3a897490bd02f544cfd2b63d09d0fd63cab1c5ef888b59d3`

3. **Execução Dinâmica das Fixtures A1–A10 pelo Reconciliador**:
   - O teste de aceitação não apenas valida a sintaxe dos arquivos JSON estáticos: ele executa integralmente o pipeline de domínio `reconcile_baseline` para cada caso A1 a A10, verificando os assessments derivados, blockers e códigos de issues esperados.
   - Teste adversarial de mutação (`test_baseline_catalog_and_fixtures_mutation_rejection`): prova que mutações indevidas (como adulterar uma expectativa para `verified` ou corromper o hash de uma fonte) derrubam a comparação e são detectadas como `stale_evidence` ou `claim_source_missing`.

---

## 2. Arquivos Modificados

| Arquivo | Descrição |
|---|---|
| `spikes/runtime_choice/contracts.py` | Definição de `ValidationMode`, atualização de `ScenarioResult` com `environment_ref`, `validation_mode`, `target_differences`, métodos de agregação e qualificação em `RuntimeComparison`, exportações em `__all__`. |
| `spikes/runtime_choice/__init__.py` | Exportação de `ValidationMode`. |
| `spikes/runtime_choice/effect_store.py` | Definição de `EXPECTED_SCENARIO_CATALOG_SHA256` e enforcement do hash em `load_scenario_catalog`. |
| `tests/test_runtime_spike_contracts.py` | Atualização de instâncias de `ScenarioResult`, novo teste de roundtrip, rejeição de modos inválidos/antigos sem origem e preservação de `target_differences`. |
| `tests/test_runtime_spike_effects.py` | Teste de verificação e falha sob mutação do catálogo R01–R12. |
| `tests/test_baseline_catalog.py` | Validação estrita de SHA-256 dos catálogos, execução dinâmica de A1–A10 pelo reconciliador e teste de rejeição de mutação. |

---

## 3. Validação Executada

### 3.1 Comando Focal do Ticket CR-16
```powershell
python -m pytest tests/test_runtime_spike_contracts.py tests/test_runtime_spike_effects.py tests/test_baseline_catalog.py -v
```
Resultado: **16 passed in 4.31s**
