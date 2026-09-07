# DF-22 — Integração de Provedores em Content & Visual Studios

## Resultado

O ticket **DF-22** foi implementado com 100% de sucesso, entregando:
1. Eliminação do código legado de chamadas HTTP diretas via `urllib.request` nos geradores de texto e imagem.
2. Unificação da detecção de chaves via `core.execution.providers.get_openrouter_api_key()`, eliminando código duplicado de consulta ao Windows Registry.
3. Integração com `ModelProvider` e injeção de provedores (`MockModelProvider`, `UnifiedModelProvider`) em `ContentEngine`, permitindo testes e geração determinísticos offline.
4. Suporte a reservas atômicas, execução e assentamento de tentativas via `ExecutionBudgetManager` tanto em `ContentEngine` quanto em `CloudVisualEngine` / `VisualStudio`.
5. Explicitação de qualidade reprovada no loop de crítica anti-slop (`quality_rejected`, `rejection_reason`) com preservação do rascunho inicial e métricas de violação.
6. Explicitação de failover e provedor trocado (`fallback_occurred`, `original_provider_requested`, `fallback_reason`) em `ContentResponse` e `VisualAssetResult`.
7. Contenção rígida em downloads externos via `_safe_download_image` com streaming em chunks e teto máximo inegociável de 25 MiB (`MAX_IMAGE_BYTES`), evitando consumo irrestrito de memória ou streams infinitos.
8. Expansão da suíte de testes em `tests/test_anti_slop_engine.py` e `tests/test_visual_studio.py` (7 novos testes focais, 36 testes passando sem regressão).
9. Validação do harness oficial com 100% de aprovação (309 passed, 1 skipped).

## Entregas Detalhadas

1. **Modelos de Domínio (`core/content/models.py` & `core/visual/models.py`)**:
   - `ContentResponse`: Adicionados campos `quality_rejected`, `rejection_reason`, `fallback_occurred`, `original_provider_requested`, `fallback_reason` e `cost_usd`, preservando retrocompatibilidade total na serialização `to_dict` / `from_dict`.
   - `VisualAssetResult`: Adicionados campos `fallback_occurred`, `fallback_reason` e `original_provider_requested`, mantendo conformidade com registros persistidos em disco.

2. **Motor Anti-AI-Slop (`core/content/engine.py`)**:
   - Construtor aceita `provider: Optional[ModelProvider] = None`.
   - Invocação de modelos locais e remotos migrada para `get_model_provider(...)` e `ModelProvider.generate(...)`.
   - Método `generate(...)` aceita `budget_manager` e `budget_id`, efetuando reservas atômicas e commits de `AttemptRecord`.
   - Loop de critique & polish marca `quality_rejected=True` e `rejection_reason` quando o rascunho inicial viola o limiar estipulado.
   - Failover automático para procedural registra explicitamente o motivo e o provedor que falhou.

3. **Ateliê Visual e Cloud Engine (`core/visual/cloud_engine.py` & `core/visual/studio.py`)**:
   - Remoção de consulta duplicada ao registro Windows; delegada a `get_openrouter_api_key()`.
   - `_safe_download_image(url, destination)`: Leitor defensivo em stream com inspeção de cabeçalho `Content-Length` e medição de bytes lidos, abortando com `ValueError` caso exceda 25 MiB.
   - Suporte a `ExecutionBudgetManager` em `CloudVisualEngine.generate(...)` e na fachada `VisualStudio`.
   - Falhas na nuvem sem modo estrito ativam fallback procedural com metadados explícitos.

4. **Cobertura de Testes**:
   - `test_engine_with_injected_mock_provider`: Valida injeção de `MockModelProvider`.
   - `test_engine_quality_rejection_explicit_tracking`: Valida ativação de `quality_rejected` e preservação de rascunho tóxico e versão polida.
   - `test_engine_provider_fallback_explicit_tracking`: Valida captura de exceção de rede e marcação de `fallback_occurred`.
   - `test_engine_execution_budget_manager_integration`: Valida reserva, commit e registro de attempt no SQLite para geração de texto.
   - `test_safe_download_image_enforces_max_bytes`: Valida rejeição por `Content-Length` e por streaming não anunciado superior a 25 MiB.
   - `test_cloud_engine_fallback_explicit_metadata`: Valida metadados de fallback na falha da nuvem.
   - `test_visual_engine_execution_budget_manager_integration`: Valida integração do ateliê visual com reservas e commits no SQLite.

## Validação Determinística

- `python core/harness/runner.py --quick` -> **`[HARNESS_PASS]`** (309 passed, 1 skipped)
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` -> **309 passed, 1 skipped** (100% de sucesso)
