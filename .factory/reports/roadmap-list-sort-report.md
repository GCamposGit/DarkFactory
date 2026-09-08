# Roadmap — lista compacta e ordenação

## Resultado

- A visão geral deixou de usar cards lado a lado e passou a usar linhas compactas agrupadas por macroetapa.
- Cada linha mantém ID, título, status, horizonte, tipo, alertas, dependências e fontes, com acesso ao drawer de detalhes.
- A tabela acessível ganhou ordenação por item, tipo, etapa, estado, horizonte, dependências e proveniência.
- A ordenação é estável, alterna crescente/decrescente, persiste ao aplicar filtros e anuncia o estado via `aria-sort` e região viva.

## Arquivos

- `hub/frontend/roadmap.js`
- `hub/frontend/styles.css`
- `tests/test_roadmap.py`

## Validação

- `node --check hub/frontend/roadmap.js` — passou.
- `python core/harness/runner.py --quick` — `[HARNESS_PASS]`, 203 passed, 1 skipped.
