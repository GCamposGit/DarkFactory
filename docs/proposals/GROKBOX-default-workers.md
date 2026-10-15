# Proposta: grokbox como worker padrao do portao e sonda /health de 10 s (GROKBOX)

`core/harness/*` e protegido por `core/orchestrator/guard.py`: o job `trusted-pr-policy` reprova PR que
o altere, e so o owner aplica por commit direto. Por isso a fabrica NAO altera os arquivos abaixo; este
documento e o patch ao lado (`docs/proposals/GROKBOX-default-workers.patch`) trazem a mudanca.

| Arquivo | Mudanca |
|---|---|
| `core/harness/remote_dispatch.py` | `DEFAULT_WORKERS = "http://100.77.130.5:8080,http://100.78.181.90:8080"` (grokbox, depois Desktop); `HEALTH_PROBE_TIMEOUT_SEC = 10.0` |
| `core/harness/test_subagent.py` | `remote_worker_url` e `--worker-url` passam a `http://100.77.130.5:8080` |

## 1. Problema e evidencia

Em 14/10/2026 o grokbox (`http://100.77.130.5:8080`, Linux, Python 3.13) rodou `runner.py --quick` com
4157 testes passando em 143 s; o Notebook leva 23-30 min e o Desktop estava offline. A rota para o
grokbox usa relay DERP (sem UDP direto, a porta 41641 nao pode ser liberada), e a sonda `/health` de 2 s
(`HEALTH_PROBE_TIMEOUT_SEC`) as vezes estoura; com 10 s funciona. Hoje o padrao do codigo so conhece o
Desktop, entao cada maquina precisa exportar `DARKFAC_TEST_WORKERS` na mao.

## 2. Passo exato do owner

Em `C:\dev\DarkFac`, no `main` atualizado (PowerShell, um comando por vez):

```powershell
git -C C:\dev\DarkFac pull --ff-only origin main
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\GROKBOX-default-workers.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\GROKBOX-default-workers.patch
python -m pytest C:\dev\DarkFac\tests\test_remote_dispatch.py C:\dev\DarkFac\tests\test_remote_dispatch_resilience.py C:\dev\DarkFac\tests\test_worker_onprem_test_subagent.py C:\dev\DarkFac\tests\test_worker_silence_resume.py -q -p no:cacheprovider
git -C C:\dev\DarkFac add core/harness/remote_dispatch.py core/harness/test_subagent.py
git -C C:\dev\DarkFac commit -m "feat(harness): grokbox como worker padrao e sonda /health de 10 s [GROKBOX]"
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac rebase origin/main
git -C C:\dev\DarkFac push origin HEAD:main
```

Notas:

- Commite SOMENTE os 2 caminhos acima (nunca `git add -A`).
- Direto no `main`, sem PR. Antes do push, `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main`
  acusa `GUARD VIOLATION` (esperado); depois do push fica `[GUARD PASS]`.
- Os testes que afirmavam `2.0` e o URL do Desktop como padrao ja foram tornados independentes do valor
  (`tests/test_remote_dispatch_resilience.py`, `tests/test_worker_onprem_test_subagent.py`), entao passam
  antes e depois do patch.
- Nao precisa de deploy: o compose da VPS ja injeta as variaveis (`docker-compose.cloud.yml`).

## 3. Verificacao

`git apply --check` do patch na worktree da entrega (base `fde6b32`): limpo. Apos o push, o runbook
`docs/runbooks/grokbox_test_worker.md` (item 6) descreve o portao so remoto para confirmar.
