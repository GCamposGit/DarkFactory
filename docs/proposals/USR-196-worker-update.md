# Proposta: /system/update do worker sobrevive a arquivos que o upstream deixou de versionar (USR-196)

`core/harness/*` e protegido por `core/orchestrator/guard.py`: o job `trusted-pr-policy` do CI reprova qualquer PR que o altere, e so o owner aplica por commit direto. Por isso a fabrica NAO altera `core/harness/remote_worker.py`: este documento e o patch ao lado (`docs/proposals/USR-196-worker-update.patch`) trazem a mudanca ja testada. Os outros dois itens do USR-196 (erro real do `/system/update` no relatorio do deploy e backup da VPS independente do Desktop) ja estao no PR da fabrica, em arquivos nao protegidos.

O patch toca 1 arquivo protegido (`core/harness/remote_worker.py`). O teste novo vai como `docs/proposals/USR-196-test_worker_update_removed_files.py.txt` (se estivesse em `tests/` ele reprovaria o portao da branch, porque so passa depois do patch); o owner o copia para `tests/` no mesmo commit.

## 1. Problema e evidencia

No deploy de 09-10/10/2026 o Desktop (`DESKTOP-G45IPEM`) ficou em `e8fff1d`: o commit que desversionou `.factory/telegram/config.json` (USR-184) removeu o arquivo do indice, mas o checkout do worker ainda o rastreava com modificacoes locais (o token novo gravado nele). O `git pull` do `/system/update` aborta nesse caso ("Your local changes to the following files would be overwritten by merge"), o worker nunca chega ao SHA esperado e todo redeploy sai com codigo 1 ("Desktop update did not reach expected SHA"). Nenhum agente consegue destravar isso de fora: o worker so executa `git pull`.

## 2. O que o patch faz

Em `update_system` (`POST /system/update`), antes do `git pull`, a nova funcao `preserve_files_unversioned_upstream(root_path, subp_kwargs)`:

1. Descobre o upstream (`@{u}`), roda `git fetch --quiet` e calcula o merge-base com `HEAD`.
2. Lista os arquivos removidos pelo upstream desde o merge-base (`git diff --diff-filter=D`) que tambem estao modificados localmente (`git diff HEAD`, staged ou nao).
3. Para cada um, copia o arquivo para `.factory/backups/worker_update/<AAAAMMDDTHHMMSS>/<caminho original>` (a pasta `.factory/backups/` ja e ignorada pelo git) e SO ENTAO executa `git checkout HEAD -- <arquivos>`, para o pull poder remove-los sem abortar.
4. Acrescenta ao `output` da resposta linhas `[USR-196] ...` dizendo quais arquivos foram preservados e onde (o cliente `core/infra/node_sync.py` ja leva esse `output` ao relatorio do deploy, redigido e truncado).

Garantias (cobertas pelos 6 testes):

- Arquivo que continua versionado nunca e tocado: se o upstream tambem o altera, o pull aborta como antes e a modificacao local fica intacta, sem backup.
- Arquivo removido pelo upstream mas SEM modificacao local: nada a preservar, o pull o apaga normalmente.
- Se a copia de backup falhar, o arquivo NAO e descartado (nada de perder dado); a nota `backup failed` vai no `output`.
- Sem upstream configurado, fora de repositorio git ou com qualquer erro de git, a funcao devolve lista vazia e o update segue exatamente como antes (nunca levanta excecao).
- Os comandos novos rodam sem shell (`subprocess.run([...])`); o `git pull` e o `rev-parse` existentes ficam como estavam.

## 3. Passo exato do owner

Em `C:\dev\DarkFac`, no `main` atualizado (PowerShell, um comando por vez):

```powershell
git -C C:\dev\DarkFac pull --ff-only origin main
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\USR-196-worker-update.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\USR-196-worker-update.patch
Copy-Item C:\dev\DarkFac\docs\proposals\USR-196-test_worker_update_removed_files.py.txt C:\dev\DarkFac\tests\test_worker_update_removed_files.py
python -m pytest C:\dev\DarkFac\tests\test_worker_update_removed_files.py C:\dev\DarkFac\tests\test_worker_onprem_test_subagent.py C:\dev\DarkFac\tests\test_remote_worker_restart.py -q
git -C C:\dev\DarkFac add core/harness/remote_worker.py tests/test_worker_update_removed_files.py
git -C C:\dev\DarkFac commit -m "fix(harness): /system/update do worker preserva e descarta arquivos que o upstream deixou de versionar [USR-196]"
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac rebase origin/main
git -C C:\dev\DarkFac push origin HEAD:main
```

Notas:

- Commite SOMENTE os 2 caminhos acima (nunca `git add -A`: o checkout compartilhado pode ter alteracoes locais soltas).
- Direto no `main`, sem PR (um PR seria reprovado pelo `trusted-pr-policy`). Antes do push, `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main` acusa `GUARD VIOLATION` (esperado: o guard compara caminhos, nao autoria); depois do push o diff some e fica `[GUARD PASS]`.
- O patch so passa a valer no Desktop depois que o checkout do worker puxar o `main` e o processo do worker for reiniciado. O destravamento manual unico do Desktop ja foi feito em `OA-008` (marcado `done`); este patch evita a recorrencia: dali em diante o proprio `/system/update` cuida de casos iguais. Se o Desktop travar de novo ANTES do patch chegar nele, repita o passo de `OA-008`.
- Depois do merge siga o passo normal de deploy (`python C:\dev\DarkFac\scripts\dokploy_redeploy.py`); nenhuma mudanca de produto vai junto.
- O patch foi verificado com `git apply --check` + `git apply` numa arvore detached limpa de `origin/main` (`147b3cf`), seguido dos testes focais (secao 4).

## 4. Testes

`tests/test_worker_update_removed_files.py` (novo, 6 testes, git real em `tmp_path`: origin bare + seed + checkout do worker, sem rede):

- arquivo desversionado e modificado localmente: copia fiel no backup, modificacao local descartada, pull chega ao commit esperado, outro arquivo modificado que continua versionado permanece intacto;
- arquivo removido sem modificacao local: sem backup;
- arquivo que continua versionado e foi alterado nos dois lados: pull aborta, nada e descartado, o stderr real do git ("overwritten") aparece no `output`;
- falha na copia (`shutil.copy2` forcado a falhar): o arquivo local fica intacto;
- sem upstream configurado e fora de repositorio: lista vazia, sem excecao.

Resultado na arvore limpa de `origin/main` + `git apply`: `6 passed`; `tests/test_worker_onprem_test_subagent.py` + `tests/test_remote_worker_restart.py`: `21 passed`. Sem o patch, os 4 testes que exercem a funcao nova falham (cenario do Desktop reproduzido).

## 5. Riscos e limites

- O descarte so atinge arquivos que o upstream removeu E que tem modificacao local; a copia fica em `.factory/backups/worker_update/` (ignorada pelo git, sem rotacao automatica: o owner pode apaga-la quando quiser).
- `git fetch --quiet` roda antes do pull (que ja buscaria de qualquer forma); se o fetch falhar, a preparacao e pulada e o pull relata o erro real, que chega ao relatorio do deploy.
- Arquivos NAO rastreados que o upstream passa a adicionar (erro "untracked working tree files would be overwritten") continuam fora do escopo desta mudanca.
