# Proposta: portao resiliente a restart/congelamento da VM do worker (WORKER-RESTART)

`core/harness/*` e protegido por `core/orchestrator/guard.py`: o job `trusted-pr-policy` reprova PR que
o altere, e so o owner aplica por commit direto. Por isso a fabrica NAO altera os arquivos abaixo; este
documento e o patch ao lado (`docs/proposals/WORKER-RESTART-resilience.patch`) trazem a mudanca.

| Arquivo | Mudanca |
|---|---|
| `core/harness/remote_dispatch.py` | job perdido por restart (`worker_restarted`, `worker_unreachable`, `job_lost_on_worker`): espera o MESMO worker voltar (`DARKFAC_WORKER_RESTART_GRACE_SEC`, padrao 180 s) e reenvia ate `DARKFAC_WORKER_RESTART_RETRIES` (padrao 2) vezes, com linhas `[REMOTE_RETRY] worker=... attempt=N/2 reason=...`; so depois cai para o proximo worker / local |
| `core/harness/remote_worker.py` | a recuperacao ao subir NAO apaga mais o worktree de job orfao (so de job terminal com 1 h ou mais); job cujo worktree sumiu ou que um sucessor ja marcou `worker_restarted` termina como `worker_restarted`, nunca como falhas de teste |
| `tests/test_remote_restart_retry.py` (novo) | copia de `docs/proposals/WORKER-RESTART-test_remote_restart_retry.py.txt` |

## 1. Problema e evidencia

Em 15/10/2026 a plataforma congelou e restaurou a VM do grokbox (kernel: "virtual machine fork"; as
18:12:29 IP e gateway mudaram e o Tailscale ficou fora ate 18:13). No portao do USR-192 o processo do
worker (pid 303996) terminou no meio do job; outra instancia recuperou o estado e marcou o job
`worker_restarted`, e essa recuperacao APAGOU o worktree do job. Os testes seguintes falharam por
arquivo ausente (432 falhas falsas). Do lado do Notebook, `/health` sumiu (3 sondas) e apareceu
`[REMOTE_JOB_LOST]`; o portao caiu para o proximo worker / local e perdeu os ~2-3 min ja gastos, sendo
que a VM voltava em menos de 1 min. Desde o commit 91fb348 o grokbox e o primeiro de `DEFAULT_WORKERS`,
entao qualquer congelamento reinicia o portao inteiro no Notebook (23-30 min).

## 2. O que muda

Dispatch (`_dispatch_with_restart_retry`, `_wait_for_worker_back`):

- So reenvia para as causas `RESTART_RETRY_ERROR_CODES`; `worker_silent`, `worker_internal_error`,
  `worker_setup_failed`, `client_deadline_exceeded` e rejeicao de credenciais caem direto, como antes.
- Espera `/health` responder e `busy=false` (sonda a cada 5 s, ate a graca). Se a graca estoura, desiste
  (`[REMOTE_RETRY] ... giving up`) e o fluxo antigo segue: `[REMOTE_FALLBACK]`, proximo worker, local.
- `DISPATCH_ATTEMPTS` ganha entradas `lost_retrying` (com `retry` e `worker_back`); o relogio de passo do
  USR-178 continua por tentativa final (`remote_spent` do `[REMOTE_FALLBACK]`), sem cobrar o tempo remoto
  de nenhum timeout de passo.
- Classificacao: `_finalize_remote_result` ja transforma `INFRASTRUCTURE_ERROR_CODES` em `RemoteJobLost`
  (nunca `[HARNESS_FAIL]`); o retry so intercepta `_WorkerUnavailable`, entao a regra e preservada.
- `DARKFAC_WORKER_RESTART_RETRIES=0` desliga o reenvio (os dois testes existentes que simulam job perdido
  ja foram ajustados para isso em `tests/test_worker_silence_resume.py` e `tests/test_remote_job_recovery.py`).

Worker:

- `_close_orphan` termina a arvore de processos e grava `worker_restarted`, mas preserva o diretorio.
- `_recover_persisted_jobs` chama `_gc_old_terminal_worktree`: so remove worktree de job terminal com
  `updated_at` a 1 h ou mais (`WORKTREE_GC_MIN_AGE_SEC`).
- `_run_job` consulta `_lost_underfoot_reason` apos o processo terminar: worktree ausente, ou registro em
  disco ja `failed/worker_restarted` (outra instancia assumiu o job), vira `worker_restarted`.

## 3. Passo exato do owner

Em `C:\dev\DarkFac`, no `main` atualizado (PowerShell, um comando por vez):

```powershell
git -C C:\dev\DarkFac pull --ff-only origin main
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\WORKER-RESTART-resilience.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\WORKER-RESTART-resilience.patch
Copy-Item C:\dev\DarkFac\docs\proposals\WORKER-RESTART-test_remote_restart_retry.py.txt C:\dev\DarkFac\tests\test_remote_restart_retry.py
python -m pytest C:\dev\DarkFac\tests\test_remote_restart_retry.py C:\dev\DarkFac\tests\test_remote_dispatch.py C:\dev\DarkFac\tests\test_remote_dispatch_resilience.py C:\dev\DarkFac\tests\test_remote_job_recovery.py C:\dev\DarkFac\tests\test_worker_silence_resume.py C:\dev\DarkFac\tests\test_remote_worker_restart.py -q -p no:cacheprovider
git -C C:\dev\DarkFac add core/harness/remote_dispatch.py core/harness/remote_worker.py tests/test_remote_restart_retry.py
git -C C:\dev\DarkFac commit -m "feat(harness): portao reenvia no mesmo worker apos restart e worker nao apaga worktree de job ativo [WORKER-RESTART]"
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac rebase origin/main
git -C C:\dev\DarkFac push origin HEAD:main
```

Depois do push, atualize o worker do grokbox (ele roda `remote_worker.py`): `POST /system/update` seguido
de `POST /system/restart` no worker (mesmo mecanismo do deploy), ou `sudo systemctl restart darkfac-test-worker` apos
`git pull` no console da VM; confirme `systemctl status
darkfac-test-worker`.

Notas:

- Commite SOMENTE os 3 caminhos acima (nunca `git add -A`).
- Direto no `main`, sem PR. Antes do push, `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main`
  acusa `GUARD VIOLATION` (esperado); depois do push fica `[GUARD PASS]`.
- Nao rode o pytest acima junto com um portao em andamento (execucoes simultaneas morrem).
- Nao precisa de deploy da VPS.

## 4. Verificacao

`git apply --check` do patch na worktree da entrega: limpo. O novo teste cobre: reenvio no mesmo worker
quando `/health` volta na 2a sonda, desistencia depois de 2 retries caindo para o proximo worker,
graca estourada sem reenvio, causas que nao reenviam, `worker_restarted` nunca `[HARNESS_FAIL]`,
recuperacao que preserva o worktree de job orfao, GC so de job terminal antigo e deteccao de job perdido
por worktree ausente ou fechado por um sucessor. Os testes usam relogio e sleep injetados.
