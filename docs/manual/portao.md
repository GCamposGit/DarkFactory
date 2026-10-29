<!-- covers: core/harness/runner.py, core/harness/affected.py -->
# Portao em camadas (USR-377 S5)

O runner aceita `--profile pr|full` (padrao `full`, comportamento inalterado).

- `pr`: `syntax_only`, `static_types`, `affected_tests`
  (`python -m core.harness.affected --base origin/main --run`; escala para a suite
  completa se mudar config, governanca ou harness) e `smoke_tests` (`-m smoke`).
  Forca `--no-cache --local`. Mudanca so de docs roda apenas o smoke.
- `full`: os mesmos checks de sintaxe/tipos mais a suite completa (paralela e serial).

Quando `affected` recebe `--base HEAD` em uma worktree limpa, ele retorna somente
os guards globais registrados sem montar o grafo de imports. Com qualquer mudanca
staged, unstaged ou untracked, a selecao usa o diff e a analise normal.

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
