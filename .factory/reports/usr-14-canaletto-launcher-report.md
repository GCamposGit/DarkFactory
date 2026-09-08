# Relatório de Conclusão: Demanda USR-14 — Inicializador Headless do Canaletto

## Resumo Executivo

A demanda **USR-14** ("Ajustar link para Canalleto na home para iniciar .py antes de redirecionar para localhost") foi implementada e validada com 100% de sucesso.
O problema anterior ocorria quando o usuário clicava no card do **Canaletto Gallery & AI Studio** no DarkHub (`http://127.0.0.1:8899`); se o script `run_canaletto.py` não estivesse ativo no terminal, a conexão falhava (`ERR_CONNECTION_REFUSED`).

A solução implementada transforma o DarkHub em um orquestrador inteligente para serviços locais, capaz de verificar a saúde do serviço, disparar o processo Python em background de forma headless e redirecionar o usuário diretamente para a galeria sem atritos ou falhas de navegação.

---

## Entregas Detalhadas

### 1. Modelos de Dados & Catálogo (`hub/backend/models.py`, `hub/data/services.json`)
- **`ServiceItem` / `ServiceCreate` / `ServiceUpdate`**:
  - Campo opcional adicionado: `launch_script: Optional[str] = Field(default=None, description="Optional relative or absolute python script to launch service if offline")`.
- **`ServiceLaunchResponse`**:
  - Modelo Pydantic v2 estrito com `service_id`, `url`, `status` (`'online'`, `'already_running'`, `'starting'`, `'failed'`), `launched: bool` e `message: str`.
- **Catálogo de Serviços (`hub/data/services.json` e `hub/data/default_services.json`)**:
  - Atualizado o item `canaletto-gallery` com `"launch_script": "run_canaletto.py"`.

### 2. Lógica Headless no Backend (`hub/backend/service.py`)
- **`HubService.launch_service(service_id, max_wait_sec=5.0, poll_interval=0.2)`**:
  - **Probe Prévia**: Executa `ping_url` no serviço alvo; se já estiver `ONLINE`, retorna imediatamente `already_running` (`launched=False`), evitando spawns desnecessários e duplicidade de processos.
  - **Spawn Resiliente no Windows**: Se offline, invoca `subprocess.Popen([sys.executable, str(script_path), "--no-browser"])` em modo desanexado (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`), com `CANALETTO_NO_BROWSER=1` e `PYTHONUNBUFFERED=1`.
  - **Isolamento de Logs**: Redireciona `stdout` e `stderr` para `.factory/services/{service_id}.log`.
  - **Polling Determinístico**: Aguarda até `max_wait_sec` com intervalos curtos até que a porta local responda com status `ONLINE`.
- **`HubService.get_service_launch_target(service_id, max_wait_sec=5.0)`**:
  - Orquestra o ciclo completo e retorna a URL final de destino para redirecionamento.

### 3. Endpoints REST & Redirecionamento (`hub/backend/api.py`)
- **`POST /api/services/{service_id}/launch`**:
  - Disparo e telemetria programática para agentes e chamadas assíncronas.
- **`GET /api/services/{service_id}/open`**:
  - Redirecionador HTTP 307 Temporary Redirect para o navegador, permitindo navegação sem restrições de pop-up blocker.

### 4. Suporte a `--no-browser` em `run_canaletto.py`
- Atualizada a função `open_browser_delayed` para inspecionar `CANALETTO_NO_BROWSER=1` e `--no-browser`.
- Quando disparado pelo DarkHub, não abre abas redundantes em background.
- Quando executado manualmente no terminal (`python C:\dev\DarkFac\run_canaletto.py`), mantém a abertura normal do navegador.

### 5. Frontend (`hub/frontend/app.js`)
- Em `renderQuickDock()` e `renderServices()`, links de serviços com `launch_script` apontam dinamicamente para `${API_BASE}/services/${safeId}/open`.
- Rótulo de botão claro: `"Iniciar & Abrir"`.
- Command Palette (busca rápida) configurada para despachar via rota de inicialização.
- Listener global para emissão de toast discreto ("Iniciando serviço local em segundo plano...").

---

## Contrato de Reachability e Validação

Suíte dedicada: [`tests/test_ajustar_link_para_canalle.py`](file:///C:/dev/DarkFac/tests/test_ajustar_link_para_canalle.py) (16 testes unitários e de integração cobrindo modelos, catálogo, lógica de subprocesso, idempotência, flags CLI, endpoints HTTP e frontend):

```powershell
python -m pytest C:\dev\DarkFac\tests\test_ajustar_link_para_canalle.py -v -c C:\dev\DarkFac\pytest.ini
# ============================= 16 passed in 0.44s ==============================
```

Harness Geral:
```powershell
python C:\dev\DarkFac\core\harness\runner.py --quick
# [STEP_PASS] syntax_and_types
# [STEP_PASS] unit_and_integration_tests
# ======================= 328 passed, 1 skipped in 19.69s =======================
# [HARNESS_PASS]
```
