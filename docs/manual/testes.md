<!-- covers: core/harness/remote_dispatch.py, core/harness/suite_lock.py, scripts/complexity_ratchet.py -->
# Testes

## Capacidade e prazo do portão

Ticket usa `python core/harness/runner.py --profile pr --local`: compileall,
typecheck, contratos de camada e complexidade, afetados e fumaça fixa. Não há
pré-teste focal adicional. A suíte inteira fica para Linux/CI de main; para
restaurar o caminho anterior do launcher, use `DARKFAC_GATE_SCOPE=full`.

Afetados usam merge-base de `origin/main`, nunca somente o último commit.
Seleção incerta (configuração/helpers globais, falha de grafo, ausência da base,
ativo sem teste ou código sem cobertura detectada) registra
`scope=affected_fallback` e executa `tests/test_harness_contract.py`, sem ampliar
para a suíte local inteira. A coleta conta casos parametrizados e define prazo
`min(1800, 120 + 20 * N)` segundos; seleção/coleta/retry compartilham o teto externo
de 1800s do passo. A coleta isolada é limitada a 120s.

PR aceita 1,5 GiB livres e usa `-n 0` até um worker, sem gateway execnet; acima de
30 arquivos e com dois workers disponíveis pode paralelizar, separando `serial`.
Espera de RAM padrão: 300s; abaixo do mínimo retorna 75 (`gate_insufficient_ram`).
Full conserva a conta de reserva de 2 GiB mais 2 GiB por worker e seu prazo
calibrado: um worker recebe duas parcelas, executando em processo único.

Crash xdist/INTERNALERROR/EOFError/exit 3 em execução paralela recebe um único
retry serial no mesmo orçamento, com `--lf` quando lastfailed existe e não está
vazio; sem cache repete os alvos. Falha real ou segundo crash continua reprovando.
Health/envio remoto têm timeout de socket limitado a 5s. Worker inacessível cede
para o próximo e depois para local no PR; `required` só é estrito no escopo full.
Candidato imutável, suite_lock e `[HARNESS_PASS]` continuam obrigatórios.

## Observação de capacidade antes da admissão (USR-468)

`core.harness.suite_lock.probe_free_slot()` verifica brevemente os slots de suíte
e solta todos os handles adquiridos. Se o processo já descende de um dono da
trava, a sonda informa ocupado. É somente uma observação: a execução real ainda
precisa adquirir `suite_lock`, pois outro processo pode ocupar o slot depois da
sonda. Worker ocupado gera espera de admissão, sem iniciar PLAN/BUILD para produzir
mais candidatos que não podem ser validados.

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

## Despacho remoto de testes e hosts desabilitados (USR-430)

`core/harness/remote_dispatch.py` gerencia o envio do portão de testes para workers remotos (como a VM Grokbox via Tailscale). A identidade lógica vem de `core/infra/host_identity.py`, que associa nomes de host, IPs Tailscale, ids legados e ids canônicos do inventário (por exemplo, `darkfac-desktop` e `onprem-z97-server` ao host `desktop`). `KNOWN_HOST_IDENTIFIERS` continua exportado pelo despachante e usa essa tabela comum.

Quando um host está temporariamente fora da frota ou com outra carga (configurado em `DARKFAC_FLEET_DISABLED_HOSTS`, ex.: `desktop`), o despachante resolve primeiro a identidade lógica de cada URL, `node_id` e `hostname`. Assim, `onprem-z97-server` e `darkfac-desktop` são descartados para `desktop`; `predator-neo-16` e `AI_Notebook` continuam elegíveis. Só identificadores desconhecidos usam a compatibilidade legada por substring. O filtro atua antes das requisições e sondas `/health`; se um worker com endereço arbitrário se identificar como host desabilitado, o runner descarta o candidato e tenta o próximo worker ou a execução local. A rotação de workers em falha (`_rotate_gate_workers` no `run_ticket.py`) também respeita essa lista e nunca reintroduz hosts desabilitados na suíte de tentativas.

O worker resolve `node_id` pelo hostname local quando não recebe configuração explícita; `/health` informa o `node_id`, `hostname` e `node_id_source`. `scripts/install_test_worker.ps1` mantém esse padrão por default e aceita `-NodeId` como override explícito. O worker corrige um id conhecido que pertença a outro host quando o hostname local identifica a máquina, para que o `/health` represente o host que respondeu.
