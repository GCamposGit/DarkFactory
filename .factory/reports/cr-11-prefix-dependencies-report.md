# Relatório de Conclusão — CR-11: Corrigir Dependências Entre Prefixos

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-11 (Trilha HF-01 Confiança, Complementos Locais)  
**Achado endereçado:** F14 (tokens com prefixo completo vazavam números para o parser de números sem prefixo, gerando dependências fantasmas e auto-ciclos artificiais)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`, `core/planning/baseline_sources.py`, `tests/test_baseline_sources.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Consumo Integral de Spans de Tokens com Prefixo (Anti-Ghost & Anti-Cycle)**:
   - Tokens com prefixo completo consomem seus spans de caracteres antes de qualquer interpretação de números/ranges sem prefixo (`bare IDs / bare ranges`).
   - Tokens com prefixos conhecidos (`DF`, `RM`, `HF`, `USR`, `INFRA` ou o prefixo da própria tabela) emitem seus IDs canônicos normatizados e registram o prefixo ativo posicional.
   - Tokens com prefixos desconhecidos (ex.: `UNKNOWN-11`, `FOO-01`, `UTF-8`, `RFC-2119`, `ISO-9001`) consomem seus spans sem emitir vínculo e sem deixar resíduos numéricos (`11`, `01`, `8`, `2119`), eliminando a geração de dependências fantasmas (`HF-11`, `HF-08`, etc.).
   - Em itens como `HF-11` dependendo de `DF-11`, o resultado é estritamente `['DF-11']` (sem gerar auto-dependência `HF-11 -> HF-11`).
   - Na catalogação real, corrigiu a extração do item `HF-12` em `docs/HYBRID_WORKFLOW_PLAN_2026-09-08.md` (`HF-03, HF-11, INFRA-08, INFRA-09`), eliminando os fantasmas `HF-08` e `HF-09` e mantendo estritamente `['HF-03', 'HF-11', 'INFRA-08', 'INFRA-09']`.

2. **Gramática Estrutural de Continuação de Prefixos e Delimitadores**:
   - Continuações como `HF-01, 02` herdam o prefixo ativo do token precedente (`HF`), produzindo `['HF-01', 'HF-02']`.
   - Listas com múltiplos prefixos e continuações locais (`HF-01, 02, DF-05, 06`) associam cada número sem prefixo ao token precedente mais próximo no texto (`HF-01, HF-02, DF-05, DF-06`).
   - Conjunções estruturais (`e`, `and`) e pontuações de lista (`,`, `;`, `/`, `:`) funcionam como delimitadores válidos.
   - Números em prosa livre (ex.: `Python 3.12`, `porta 8080`, `ver nota 42`, `(revisão 2)`) não são precedidos por delimitadores de lista e são integralmente ignorados, sem inventar vínculos arbitrários.

3. **Deduplicação e Preservação de Ordem**:
   - Ranges e IDs declarados repetidamente (ex.: `HF-01, HF-01` ou `02, 02`) são deduplicados deterministicamente via `dict.fromkeys` preservando a ordem posicional de aparição no documento.

---

## 2. Arquivos Modificados

| Arquivo | Mudança |
|---|---|
| `core/planning/baseline_sources.py` | Definido `KNOWN_PREFIXES`; implementadas funções de validação delimitadora `_is_valid_delimiter_before` e `_is_valid_delimiter_after`; reimplementado `_parse_markdown_dependencies` com passadas determinísticas de consumo de spans e herança posicional de prefixos ativos. |
| `tests/test_baseline_sources.py` | Adicionadas 6 funções de teste de aceitação cobrindo controle positivo (`DF-11` em tabela HF sem ciclo), remoção dos fantasmas de `HF-12`, combinações gramaticais e herança de prefixo, prefixos mistos e ranges, deduplicação de repetidos e rejeição de prosa/prefixos desconhecidos. |

---

## 3. Validação Executada

### 3.1 Testes de Unidade Focais
```powershell
python -m pytest tests/test_baseline_sources.py -v
```
Resultado: **12 passed, 1 skipped in 0.39s**

### 3.2 Testes Integrados da Trilha Baseline
```powershell
python -m pytest tests/test_baseline_cli.py tests/test_baseline_reconcile.py tests/test_baseline_catalog.py -v
```
Resultado: **48 passed in 2.84s**

### 3.3 Harness Determinístico Completo
```powershell
python core/harness/runner.py --quick
```
Resultado: **608 passed, 2 skipped in 88.27s (0:01:28)**  
`[STEP_PASS] syntax_and_types`  
`[STEP_PASS] unit_and_integration_tests`  
`[TEST_COUNT] count=610`  
`[HARNESS_PASS]`

### 3.4 Suíte Geral Pytest
```powershell
python -m pytest tests -v
```
Resultado: **608 passed, 2 skipped in 91.52s (0:01:31)**
