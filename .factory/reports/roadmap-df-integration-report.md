# Roadmap RM + DF — relatório de correção

## Diagnóstico

O painel não filtrava por prefixo. O snapshot continha apenas RM-01–RM-09 porque o serviço registrava somente `.factory/roadmap/darkfac.json` como fonte; os tickets DF-01–DF-23 estavam documentados em `docs/DEVELOPMENT_PLAN_2026-09-05.md`, mas não eram projetados no domínio.

## Correção

- Adicionado `MarkdownDevelopmentPlanSource`, que lê a tabela DF como dados tipados, preserva a fonte navegável e interpreta dependências simples e intervalos.
- O serviço do Hub agora compila as fontes `approved-roadmap` e `development-plan` no mesmo snapshot.
- Relatórios de implementação existentes são ligados como evidência; tickets sem evidência permanecem planejados.
- Snapshots parciais continuam utilizáveis quando uma fonte auxiliar está indisponível; a indisponibilidade permanece explícita na resposta.
- A renderização frontend não precisou de filtro ou correção: ela já renderizava todos os itens recebidos pela API.

## Resultado observado

- 32 itens publicados: 9 RM + 23 DF.
- 2 fontes consultadas, nenhuma indisponível.
- 7 tickets DF com relatório de implementação foram marcados como concluídos; os demais permanecem planejados.
- Nenhuma inconsistência de dependência no snapshot atual.

## Validação

- `python core/harness/runner.py --quick` — `[HARNESS_PASS]`, 188 testes.
- `python -m pytest tests/test_roadmap.py -v` — passou.
- `node --check hub/frontend/roadmap.js` — passou.
- `git diff --check` — passou nos arquivos alterados.
