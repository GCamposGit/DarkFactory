<!-- covers: core/harness/runner.py, core/harness/affected.py -->
# Portao em camadas (USR-377 S5)

Tickets e frota executam localmente `runner.py --profile pr --local`, sem
pré-teste focal duplicado. O perfil roda compileall, typecheck, `lint-imports`,
`python scripts/complexity_ratchet.py`, testes afetados e fumaça fixa em
`tests/test_harness_contract.py`. `docs_sync` permanece no CI com base/head.
O perfil PR força `--no-cache --local`; o veredito continua exigindo candidato
limpo e imutável, testes descobertos/executados e `[HARNESS_PASS]`.

O subprocesso de testes afetados usa um prazo compartilhado para seleção, coleta e execução
quando chamado pelo portão. A margem final permite devolver a saída parcial e indicar a fase
do timeout antes de o runner encerrar o passo.

A seleção compara o candidato inteiro ao merge-base com `origin/main`, incluindo
alterações staged, unstaged e untracked na CLI. Não compara só o último commit.
Usa imports transitivos, referências de texto e guards de repositório. Quando
há mudança global (conftest/helpers de teste, pytest.ini, harness.config.json,
requirements, governança, harness ou módulo muito importado), falha do grafo,
base solicitada ausente, ativo sem associação ou código sem cobertura identificada,
roda somente a fumaça fixa e registra `scope=affected_fallback`, junto da causa.
Essa evidência cobre a seleção/fumaça; a suíte completa fica para Linux/CI de main.
Mudança só de docs executa a fumaça do perfil.

O orçamento dos afetados usa a coleta real (inclusive parametrizações):
`min(1800, 120 + 20 * testes_coletados)` segundos. A coleta recebe
`min(prazo_restante, 600, 120 + 10 * arquivos_selecionados)` segundos; o passo
externo `affected_tests` tem piso de 1800s para que uma configuração antiga
de 120s não trunque esse orçamento. O mesmo prazo interno cobre seleção, coleta,
execução e eventual retry.
Durante `--run` o pytest sai ao vivo (uma linha por arquivo de teste, com flush
imediato) e, sem saída nova, uma linha `[TEST_PROGRESS] running elapsed_sec=...`
a cada 30s; não há mais janela silenciosa até o fim. Fora de
`DARKFAC_GATE_SCOPE=full`, coleta e execução somam no máximo
`DARKFAC_AFFECTED_MAX_SECONDS` (padrão 900s = 15 min; `0` desliga). Ao estourar, o
processo mata o pytest, imprime `[AFFECTED_TIME_CAP] {...}` (JSON com `error`,
`phase`, `cap_sec`, `elapsed_sec` e a ação: delegar o portão ao host Linux ou ao CI,
ou elevar a variável), seguido de `[STEP_TIMEOUT]`, e sai com código 124.
O launcher mantém seu envelope externo para os demais contratos e fila do lock.
Se a ACL impedir a leitura/criação do lock canônico ou a gravação do sidecar,
`SuiteLock` libera qualquer slot canônico adquirido e tenta um diretório por
identidade junto ao estado configurado, seguido de um fallback em `%TEMP%`.
O registro `[RUN_TIMING]` inclui `lock_degraded` e `lock_state_dir`; no modo
degradado, a coordenação entre identidades distintas permanece best-effort.

Com pelo menos `MIN_SERIAL_AVAILABLE_GIB = 1.5` GiB livres o escopo PR degrada
para execução serial. Até um worker usa `-n 0`, sem subprocesso xdist/gateway.
A seleção de até 30 arquivos roda serial; seleções maiores usam a capacidade
medida quando cabem dois ou mais workers, separando os marcados `serial`.
A espera de RAM é de 300s (`DARKFAC_GATE_RAM_WAIT_S` pode alterar); abaixo de
1,5 GiB após a espera retorna 75, `gate_insufficient_ram`, causa de infraestrutura.

Uma queda de worker em passo paralelo (INTERNALERROR, EOFError, worker crashed
ou exit 3) repete uma única vez em série, dentro do mesmo prazo: `-n 0`, com
`--lf` se existe cache lastfailed não vazio; sem cache, repete os mesmos alvos.
Uma falha real sem crash não recebe esse retry; falha serial continua reprovando.

Para uso manual, `DARKFAC_GATE_SCOPE=full` restaura no launcher `--quick` e seu
despacho remoto anterior. Nesse escopo a primeira capacidade continua exigindo
4 GiB livres (reserva de 2 GiB + 2 GiB/worker); um worker roda com `-n 0` e recebe
o dobro do prazo paralelo calibrado para dois workers. Zero capacidade retorna 75.
Cache por árvore, suite_lock e vínculo do veredito ao candidato permanecem.

Health e envio remoto têm timeout de socket de no máximo 5s. Worker inacessível
cede ao próximo; esgotados os workers, PR segue local mesmo com remoto required.
`required` só é estrito com `DARKFAC_GATE_SCOPE=full`. Na frota com
`DARKFAC_FLEET_GATE_REMOTE=1`, o launcher passa `--profile pr --remote-required`,
despachando o perfil `pr` para o worker remoto (sem checar RAM local antes) e só
validando o piso local de 1,5 GiB se cair para execução local. Fora da frota,
`runner.py --profile pr` continua local (`--local`).

Quando `affected` recebe `--base HEAD` em uma worktree limpa, ele retorna somente
os guards globais registrados sem montar o grafo de imports. Com qualquer mudanca
staged, unstaged ou untracked, a selecao usa o diff e a analise normal.

Se `runner.py` recusar um candidato sujo, a mensagem mantém a linha canônica
`Candidate worktree is dirty; commit or use a clean checkout before running the official harness`
e acrescenta em uma linha própria `[DIRTY_PATHS]` com até 20 caminhos. Quando houver mais,
o fim da linha informa `+N` caminhos omitidos. A linha canônica permanece estável para o
classificador `gate_dirty_worktree` de `run_ticket.py`; use a lista seguinte para localizar
as alterações tracked ou untracked antes de limpar ou preparar um checkout limpo.

Os perfis vivem em `harness.config.json` (campo `profiles` de cada step; padrao `["full"]`).
Comando: `python C:\dev\DarkFac\core\harness\runner.py --profile pr`

## Mudanca pendente no workflow

`.github/workflows/*` e protegido; aplicar manualmente (`git apply`) o diff abaixo.

Causa da duplicidade: cada PR dispara `pull_request` e `pull_request_target` no mesmo
`ci.yml`. No run do `pull_request_target` os jobs `pr-tests`/`pr-validation` aparecem como
`skipped`, e o GitHub trata check `skipped` como aprovado, entao o check exigido nao garante
que os testes rodaram.

Correcao:
- `trusted-pr-policy` vai para o arquivo proprio `.github/workflows/trusted-policy.yml`,
  disparado so por `pull_request_target` (codigo do workflow vem da base; um PR nao consegue
  editar o workflow que roda o guard). O nome do job/check continua `trusted-pr-policy`.
- `ci.yml` dispara so em `pull_request`, `push` e `schedule`; assim os nomes
  `pr-tests`/`pr-validation` existem apenas no run de `pull_request` e nunca aparecem como
  `skipped` vindos de um run de target.
- Os agregadores `pr-validation (*)` falham a menos que `pr-tests` (pesado) ou `ledger-fast`
  (leve) tenham terminado com `success`.
- `pr-tests` fica so em ubuntu com `--profile pr`; Windows 4 shards roda em push na main e em
  cron noturno.

Nomes dos checks exigidos nao mudam: `pr-validation (ubuntu-latest)`,
`pr-validation (windows-latest)`, `trusted-pr-policy`.
Nota: o CI materializa o harness da base; o perfil `pr` so vale depois que este commit estiver na main.

```diff
diff --git a/.github/workflows/trusted-policy.yml b/.github/workflows/trusted-policy.yml
new file mode 100644
--- /dev/null
+++ b/.github/workflows/trusted-policy.yml
@@ -0,0 +1,28 @@
+name: DarkFac Trusted Policy
+
+# Runs only on pull_request_target so the workflow code itself is base-owned
+# (a PR cannot edit the workflow that enforces the guard). Never executes candidate code.
+on:
+  pull_request_target:
+    types: [opened, synchronize, reopened, ready_for_review]
+
+permissions:
+  contents: read
+
+concurrency:
+  group: trusted-policy-${{ github.event.pull_request.number }}
+  cancel-in-progress: true
+
+jobs:
+  trusted-pr-policy:
+    runs-on: ubuntu-latest
+    steps:
+      - name: Checkout trusted base
+        uses: actions/checkout@v4
+        with:
+          ref: ${{ github.event.pull_request.base.sha }}
+          path: trusted
+          fetch-depth: 1
+          persist-credentials: false
+      - name: Checkout candidate merge without executing it
+        uses: actions/checkout@v4
+        with:
+          ref: refs/pull/${{ github.event.pull_request.number }}/merge
+          path: candidate
+          fetch-depth: 0
+          persist-credentials: false
+      - name: Enforce base-owned governance policy
+        working-directory: candidate
+        run: python ../trusted/core/orchestrator/guard.py ${{ github.event.pull_request.base.sha }}
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 116abd3c..ff0eb23a 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -4,8 +4,8 @@ on:
   push:
     branches: [main, master]
   pull_request:
-  pull_request_target:
-    types: [opened, synchronize, reopened, ready_for_review]
+  schedule:
+    - cron: "17 5 * * *"  # nightly Windows 4-shard run on main
 
 permissions:
   contents: read
@@ -13,31 +13,9 @@ permissions:
 # USR-300: cancel superseded runs of the same PR only; never cancel pushes to main.
 concurrency:
   group: ci-${{ github.event_name }}-${{ github.event.pull_request.number || github.ref }}
-  cancel-in-progress: ${{ github.event_name == 'pull_request' || github.event_name == 'pull_request_target' }}
+  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
 
 jobs:
-  trusted-pr-policy:
-    if: github.event_name == 'pull_request_target'
-    runs-on: ubuntu-latest
-    steps:
-      - name: Checkout trusted base
-        uses: actions/checkout@v4
-        with:
-          ref: ${{ github.event.pull_request.base.sha }}
-          path: trusted
-          fetch-depth: 1
-          persist-credentials: false
-      - name: Checkout candidate merge without executing it
-        uses: actions/checkout@v4
-        with:
-          ref: refs/pull/${{ github.event.pull_request.number }}/merge
-          path: candidate
-          fetch-depth: 0
-          persist-credentials: false
-      - name: Enforce base-owned governance policy
-        working-directory: candidate
-        run: python ../trusted/core/orchestrator/guard.py ${{ github.event.pull_request.base.sha }}
-
   # USR-300: classify the change set. Governance/code/config changes are always heavy.
   changes:
     if: github.event_name == 'pull_request'
@@ -102,14 +80,6 @@ jobs:
         include:
           - os: ubuntu-latest
             shard: ""
-          - os: windows-latest
-            shard: 1
-          - os: windows-latest
-            shard: 2
-          - os: windows-latest
-            shard: 3
-          - os: windows-latest
-            shard: 4
     runs-on: ${{ matrix.os }}
     steps:
       - name: Checkout trusted base
@@ -149,12 +119,7 @@ jobs:
         shell: bash
         env:
           DARKFAC_HARNESS_CACHE: "off"
-        run: |
-          if [ -n "${{ matrix.shard }}" ]; then
-            python core/harness/runner.py --quick --shard "${{ matrix.shard }}/4"
-          else
-            python core/harness/runner.py --quick
-          fi
+        run: python core/harness/runner.py --profile pr
 
   # Required-check names kept stable: always-run aggregators (skipped heavy jobs still pass).
   pr-validation-ubuntu:
@@ -176,22 +141,24 @@ jobs:
 
   pr-validation-windows:
     name: pr-validation (windows-latest)
-    needs: [changes, pr-tests]
+    needs: [changes, ledger-fast, pr-tests]
     if: always() && github.event_name == 'pull_request'
     runs-on: ubuntu-latest
     steps:
-      - name: Aggregate result
+      - name: Aggregate result (Windows now runs on main + nightly; kept for a stable required-check name)
         run: |
           heavy="${{ needs.changes.outputs.heavy }}"
-          echo "changes=${{ needs.changes.result }} heavy=$heavy pr-tests=${{ needs.pr-tests.result }}"
+          echo "changes=${{ needs.changes.result }} heavy=$heavy ledger-fast=${{ needs.ledger-fast.result }} pr-tests=${{ needs.pr-tests.result }}"
           [ "${{ needs.changes.result }}" = "success" ] || exit 1
           if [ "$heavy" = "true" ]; then
             [ "${{ needs.pr-tests.result }}" = "success" ] || exit 1
+          else
+            [ "${{ needs.ledger-fast.result }}" = "success" ] || exit 1
           fi
 
   main-tests:
     name: main-tests (${{ matrix.os }}${{ matrix.shard && format(' {0}/4', matrix.shard) || '' }})
-    if: github.event_name == 'push'
+    if: github.event_name == 'push' || github.event_name == 'schedule'
     strategy:
       fail-fast: false
       matrix:
@@ -230,7 +197,7 @@ jobs:
 
   main-validation:
     needs: main-tests
-    if: always() && github.event_name == 'push'
+    if: always() && (github.event_name == 'push' || github.event_name == 'schedule')
     runs-on: ubuntu-latest
     steps:
       - name: Aggregate result (all shards must pass)
```

Contratos do repositório inteiro (`PR_ALWAYS_CONTRACT_TESTS` em `core/harness/affected.py`: guards marcados, censo de ids de modelo e drift de docs de modelo) rodam sempre no perfil de PR, também no fallback de fumaça. No fallback entram ainda os testes modificados no diff e os testes que citam, pelo caminho pontilhado ou por import direto, o módulo `.py` alterado (busca textual com cache por execução), limitados aos `PR_FALLBACK_MAX_CITED_FILES = 25` mais específicos (menor fanout) quando um módulo quente explode a seleção. Também entram os testes que citam pelo caminho um arquivo não-Python alterado no mesmo diff (por exemplo, `.gitignore`), mesmo quando outro arquivo escala para o fallback (USR-575: o #934 mudou `.gitignore` junto de um módulo quente e pulou os testes de higiene que quebraram o `main`). Alterações em ledgers gerados mapeados em `LEDGER_DATA_FILES` (`.factory/demands/demands.json`, `.factory/owner_actions/owner_actions.json`, `.factory/learning/learning_ledger.json`) selecionam apenas os testes de higiene correspondentes em vez de casar referências de fixture em dezenas de testes não relacionados. A suíte completa continua garantida em Linux/CI de `main`.
