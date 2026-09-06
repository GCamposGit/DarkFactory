# Roadmap Operacional — relatório de implementação

## Resultado

Implementado o painel operacional RM-01–RM-07 do DarkHub. A tela é uma projeção somente leitura do manifesto versionado `.factory/roadmap/darkfac.json`; não existe estado de planejamento paralelo no frontend.

## Entregas

- `core/roadmap/`: contratos Pydantic, adapter de fonte JSON, normalização de identidade, precedência explícita, checker de ciclos/conflitos/órfãos/evidências, snapshot store e serviço de consulta.
- `core/roadmap/cli.py`: inspeção de snapshot e health usando o mesmo serviço consumido pelo HTTP.
- `hub/backend/api.py`, `hub/backend/service.py`, `hub/backend/main.py`: endpoints `/api/projects/{project_id}/roadmap` e aliases sem `/api`, item, health e fonte navegável.
- `hub/frontend/roadmap.js`, `hub/frontend/index.html`, `hub/frontend/styles.css`: drawer por projeto, filtros, visão geral por macroetapa, linha do tempo sem datas inventadas, grafo causal, tabela acessível, alertas de consistência e detalhe de proveniência.
- `docs/ROADMAP_OPERACIONAL.md`: fonte humana e política de fidelidade do roadmap aprovado.
- `tests/test_roadmap.py`: cobertura de contrato, determinismo, isolamento, fallback stale, falha fechada, API, CLI e superfície visual.

## Decisões de fidelidade

- Datas ausentes continuam ausentes; o campo de horizonte não é convertido em data.
- `related_to` não participa de bloqueios nem de ciclos causais.
- Conflitos mostram a versão de maior precedência, mas mantêm as duas fontes e um alerta explícito.
- Fonte indisponível sem snapshot anterior retorna erro estruturado; com snapshot anterior, a resposta fica marcada como stale.
- O Canaletto não é registrado como projeto nem como fonte.
- RM-08 (atualização incremental, telemetria e escala) e RM-09 (histórico/diff) permanecem adiados conforme o roadmap aprovado.

## Validação executada

- `python -m pytest tests/test_roadmap.py -v` — 13 passed.
- `node --check hub/frontend/roadmap.js` — passed.
- `python core/harness/runner.py --quick` — `[HARNESS_PASS]`, 187 passed.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` — 187 passed.
- `git diff --check` — passed para os arquivos rastreados da feature.
