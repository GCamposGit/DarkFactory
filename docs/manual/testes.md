<!-- covers: core/harness/remote_dispatch.py, core/harness/suite_lock.py, scripts/complexity_ratchet.py -->
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

## Isolamento de estado da suite (USR-425 T1)

`tests/conftest.py` limpa, antes da coleta, marcadores herdados de execucao (`DARKFAC_LINE_LOCK_HELD`,
`DARKFAC_LOCAL_CANCEL_PARENT`, `DARKFAC_LOCAL_RUN_ID` e `DARKFAC_RUN_ID`) e sempre troca
`DARKFAC_STATE_ROOT` por uma raiz temporaria. Cada teste recebe outra raiz privada para estado de ticket,
evitando corridas entre testes paralelos que reutilizam o mesmo `ticket_id` e produzem `SPEC.md`,
`tickets.json` ou `progress.json`. Um teste que precise validar heranca desses marcadores deve configura-los
explicitamente no subprocesso ou com `monkeypatch` no proprio teste.

## Cache do pytest em worktrees Windows (USR-421)

`tests/_cache_dir.py` mantém o cache por worktree sob `DarkFac/pytest-cache` no diretório
temporário do usuário, fora do checkout. Isso preserva `--lf` entre execuções e permite que
ambientes isolados escrevam o cache sem depender de `%LOCALAPPDATA%`. O cacheprovider inicializa
esse diretório durante `pytest_sessionfinish`; um destino inacessível pode deixar o processo parado
depois de `100%`, antes do resumo e do código de saída. `redirect_cache_dir` grava o destino antes
de o cache plugin ler `cache_dir` e reaplica o mesmo caminho no `Cache` já criado, no início de
`pytest_sessionfinish`, antes de `Cache.set`.

Contrato de encerramento do foco: `python -m pytest tests/test_fleet_dispatcher.py -q` termina sozinho, com código de saída e uma linha de resumo do pytest no formato `N passed in Xs`, dentro do prazo configurado em `pytest.ini` (`timeout = 120`, `timeout_method = thread`). O cache por worktree fica em `DarkFac/pytest-cache` no diretório temporário do usuário. `redirect_cache_dir` grava esse destino antes de o cache plugin ler `cache_dir` e reaplica o caminho no `Cache` já criado, no início de `pytest_sessionfinish`, antes de `Cache.set`. Um `%LOCALAPPDATA%` não gravável não deixa o processo aberto depois de `100%`.

O laço de autocorrecao classifica `gate_dirty_worktree` somente quando encontra a linha completa `[ERROR]`
emitida por `core/harness/runner.py`; menções ao texto em tracebacks ou logs de testes não bastam.

Teste que limpa todo prefixo `DARKFAC_*` do ambiente (ex.: `_isolated_env` em
`tests/line/test_context_retention.py`) deve reinstalar `DARKFAC_STATE_ROOT` dentro do seu proprio
`tmp_path` logo depois da limpeza. Sem isso, `core.paths.state_root()` cai de volta em
`project_root() / ".factory"`, a raiz real do checkout/worktree — e o portao oficial roda a suite sob
uma trava de linha propria e viva la (USR-413/425). `line_lock.is_locked()` entao ve essa trava do
proprio portao e qualquer teste que dependa de `CloudWorker.poll_and_execute_once` (ex.: o sweep de
retencao de contextos) é descartado antes de rodar, sem nenhuma excecao — falha muda (USR-427).

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

O fingerprint de higiene inclui os arquivos ignorados de `.factory/`, mas normaliza `heartbeat_at` apenas nas travas `line_lock` conhecidas. Assim, o heartbeat legítimo do launcher pode avançar durante o portão sem reprovar a suíte; mudanças nos demais campos ou remoção da trava continuam visíveis.
