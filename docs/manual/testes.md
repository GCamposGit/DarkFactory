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

### Identidade restrita no lock da suíte (USR-463)

O arquivo `suite-N.lock` existente só precisa ser aberto para leitura: o sistema
operacional mantém a trava, sem alterar os metadados do arquivo. Isso permite
que uma identidade restrita -- por exemplo `CodexSandboxUsers`, cuja ACL concede
`ReadAndExecute` sobre locks criados por outra conta -- adquira um slot canônico
livre. Um lock ausente é criado com abertura exclusiva; se outro processo vencer
a corrida de criação, o arquivo criado por ele é aberto para leitura. Falhas de
permissão no acesso ao diretório ou ao lock são tentadas um número limitado de
vezes e, ao persistirem, viram `SuiteLockUnwritable` com `path` e `reason`, sem
propagar um `PermissionError` cru. `_try_lock_file` só retorna `None` para slot
efetivamente ocupado por outro processo. `probe_free_slot()` nunca propaga o erro
estruturado: trata um slot inacessível como "nenhum slot livre observado" e
segue para o próximo, sem derrubar o chamador com traceback.

#### Modo degradado: estado por identidade (USR-463 T2)

`resolve_state_dir()` é o contrato que `SuiteLock.acquire()` usa para escolher
onde manter o lock. Uma falha de permissão no diretório canônico ou na leitura
do lock leva ao fallback por identidade; se o fallback também não puder criar
ou abrir seu lock, a função levanta `SuiteLockUnwritable` estruturado:

- Verifica o artefato exato que uma aquisição real usa
  (`<diretório-canônico>/suite-0.lock`) abrindo-o para leitura. Se estiver
  ausente, cria-o com modo exclusivo, tratando `FileExistsError` como uma
  corrida vencida por outro processo, e então o reabre para leitura. Nunca
  chama `touch()` nem abre um lock preexistente para escrita.
- Com acesso de leitura: retorna o diretório canônico, `degraded=False`,
  `reason=None`.
  Nenhum diretório novo é criado além do canônico; nenhum aviso é impresso.
- Sem acesso ou sem capacidade para criar um lock ausente: tenta primeiro o diretório por identidade --
  `DARKFAC_HARNESS_STATE_DIR/darkfac-harness-<usuário>` quando essa variável
  está configurada, senão `tempfile.gettempdir()/darkfac-harness-<usuário>`.
  Se a árvore configurada também negar a criação ou leitura do lock, tenta
  `tempfile.gettempdir()/darkfac-harness-<usuário>` como reserva independente.
  O diretório escolhido retorna com `degraded=True` e `reason` explicando a
  degradação.
- A sondagem e a aquisição repetem falhas de permissão até três vezes. Se todos
  os candidatos forem inacessíveis, a resolução falha com
  `SuiteLockUnwritable` estruturado.
- Se a permissão do lock canônico mudar depois da sondagem, a própria aquisição
  também troca para o fallback por identidade e espera um eventual dono
  canônico confirmado antes de adquirir o slot.

`SuiteLock.acquire()` em modo degradado:

- Expõe `degraded=True` e `state_dir_used` (o diretório realmente usado para o
  lock mantido) na instância, preenche `waited_sec` como de costume, e imprime
  exatamente uma linha `[SUITE_LOCK]` explicando a degradação e deixando claro
  que a exclusão entre identidades diferentes é best-effort (duas contas, cada
  uma no seu próprio fallback, não se veem).
- Antes de tomar o slot no diretório de fallback, espera enquanto o diretório
  **canônico** tiver um sidecar `suite-N.json` legível apontando para um
  processo confirmadamente vivo neste host (`process_identity.same_process`,
  mesmo hostname). Um sidecar ausente, ilegível, de outro host, ou cujo dono
  já morreu nunca bloqueia -- só um dono vivo confirmado bloqueia. A espera
  respeita o `timeout_sec` já configurado da instância; se o prazo estourar
  antes do dono canônico liberar, `acquire()` levanta `SuiteLockTimeout` com
  `waited_sec` preenchido, exatamente como o timeout normal de slot ocupado.
- Caso canônico acessível continua sem aviso e sem criar diretório de fallback:
  nenhum aviso novo impresso, nenhum diretório de fallback criado.

#### Portão oficial reporta o modo degradado sem quebrar (USR-463 T3)

`core/harness/runner.py` (o portão oficial, `--quick`) usa o modo degradado
acima de ponta a ponta, sem propagar `PermissionError`/`SuiteLockUnwritable`
cru nem perder a telemetria do run:

- `_append_run_timing()` grava `run_timings.jsonl` no diretório que
  `suite_lock.resolve_state_dir()` resolve -- canônico quando o lock está acessível, ou o
  fallback por identidade quando não -- em vez do acessor estrito
  `suite_lock.state_dir()`, que levantaria `SuiteLockUnwritable` exatamente
  neste caso. `_finish_run_timing()` continua envolvendo a gravação em
  `contextlib.suppress(Exception)`, então uma falha residual aqui (ex.: disco
  cheio) nunca muda o veredito, mas agora a gravação em si tem onde funcionar
  sob identidade restrita.
- `_timed_suite_lock()` grava no `RunTimings` de cada run se o lock usado
  foi `lock_degraded` (bool) e qual `lock_state_dir` foi de fato usado --
  lidos de `SuiteLock.degraded`/`state_dir_used` -- então o registro
  `[RUN_TIMING]` e o `run_timings.jsonl` distinguem um run degradado (gate
  passou, mas sob um diretório de fallback, exclusão best-effort) de um run
  normal sem precisar re-derivar isso da ACL do host.
- Além do tratamento já existente de `SuiteLockTimeout`, `_timed_suite_lock()`
  também captura `SuiteLockUnwritable` antes de deixá-la subir: imprime a
  mensagem estruturada `[SUITE_LOCK] ...` que a própria exceção já carrega e
  relança o mesmo tipo (subclasse de `RuntimeError`), que o `main()` do
  runner já converte em `[ERROR]`/`[HARNESS_FAIL]` com código de saída 2 --
  nunca um traceback cru. Isso cobre o caso residual em que até o diretório de
  fallback por identidade está inacessível (o caso comum, fallback gravável,
  já é tratado por `resolve_state_dir()` sem levantar nada).
- Resultado de ponta a ponta: um gate rodando sob identidade restrita
  (ex.: grupo sandboxed `CodexSandboxUsers`, que só tem `ReadAndExecute` nos
  arquivos de lock pré-existentes de outra conta) ainda executa os passos e
  emite `[HARNESS_PASS]` quando a suíte passa, com a evidência da degradação
  no próprio `run_timings.jsonl`.

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
