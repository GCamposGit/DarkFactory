# USR-421 — encerramento do pytest focal no Windows

Reprodução em 29/10/2026. Verificação do redirecionamento em 30/10/2026. Ambiente: Windows, Python 3.12.

## Reprodução

Comando: `python -m pytest tests/test_fleet_dispatcher.py -q`.

O pytest executou os 17 casos e exibiu `[100%]`, mas o processo não retornou nem imprimiu o
resumo. Uma segunda execução com `faulthandler.dump_traceback_later(18, repeat=True)` capturou
repetidamente a thread principal neste caminho sanitizado:

```text
pytest_sessionfinish
  -> _pytest.cacheprovider.Cache.set
  -> _ensure_cache_dir_and_supporting_files
  -> _make_cachedir
  -> tempfile.mkdtemp
```

O destino resolvido pela fixture era o cache por worktree sob `%LOCALAPPDATA%`. A criação de
diretório nesse destino ficou bloqueada pelo limite de escrita do ambiente isolado; a tentativa
equivalente sob o diretório temporário do usuário terminou imediatamente. O stack não mostrou
thread ou subprocesso do `fleet_dispatcher` mantendo o pytest aberto.

## Correção e repetição

`tests/_cache_dir.py` mantém o cache por worktree em `DarkFac/pytest-cache` sob o diretório
temporário do usuário. `redirect_cache_dir` grava esse destino antes de o cache plugin ler
`cache_dir` e reaplica o caminho no `Cache` já criado no início de `pytest_sessionfinish`, antes
de `Cache.set`. O cache continua fora do checkout e preserva os dados de `--lf` entre runs.

Após a mudança, verificação em 30/10/2026 neste worktree:

- `python -m pytest tests/test_fleet_dispatcher.py -q` terminou sozinho com código 0 e resumo
  `17 passed in 3.79s`.
- `python -m pytest tests/test_pytest_cache_dir_worktree.py -q` terminou com código 0 e resumo
  `3 passed in 4.76s`. O caso do foco vive nesse arquivo, fora de `tests/test_fleet_dispatcher.py`,
  e invoca esse arquivo uma vez com o `pytest.ini` do repositório. O subprocesso não usa
  `-p no:cacheprovider` nem esvazia `addopts`. Ele roda com `LOCALAPPDATA` apontando para um
  arquivo (destino inutilizável) e `TEMP`/`TMP` graváveis no diretório temporário do usuário,
  termina dentro de 60 s (abaixo de `timeout = 120`), imprime a linha de resumo no formato
  `N passed in Xs` e devolve código 0. O outro caso grava `lastfailed` só no retorno de
  `external_cache_dir` da worktree do teste e não cria `.pytest_cache` no checkout.

Os tempos são desta execução e não constituem um novo limite de produto. O deadline configurado
por teste permanece inalterado (`timeout = 120` em `pytest.ini`).
