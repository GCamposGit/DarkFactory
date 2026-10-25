# Testes

## Testes de forma

Teste de forma e o que so confere a existencia de um arquivo, um trecho de texto dentro de markdown, skill, docs ou HTML, ou a presenca de chaves em um JSON/YAML de configuracao, sem executar comportamento. Ele passa mesmo que o produto esteja quebrado, entao nao prova nada sobre o produto e nao deve segurar o portao.

Regras:

- Esses testes vivem em `tests/shape/` e ficam fora do portao: `pytest.ini` tem `--ignore=tests/shape` em `addopts`, e o harness (`python C:\dev\DarkFac\core\harness\runner.py --quick`) herda isso.
- Para roda-los quando quiser (por exemplo ao mexer em manifests de deploy ou skills):
  `python -m pytest tests/shape -q`
- Teste que exercita comportamento (chama funcao, endpoint, CLI, mutacao detectada) fica em `tests/`. Teste novo que so le texto/arquivo/chave vai direto para `tests/shape/`.
- Arquivos em `tests/shape/` usam `Path(__file__).resolve().parents[2]` para a raiz do repositorio.
