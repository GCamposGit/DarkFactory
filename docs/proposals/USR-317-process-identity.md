# USR-317 - identidade de processo do worker sem boot_time

`core/harness/*` e caminho protegido: agentes nao podem commitar. O patch
`docs/proposals/USR-317-process-identity.patch` (formato `git diff`, relativo a raiz do repo,
aplicavel com `git apply` sobre a main atual) contem a mudanca completa. Os testes novos ja estao em
`tests/test_process_identity_usr317.py` e ficam em SKIP ate o patch ser aplicado (verde antes e depois).

## Problema

No Linux, `psutil.Process.create_time()` = `boot_time + starttime`, e o `boot_time` e recalculado depois
de cada congelamento/restauro da VM (grokbox congela a cada 5-12 min). `remote_worker.py` (linhas ~448,
~483, ~514) e `suite_lock.py` (~272) comparavam `create_time()` com o horario gravado (tolerancia 2 s e 5 s):
processo vivo era declarado morto (recuperacao de orfaos matava job vivo; `_ProcessTree` ignorava a raiz).
Alem disso `app = create_worker_app()` na importacao criava o JobManager na pasta real e rodava a
recuperacao so por importar o modulo (checagem de prontidao do supervisor da VM).

## Mudanca

- Novo `core/harness/process_identity.py` (stdlib): `proc_start_ticks(pid)` le o campo 22 de
  `/proc/<pid>/stat` (parse apos o ULTIMO `)`), so no Linux; `same_process(proc, started_at, start_ticks, ...)`
  compara os ticks de forma exata quando gravados; sem ticks (Windows/macOS ou registro antigo) mantem
  `create_time` com tolerancia. Uma unica regra para worker e trava.
- `remote_worker.py`: `HarnessJob` ganha `owner_start_ticks` e `child_start_ticks` (gravados no registro JSON,
  retrocompativel); `_process_alive`, `_terminate_process_tree` e `_ProcessTree` usam `same_process`;
  `_ProcessTree.kill` deixa de usar `is_running()` (que tambem compara create_time) quando ha ticks.
- `suite_lock.py`: o sidecar grava `start_ticks`; `_sample_holder_tree` usa `same_process(..., only_younger=True)`.
- `app` vira lazy via `__getattr__` do modulo (PEP 562): `from core.harness.remote_worker import app` e
  `uvicorn core.harness.remote_worker:app` continuam funcionando; importar nao cria JobManager. `main()` ja
  usava `create_worker_app(...)` e nao muda. Nenhum chamador de `remote_worker.app` existia no repo/scripts/runbooks.
- Limite conhecido: registros gravados ANTES do patch nao tem ticks e caem no criterio antigo (uma vez, na troca).

## Como aplicar

```powershell
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac worktree add C:\dev\DarkFac\.worktrees\usr-317-owner -b owner/usr-317 origin/main
cd C:\dev\DarkFac\.worktrees\usr-317-owner
git apply docs\proposals\USR-317-process-identity.patch
```

## Como validar

```powershell
python -m pytest C:\dev\DarkFac\.worktrees\usr-317-owner\tests\test_process_identity_usr317.py C:\dev\DarkFac\.worktrees\usr-317-owner\tests\test_remote_job_recovery.py C:\dev\DarkFac\.worktrees\usr-317-owner\tests\test_suite_lock_stalled.py C:\dev\DarkFac\.worktrees\usr-317-owner\tests\test_clock_bomb_guard.py C:\dev\DarkFac\.worktrees\usr-317-owner\tests\test_harness_contract.py -q -p no:cacheprovider
python C:\dev\DarkFac\.worktrees\usr-317-owner\core\harness\runner.py --quick
```

O portao deve imprimir `[HARNESS_PASS]` antes do push. Depois do merge, atualizar o worker do grokbox.
