# USR-421 — encerramento do pytest focal no Windows

Data: 29/10/2026. Ambiente: Windows, Python 3.12.

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

`tests/_cache_dir.py` agora mantém o cache por worktree em `DarkFac/pytest-cache` sob o diretório
temporário do usuário. O cache continua fora do checkout e preserva os dados de `--lf` entre runs.

Após a mudança:

- `python -m pytest tests/test_fleet_dispatcher.py -q` terminou com código 0 e resumo `17 passed
  in 2.57s`.
- `python -m pytest tests/test_pytest_cache_dir_worktree.py -q` terminou com código 0 e resumo
  `2 passed in 1.69s`; esse teste confirma cache persistente de `--lf`, cache fora da worktree e
  remoção do checkout temporário.

Os tempos são desta execução e não constituem um novo limite de produto. O deadline configurado
por teste permanece inalterado (`timeout = 120` em `pytest.ini`).
