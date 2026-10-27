<!-- covers: core/harness/docs_sync.py -->
# Docs-sync: o manual acompanha o codigo (USR-376)

Cada pagina de `docs/manual/*.md` declara na primeira linha os arquivos que descreve:

```
<!-- covers: core/modulo/arquivo.py, scripts/*.py -->
```

Regra para owner e agentes: se um arquivo coberto muda, a pagina correspondente muda no mesmo PR. Arquivos sob `tests/` nunca disparam a checagem. Pagina sem cabecalho nao cobre nada (a checagem avisa em stderr).

Quando a mudanca nao exige atualizar o manual, coloque no corpo de um commit do PR o trailer:

```
Docs-Unchanged: refatoracao interna, comportamento descrito no manual intacto
```

O motivo e obrigatorio (nao pode ficar vazio).

## Rodar localmente

```powershell
python -m core.harness.docs_sync --base origin/main --head HEAD
```

Codigo 0 = ok; 1 = lista "arquivo coberto alterado -> pagina que deveria mudar".

## Dicionario de dados (USR-403)

`python -m core.harness.docs_sync --dictionary` (fora do portao) lista os `.factory/**/*.json` versionados sem padrao na linha `<!-- paths: -->` de `docs/manual/dicionario_dados.md`; codigo 1 se faltar algum.

## Passo de CI (acao do owner)

`.github/workflows/*` e protegido pelo guard, entao o passo abaixo deve ser aplicado pelo owner no job de PR existente (o checkout precisa de `fetch-depth: 0`):

```yaml
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: Docs-sync do manual
        run: python -m core.harness.docs_sync --base origin/${{ github.base_ref }} --head HEAD
```
