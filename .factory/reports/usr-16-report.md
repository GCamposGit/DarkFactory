# Relatório de Execução: USR-16 - Worker Dedicado de Testes e Validação Headless On-Premises (INFRA-10)

- **Ticket**: `USR-16`
- **Título**: Worker Dedicado de Testes e Validação Headless On-Premises (INFRA-10)
- **Origem**: `user-demand`
- **Owner**: `antigravity-orchestrator`
- **Data / Horário**: 2026-09-08T13:45:00Z
- **Status do Ticket**: `completed`
- **Workspace**: `C:\dev\DarkFac`

---

## 1. Escopo e Objetivos Atingidos

1. **Daemon Headless de Testes Remotos (`core/harness/remote_worker.py`)**:
   - Desenvolvido em FastAPI com inicialização leve e sem dependências pesadas de frontend.
   - Endpoint `GET /health` (`WorkerHealthStatus`): diagnóstico rápido em ~0.1s para detecção de liveness do nó, detecção do Docker e diretório do projeto.
   - Endpoint `POST /execute` (`TestExecutionInstruction` -> `DistilledTestReport`): recebe instruções de teste serializadas, executa pytest de forma isolada e headless no nó, gerando logs completos em `.factory/test_logs/` e retornando o `DistilledTestReport` com `worker_id` e `execution_mode="remote_onprem_offload"`.
   - Compatível com o servidor `desktop-g45ipem` (Tailscale `100.78.181.90:8080`, CPU i7-4790K, 16 GB RAM, 3 TB no Drive `E:`).

2. **Extensão do Engine de Test Subagents (`core/harness/test_subagent.py`)**:
   - `TestExecutionInstruction` ampliado com:
     - `worker_mode: str = "auto"` (`auto`, `remote`, `local`).
     - `remote_worker_url: Optional[str] = "http://100.78.181.90:8080"`.
     - `allow_fallback: bool = True`.
     - `worker_probe_timeout: float = 1.0`.
   - `DistilledTestReport` ampliado com rastreabilidade explícita:
     - `worker_id: str` (ex: `onprem-z97-server` ou `local`).
     - `execution_mode: str` (`local`, `remote_onprem_offload` ou `local_fallback`).
   - Métodos desacoplados:
     - `probe_remote_worker(worker_url, timeout)`: probe HTTP com timeout restrito para evitar atrasos na inicialização.
     - `execute_remote(instruction)`: serialização e offloading HTTP POST.
     - `execute_local(instruction, execution_mode)`: execução headless local isolada.
     - `execute(instruction)`: orquestrador inteligente com **failover transparente local-first** (se o servidor estiver desligado ou sem rede, reverte silenciosa e rapidamente para o notebook local, mantendo a suíte 100% verde).

3. **Script Guarded de Inicialização (`scripts/start_onprem_worker.ps1`)**:
   - Inicializador PowerShell com `-NoProfile -NonInteractive -ExecutionPolicy Bypass`.
   - Integração com `scripts/init_terminal.ps1` para garantia de ancoragem de CWD e encoding UTF-8.
   - Parâmetros configuráveis: `-HostAddress`, `-Port`, `-NodeId`, `-RootPath`.

4. **Integração no DarkHub (`hub/backend/api.py` e `hub/backend/service.py`)**:
   - Endpoint `GET /api/harness/workers`: telemetria de latência, saúde e capacidade de todos os nós de teste cadastrados (`onprem-z97-server` e `local-notebook`).
   - Endpoint `POST /api/harness/execute`: execução remota ou local de testes via API HTTP desacoplada.

5. **Roadmap & Demandas**:
   - Inclusão formal de `USR-16`, `USR-17` e `USR-18` em `.factory/demands/demands.json`.
   - Sincronização do status de `INFRA-10` para `DELIVERED` em `.factory/infra/roadmap.md`.

---

## 2. Evidência Determinística de Validação

### Suíte Focal de Testes Unitários e de Integração
Comando executado:
```powershell
python -m pytest tests/test_worker_onprem_test_subagent.py -v
```
**Resultado**: 12/12 testes aprovados (100% pass) em 0.45s:
- `test_instruction_and_report_models_defaults_and_serialization`: PASSED
- `test_remote_worker_app_health_endpoint`: PASSED
- `test_remote_worker_app_execute_endpoint`: PASSED
- `test_engine_probe_remote_worker_success`: PASSED
- `test_engine_probe_remote_worker_offline`: PASSED
- `test_engine_execute_local_mode`: PASSED
- `test_engine_execute_remote_success`: PASSED
- `test_engine_execute_auto_failover_to_local_when_remote_offline`: PASSED
- `test_engine_execute_remote_fail_closed_when_no_fallback`: PASSED
- `test_engine_execute_remote_network_error_triggers_fallback`: PASSED
- `test_hub_api_list_test_workers`: PASSED
- `test_hub_api_execute_test_suite`: PASSED

### Validação Obrigatória do Harness
Comando executado:
```powershell
python core/harness/runner.py --quick
```
**Resultado**:
```text
[STEP_PASS] syntax_and_types
[STEP_PASS] unit_and_integration_tests
[TEST_COUNT] count=398
397 passed, 1 skipped in 26.04s
[HARNESS_PASS]
```
Zero regressões introduzidas na fábrica.

---

## 3. Gestão Remota Headless & Autonomia do Nó

O worker foi aprimorado para operar de forma 100% autônoma e invisível:
1. **Zero Janelas de Terminal**:
   - `core/harness/test_subagent.py` agora enforça `creationflags = subprocess.CREATE_NO_WINDOW` em todos os subprocessos pytest no Windows.
   - `scripts/start_onprem_worker.ps1` suporta a flag `-Headless` executando em segundo plano (`WindowStyle: Hidden`).
2. **Auto-Update e Gestão Remota via REST API**:
   - `POST /system/exec`: executa comandos e scripts remotamente no nó via HTTP com resposta JSON.
   - `POST /system/update`: executa `git pull` automático no repositório do nó sem intervenção humana.
   - `POST /system/restart`: reinicia o daemon em modo headless em segundo plano.
3. **Inicialização Silenciosa Automática no Boot (Zero-Admin)**:
   - `scripts/install_onprem_worker_user_startup.ps1`: instala um launcher VBScript silencioso na pasta `Startup` do usuário (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DarkFacWorker.vbs`).
   - O worker inicializa automaticamente e invisível sempre que o Windows iniciar ou o usuário logar, sem solicitar privilégios de administrador.

---

## 4. Instruções de Operação para o Desenvolvedor

Para rodar qualquer suíte com offloading para o worker remoto (com failover local automático):
```powershell
python C:\dev\DarkFac\core\harness\test_subagent.py --target tests/test_roadmap_scale.py --worker-mode auto
```

Para forçar execução estritamente remota:
```powershell
python C:\dev\DarkFac\core\harness\test_subagent.py --target tests/test_roadmap_scale.py --worker-mode remote --no-fallback
```

Para atualizar o worker no desktop remotamente direto do notebook:
```python
import urllib.request, json
req = urllib.request.Request("http://100.78.181.90:8080/system/update", data=json.dumps({}).encode("utf-8"), headers={"Content-Type": "application/json"})
res = urllib.request.urlopen(req)
print(res.read().decode("utf-8"))
```

