# DF-09 — Transporte HTTPS fail-closed para pesquisa

## Resultado

DF-09 foi implementado sem alterar o experimento Canaletto e sem criar commit.

- `core/research/transport.py` centraliza o GET HTTPS, validação de status,
  timeout, TLS, resposta vazia e payload inválido.
- Certificados são sempre verificados via certifi, trust store do sistema ou
  CA explícita (`DARKFAC_CA_BUNDLE`/`ca_bundle`). Não existe fallback para
  `_create_unverified_context`.
- `ArxivClient` usa exclusivamente `https://export.arxiv.org`.
- `ResearchSearchResult` permanece compatível com iteração/listas, mas expõe
  `success`, `empty` e `failed`, com `TransportFailure` tipado para distinguir
  timeout, TLS, transporte, HTTP inválido e resposta inválida/vazia.
- Mensagens e representações de falha não incluem URL, query, headers ou
  credenciais; o token GitHub permanece somente no header da requisição.
- O parsing de Atom/JSON inválido não é mais convertido silenciosamente em
  resultado vazio.

## Arquivos

- `core/research/transport.py`
- `core/research/arxiv_client.py`
- `core/research/github_scout.py`
- `core/research/__init__.py`
- `tests/test_research_engine.py`

## Validação

Runtime utilizado: Python bundled em
`C:\Users\guigc\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`.
Para os gates compartilhados, o diretório do runtime foi prefixado ao `PATH`
e `PYTEST_ADDOPTS=--basetemp=.pytest-tmp-df09-full` evitou a pasta temporária
global restrita desta máquina.

- `python -m pytest tests/test_research_engine.py -v`: **19 passed**.
- `python core/harness/runner.py --quick`: **191 passed, 1 skipped**,
  `[HARNESS_PASS]`.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py`:
  **191 passed, 1 skipped**, 2 warnings de dependências externas.
- `python -m py_compile` nos módulos alterados e `python -m compileall -q
  core hub run_hub.py`: passou.
- Smoke offline adicional: HTTP sem TLS rejeitado, CA inexistente falha
  fechado, corpo vazio não é busca vazia e token não aparece no contrato de
  falha: passou.

O primeiro ensaio cru do ambiente expôs ausência de `python` no PATH e acesso
negado à raiz temporária do pytest; as RCAs foram registradas no ledger
(`rca_83089701`, `rca_5c115a7b`, `rca_135666b9`, `rca_fcd383e9`,
`rca_4e35cd82`). O gate foi então repetido com o runtime já provisionado e
passou integralmente.
