# Testes

## Testes de forma

Teste de forma e o que so confere a existencia de um arquivo, um trecho de texto dentro de markdown, skill, docs ou HTML, ou a presenca de chaves em um JSON/YAML de configuracao, sem executar comportamento. Ele passa mesmo que o produto esteja quebrado, entao nao prova nada sobre o produto e nao deve segurar o portao.

Regras:

- Esses testes vivem em `tests/shape/` e ficam fora do portao: `pytest.ini` tem `--ignore=tests/shape` em `addopts`, e o harness (`python C:\dev\DarkFac\core\harness\runner.py --quick`) herda isso.
- Para roda-los quando quiser (por exemplo ao mexer em manifests de deploy ou skills):
  `python -m pytest tests/shape -q`
- Teste que exercita comportamento (chama funcao, endpoint, CLI, mutacao detectada) fica em `tests/`. Teste novo que so le texto/arquivo/chave vai direto para `tests/shape/`.
- Arquivos em `tests/shape/` usam `Path(__file__).resolve().parents[2]` para a raiz do repositorio.

## Como o tempo e simulado (USR-377 S4)

Testes nao devem esperar relogio real. Regras:

- `core/harness/remote_dispatch.py` expoe `_sleep` (padrao `time.sleep`), usado no intervalo entre sondas de
  `/health` (`DEAD_PROBE_GAP_SEC`, 15 s). Testes fazem
  `monkeypatch.setattr(remote_dispatch, "_sleep", lambda _s: None)`.
- `core/harness/suite_lock.py` expoe `_sleep` (backoff de espera pelo lock). A fixture
  `simulated_lock_time` em `tests/test_suite_lock_stalled.py` troca `suite_lock.time` por um relogio
  `monotonic` real + deslocamento virtual e faz `_sleep` apenas avancar o deslocamento: prazos, espera de
  stall e janela de CPU passam instantaneamente, enquanto o processo detentor continua real.
- Esperas por eventos assincronos (heartbeat do `LineLock`) usam prazo generoso com saida antecipada no
  primeiro sinal (`observe_sec`), nunca um intervalo fixo. Caso negativo usa prazo curto explicito.
- Subprocessos reais com prazo (`_run_bounded`) usam o menor prazo viavel (1 s).
- Os padroes de producao nao mudam: `_sleep` so e trocado dentro de testes.

## Contratos de camada e ratchet de complexidade (USR-377)

- `.importlinter` define dois contratos: `core` nao importa `hub`, e `core` nao importa `fastapi`/`starlette`. Rode `lint-imports` (dependencia `import-linter` em `requirements.txt`).
- Excecoes reais ficam explicitas em `ignore_imports` (hoje `core.harness.remote_worker` e `core.orchestrator.cloud_coordinator` importam `fastapi`; TODO: mover a camada HTTP para fora de `core`). Nao adicione excecoes novas sem ticket.
- `python C:\dev\DarkFac\scripts\complexity_ratchet.py` mede o tamanho de cada funcao em `core/` e `hub/backend/` e compara com `.factory/config/complexity_baseline.json`: falha para funcao nova acima de 150 linhas ou funcao ja grande que cresce.
- Apos reduzir uma funcao grande, rode com `--update` para o baseline encolher (nunca cresce).
- Passos de CI pendentes (`.github/workflows/ci.yml` e protegido pelo guard; owner publica). Inserir no job `pr-tests` depois de "Install dependencies" (e, se desejado, no `ledger-fast`):

```yaml
      - name: Layer contracts (import-linter)
        working-directory: candidate
        run: lint-imports
      - name: Complexity ratchet
        working-directory: candidate
        run: python scripts/complexity_ratchet.py
```
