# Relatório de Conclusão — CR-14: Preservar Erros no Protocolo do Driver HF-02

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-14 (Trilha HF-02 Transporte, depende diretamente de CR-01)  
**Achado endereçado:** F12 (falta de run no `observe` e SQLite inacessível causando exceções brutas não sanitizadas; necessidade de preservar códigos estruturados nas bordas do driver sem esconder defeitos internos)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`, `spikes/runtime_choice/native_adapter.py`, `spikes/runtime_choice/driver.py`, `tests/test_runtime_spike_native.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Preservação de `RUN_NOT_FOUND` no Protocolo JSONL**:
   - Quando um comando `observe` é enviado para um `workflow_id` inexistente no banco, `NativeAdapter.observe` retorna estruturadamente um evento com `kind="error"`, `runtime_status="error"` e `code="RUN_NOT_FOUND"`.
   - No fluxo de subprocesso real via JSONL, a resposta é entregue no `stdout` do processo e, após o envio do comando de `shutdown`, o processo encerra com exit code 0 e sem qualquer saída no `stderr`.

2. **Sanitização de Falhas de Acesso e Inicialização do Store SQLite (`STORE_UNAVAILABLE`)**:
   - Em `NativeAdapter.__init__`, a criação do diretório nativo, a resolução do caminho do banco e a instanciação de `OrchestratorStore` e `OrchestratorRuntime` foram encapsuladas em bloco defensivo que captura `(OSError, sqlite3.Error, StoreError)`.
   - Na construção direta em Python de `NativeAdapter(config)` em cenários onde o path do store é inválido (ex.: `native` ocupado por um arquivo regular ou `orchestrator.sqlite3` sendo um diretório), é levantada a exceção tipada `NativeAdapterError("STORE_UNAVAILABLE")` em vez de vazar `sqlite3.OperationalError: unable to open database file` ou `FileExistsError`.
   - Em `driver.py:build_adapter`, `NativeAdapterError` é capturado e mapeado para `DriverConfigurationError("STORE_UNAVAILABLE")`.
   - No CLI real do driver (`python -m spikes.runtime_choice.driver --config <valid_config>`), falhas de inicialização do store resultam em:
     - Encerramento imediato com exit code **2**.
     - `stderr` contendo exclusivamente `STORE_UNAVAILABLE`.
     - `stdout` estritamente vazio.
     - Ausência completa de tracebacks ou vazamento de caminhos/segredos.
   - **Garantia de ausência de fallback**: O driver não cria bancos SQLite alternativos (ex.: em `:memory:` ou caminhos temporários fora do contrato), não altera o runtime selecionado e não emite sinais espúrios de prontidão.
   - Leitura independente dos diretórios de teste confirma que a estrutura do filesystem permanece inalterada sem arquivos órfãos.

3. **Robustez a Falhas de I/O em Tempo de Execução**:
   - Em `NativeAdapter.observe`, `NativeAdapter.start` e `NativeAdapter._run_workflow`, falhas de I/O ou corrupção do SQLite são tratadas emitindo eventos estruturados com `code="STORE_UNAVAILABLE"`.
   - Em `driver.py:run_jsonl`, exceções do tipo `NativeAdapterError` ocorridas durante o processamento de comandos são convertidas em eventos de erro com o respectivo `error.code` e `exit_code = 2`.
   - Exceções inesperadas não são silenciadas (proibição expressa de `except Exception: pass`).

4. **Preservação de Validações de Configuração e Contrato**:
   - Configurações com schema inválido continuam emitindo `CONFIG_INVALID` e exit code 2.
   - Violações de contrato nos comandos JSONL continuam emitindo `CONTRACT_INVALID` e exit code 3.

---

## 2. Arquivos Modificados

| Arquivo | Mudança |
|---|---|
| `spikes/runtime_choice/native_adapter.py` | Importado `StoreError` e `OrchestratorStore` no escopo do módulo; encapsulada a inicialização do store e runtime em `NativeAdapter.__init__` para levantar `NativeAdapterError("STORE_UNAVAILABLE")` em falhas de I/O/SQLite; ampliado o tratamento de erros em `observe`, `start` e `_run_workflow` para capturar `(sqlite3.Error, OSError, StoreError)`. |
| `spikes/runtime_choice/driver.py` | Em `build_adapter`, importado e capturado `NativeAdapterError` mapeando para `DriverConfigurationError("STORE_UNAVAILABLE")`; em `_protocol_error`, adicionado parâmetro opcional `workflow_id`; em `run_jsonl`, adicionado tratamento para `NativeAdapterError` emitindo erro de protocolo sanitizado com `exit_code = 2`. |
| `tests/test_runtime_spike_native.py` | Adicionados 3 novos testes rigorosos: `test_native_adapter_direct_construction_fails_cleanly_on_inaccessible_store`, `test_cli_reports_inaccessible_sqlite_directory_without_fallback_or_traceback` e `test_jsonl_driver_emits_store_unavailable_when_observe_fails`. Importado `NativeAdapterError`. |

---

## 3. Validação Executada

### 3.1 Testes Unitários e de Subprocessos Focais do Driver
```powershell
python -m pytest tests/test_runtime_spike_native.py -v
```
Resultado: **14 passed in 7.10s**

### 3.2 Suíte Completa do Runtime Spike (Contratos, Driver e Efeitos)
```powershell
python -m pytest tests/test_runtime_spike_contracts.py tests/test_runtime_spike_native.py tests/test_runtime_spike_effects.py -v
```
Resultado: **25 passed in 11.23s**

### 3.3 Harness Determinístico da Fábrica
```powershell
python core/harness/runner.py --quick
```
Resultado:
- `[STEP_PASS] syntax_and_types`
- `[STEP_PASS] unit_and_integration_tests`
- **617 passed, 2 skipped in 89.46s**
- `[HARNESS_PASS]`

### 3.4 Suíte Global de Testes do Repositório
```powershell
python -m pytest tests -v
```
Resultado: **617 passed, 2 skipped in 91.41s**

---

## 4. Conclusão

O ticket **CR-14** foi concluído com 100% de aprovação técnica em estrita aderência ao plano aprovado e aos critérios de aceite do `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`. A Trilha HF-02 (Transporte e Driver) está plenamente estabilizada para a subsequente qualificação de oráculos (CR-16).
