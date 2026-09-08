# DF-17 — Relatório de proveniência de benchmarks

## Resultado

Implementação concluída. O ledger agora distingue valor observado, reportado, derivado, estimado e desconhecido por campo. Modelos descobertos apenas via catálogo live mantêm scores de capacidade como `null`; heurísticas de nome não são promovidas a medição nem entram na fronteira Pareto.

## Alterações

- `core/benchmarks/models.py`
  - adicionados `MetricQuality`, `MetricAcquisitionMode` e `FieldProvenance`;
  - cada `ModelBenchmarkEntry` serializa `field_provenance` com `source`, `observed_at`, `unit`, `acquisition_mode` e qualidade;
  - valores ausentes são opcionais/desconhecidos;
  - `is_measured()` exclui derivados e heurísticas; `is_rankable()` permite custo derivado somente quando seus insumos são conhecidos.
- `core/benchmarks/fetcher.py`
  - baseline usa os defaults de proveniência do catálogo, sem defaults numéricos fabricados;
  - Artificial Analysis, quando configurada, atualiza apenas campos explicitamente retornados;
  - OpenRouter atualiza apenas preços/metadados live e não relabela scores de benchmark;
  - modelos novos sem capacidade medida permanecem no ledger com scores desconhecidos e são excluídos do ranking;
  - novo `offline=True` bloqueia chamadas externas e mantém fixtures determinísticas.
- `core/benchmarks/data/benchmark_catalog.json`
  - adicionados data de observação e proveniência por campo para o fixture offline canônico.
- `core/benchmarks/__init__.py`
  - contratos de proveniência exportados pela API do pacote.
- `tests/test_model_benchmark.py`
  - cobertura de round-trip, desconhecido, fixture offline, descoberta live sem score heurístico e merge de preço/score com fontes distintas.

## Invariantes verificados

1. Todo campo de métrica reportado pelo fixture carrega fonte, data, unidade e modo de aquisição.
2. `None` permanece `None`; não há fallback silencioso para score, velocidade, latência ou tokens por tarefa.
3. `cost_per_task` calculado a partir de preços conhecidos é marcado como `derived`, não como medição.
4. Simulação, `validator_only` e `live` de DF-05 continuam contratos distintos; este ticket não altera `racing.py`.
5. Nenhum arquivo de Canaletto foi alterado.

## Validação

- `python -m pytest tests/test_model_benchmark.py -v` — **11 passed**.
- `python core/harness/runner.py --quick` — **[HARNESS_PASS]**, 190 passed, 1 skipped.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` — **190 passed, 1 skipped, 2 warnings**.
- `git diff --check` nos arquivos do ticket — passou.
- smoke offline e smoke de descoberta live — passaram.

Os comandos foram executados com o Python 3.12 empacotado pelo workspace e `TEMP`/`TMP` isolados em diretório gravável, pois o `python` global não estava no PATH e o diretório temporário global estava restrito nesta sessão.

## RCA

O primeiro smoke excluiu modelos do ranking porque `is_rankable()` confundia “medido” com “utilizável”: o custo derivado de preços observados é derivado, mas ainda é válido para ranking. A correção separou as duas propriedades sem promover o derivado a medição. RCA registrada no ledger como `rca_59c9f04c`.

O primeiro harness falhou por ambiente (`python` ausente no PATH e temp global sem permissão), enquanto os testes do benchmark passavam. RCA registrada como `rca_0e5f259a`; o rerun com runtime/temp explícitos passou integralmente.
