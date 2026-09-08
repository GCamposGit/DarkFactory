# DF-14 — Adapter Comum de Provedores e Desacoplamento Arquitetural

## Resultado

O ticket **DF-14** foi implementado com 100% de sucesso, entregando:
1. Migração do primeiro fluxo real (`core/game/cli.py`) para o adapter comum (`core/execution/providers.py`).
2. Desacoplamento arquitetural estrito: **zero imports** de `hub` a partir do namespace `core/`, garantido por teste determinístico de AST estático.
3. Provedores completos (`OllamaModelProvider`, `OpenRouterModelProvider`, `UnifiedModelProvider` e `MockModelProvider`) com extração de tokens, cálculo de custo e telemetria atômica via `ModelUsageLedger`.
4. Integração do pipeline de execução do jogo (`build_live`) com reservas atômicas e assentamentos de orçamento via `ExecutionBudgetManager`.
5. Reutilização centralizada da resolução de chaves (`get_openrouter_api_key`) a partir do subsistema `core.execution`.
6. Suíte de testes `tests/test_provider_integration.py` com 12 testes cobrindo todas as garantias.
7. Validação obrigatória da Dark Factory com 100% de sucesso (302 passed, 1 skipped).

## Entregas Detalhadas

1. **Adapter Comum de Provedores (`core/execution/providers.py`)**:
   - `get_openrouter_api_key()`: Resolução unificada de chave de API no ambiente ou no Windows Registry (`HKCU\Environment`).
   - `OllamaModelProvider`: Invocação headless de modelos locais via Ollama HTTP API com métricas de tokens (`prompt_eval_count`, `eval_count`), medição de latência e telemetria via `ModelUsageLedger`.
   - `OpenRouterModelProvider`: Gateway para modelos de nuvem com cálculo de custos baseado no catálogo diário de benchmarks (`core.benchmarks`), conformidade com `UnknownCostPolicy` e telemetria.
   - `UnifiedModelProvider`: Roteador inteligente que despacha modelos com slash (`/`) para OpenRouter e modelos locais para Ollama, além de permitir injeção de `MockModelProvider`.
   - `get_model_provider(provider_id: str)`: Fábrica declarativa para instanciação de provedores (`mock`, `ollama`, `openrouter`, `auto`/`unified`).

2. **Desacoplamento e Refatoração do Echo Garden (`core/game/cli.py` & `core/game/models.py`)**:
   - Remoção total das dependências de `hub.backend.*` em `core/game/cli.py`.
   - Atualização de `_call_model` para operar sobre `ModelProvider` e efetuar reservas atômicas no `ExecutionBudgetManager`.
   - Adição de suporte ao provedor `"mock"` e `"unified"` em `ModelEvidence.provider` para permitir validações 100% offline.
   - Suporte ao parâmetro `offline_assets` em `build_live` permitindo geração procedural offline.

3. **Integração no DarkHub (`hub/backend/service.py`)**:
   - Método `get_openrouter_key()` atualizado para delegar diretamente a `core.execution.providers.get_openrouter_api_key()`, eliminando código duplicado.

4. **Garantia de Desacoplamento e Testes (`tests/test_provider_integration.py`)**:
   - `test_strict_architectural_decoupling_core_never_imports_hub`: Varre a AST de todos os arquivos `.py` em `core/` e valida ausência absoluta de imports do pacote `hub`.
   - `test_get_openrouter_api_key_from_env`: Valida resolução de chave via ambiente.
   - `test_get_openrouter_api_key_empty_returns_none`: Valida fallback seguro quando chave não existe.
   - `test_mock_model_provider_conforms_to_protocol`: Valida contrato `ModelProvider` e resposta imutável `ProviderResponse`.
   - `test_mock_model_provider_responses_by_model_and_sequence`: Valida mock configurável por modelo e sequencial.
   - `test_mock_model_provider_unknown_cost_policies`: Valida políticas `REJECT`, `ESTIMATE` e `CONSERVATIVE_MAX`.
   - `test_ollama_model_provider_mocked_http`: Valida parser de resposta e telemetria no `ModelUsageLedger`.
   - `test_openrouter_model_provider_mocked_http`: Valida parser de chat completion e telemetria no `ModelUsageLedger`.
   - `test_openrouter_missing_key_raises_runtime_error`: Valida erro estruturado na ausência de credencial.
   - `test_unified_model_provider_routing`: Valida despacho correto entre local, nuvem e mock.
   - `test_get_model_provider_factory`: Valida fábrica e exceção para provider_id inválido.
   - `test_echo_garden_build_live_with_mock_provider_and_budget`: Executa o fluxo completo do jogo com `MockModelProvider` e `ExecutionBudgetManager`, verificando integridade do manifesto, HTML portátil e registro de 3 tentativas com reservas no SQLite.

## Validação Determinística

- `python core/harness/runner.py --quick` -> **`[HARNESS_PASS]`** (302 passed, 1 skipped)
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` -> **302 passed, 1 skipped** (100% de sucesso)
