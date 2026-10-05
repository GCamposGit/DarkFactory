# Relatório de Conclusão: USR-126

**Ticket**: USR-126  
**Título**: Stabilize the Windows native runtime storage integration test  
**Status**: `completed`  
**Data**: 2026-10-05  
**PR**: [#156](https://github.com/GCamposGit/DarkFactory/pull/156) (commit `b218807`)  
**CI GitHub Actions**: Ubuntu-latest (PASS, 2m11s), Windows-latest (PASS, 9m1s)  
**Portão Oficial Notebook**: 3.066 descobertos, 3.058 aprovados, 8 pulados, 0 falhas (`[HARNESS_PASS]`)  

---

## 1. Contexto e Problema

O teste de integração do adaptador nativo Windows (`tests/test_runtime_spike_native.py`) apresentava falhas intermitentes sob carga paralela pesada no CI Windows (observado como erro `STORE_UNAVAILABLE` sem detalhes estruturados).

A anatomia da falha revelou duas causas raízes complementares:
1. **Corrida de Inicialização do EffectServer**: `EffectServer.start()` iniciava o servidor HTTP (`ThreadingHTTPServer.serve_forever()`) em thread de segundo plano e retornava imediatamente sem sincronização de prontidão. Sob alta carga de CPU no CI paralelo, requisições iniciais do `EffectClient` chegavam antes do socket estar pronto para aceitar conexões.
2. **Omissão de Diagnóstico Estruturado e Concorrência de Arquivo SQLite**:
   - `NativeAdapter._run_workflow()` capturava `(sqlite3.Error, OSError, StoreError)` e emitia `code="STORE_UNAVAILABLE"` sem nenhum detalhe de diagnóstico, mascarando se a falha se originava de contenção de trava no SQLite/NTFS, I/O de disco ou transporte do serviço de efeitos.
   - Não havia retentativas para erros transitórios de trava de arquivo NTFS (`sqlite3.OperationalError: database is locked` ou `PermissionError`).

---

## 2. Solução Implementada

1. **Prontidão Síncrona do EffectServer (`spikes/runtime_choice/effect_server.py`)**:
   - Adicionado `_wait_ready(timeout=3.0)` no `EffectServer.start()`, executando probe de socket local ativo antes de liberar a execução do cliente.

2. **Resiliência e Retentativa no EffectClient (`spikes/runtime_choice/native_adapter.py`)**:
   - Implementado loop de retry de conexão com backoff no `EffectClient._post` (até 3 tentativas, 50ms de intervalo) rotulando falhas com `source: "effect_service"`.

3. **Diagnóstico Estruturado e Higienizado sem Vazamento de Segredos (`spikes/runtime_choice/contracts.py`, `native_adapter.py`, `driver.py`)**:
   - Adicionado campo `details: dict[str, Any] = Field(default_factory=dict)` ao contrato `DriverEvent`, validado por Pydantic contra dados arbitrários e protegido estritamente contra segredos (`_reject_secret_material`).
   - Implementada a função `sanitize_diagnostic` que trunca strings, elimina caminhos de arquivos absolutos do Windows/POSIX e mascara chaves de autenticação (`token`, `key`, `password`, `secret`).
   - `NativeAdapterError` e eventos emitidos pelo `NativeAdapter` agora distinguem formalmente `source: "storage"` (SQLite/StoreError/OSError) de `source: "effect_service"`.

4. **Retentativa de Trava Transitória no Armazenamento SQLite (`core/orchestrator/store.py`)**:
   - Adicionada política de retry (até 5 tentativas com backoff aleatório de 20ms a 50ms) em `_write_transaction` e `_read_connection` para absorver contenções pontuais de arquivo no Windows NTFS sob alta concorrência.

---

## 3. Evidências de Teste e Validação

- **Testes de Regressão Adicionados (`tests/test_runtime_spike_native.py`)**:
  - `test_native_adapter_distinguishes_storage_error_from_effect_failure`: valida a distinção determinística de códigos e fontes (`storage` vs `effect_service`), sanitização de caminhos e ausência de vazamento de credenciais.
  - `test_native_adapter_observe_includes_sanitized_storage_diagnostic`: valida sanitização de caminhos no método `observe`.
  - `test_native_adapter_executes_repeatedly_without_transient_store_error`: valida estabilidade determinística em 10 execuções rápidas e concorrentes.
- **Suíte Nativa e Spikes**:
  - 17 testes de `test_runtime_spike_native.py` aprovados em 16.6s.
  - 4 cenários de `test_runtime_spike_scenarios.py` aprovados.
  - 8 testes unitários de `spikes/runtime_choice/tests/` aprovados.
- **Portão Determinístico Oficial no Notebook**:
  - Executado via `python core/harness/runner.py --quick --local`:
  - 3.066 testes descobertos, 3.058 aprovados, 8 pulados, 0 falhas (`[HARNESS_PASS]`).
- **CI GitHub Actions**:
  - PR #156: `pr-validation (ubuntu-latest)` verde (2m11s), `pr-validation (windows-latest)` verde (9m1s), `trusted-pr-policy` verde.
