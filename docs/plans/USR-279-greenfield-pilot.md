# USR-279 - Piloto greenfield stand alone (Fatia 1: plano)

Status: plano. Nenhum codigo do produto e nenhum repositorio foram criados nesta fatia.
Decisao do owner (16/10/2026): piloto tecnico pequeno e descartavel; repositorio privado na conta GitHub do owner; ambiente separado na infraestrutura existente; sem novas contratacoes.

Dependencias (ainda nao mergeadas quando este plano foi escrito): USR-274 (bootstrap), USR-275 (contrato de ambiente), USR-278 (roteamento Telegram por projeto).

## 1. Piloto e fronteiras do produto

Produto proposto: **pilot-hello** - pagina estatica unica mais servidor HTTP minimo com `/healthz`, somente biblioteca padrao do Python 3.12 (`http.server`).

Justificativa: zero dependencias externas (nada para instalar ou auditar); uma unica porta; healthcheck e smoke triviais e deterministicos; testavel com `unittest`; empacotavel em Dockerfile curto para o Dokploy; prova a cadeia completa sem risco de negocio.

Dentro do escopo:
- `GET /` devolve HTML estatico com titulo e versao; `GET /healthz` devolve JSON `{"status":"ok","version":"<semver>"}`.
- `python -m pilot_hello` sobe o servidor (porta pela variavel `PORT`, padrao 8080).
- Testes `unittest`, build e smoke por requisicao HTTP ao `/healthz`.

Fora do escopo: banco de dados, autenticacao, dominio proprio, servicos pagos, qualquer import de `core.*` ou chamada a servico DarkFac em runtime, regras de negocio alem do texto da pagina.

Nome do repositorio e texto da pagina sao detalhes do produto descartavel; ver decisoes D1 e D2.

## 2. Ambiente (contrato USR-275)

- Descritor do projeto (`ProjectDescriptor.environment`): `kind: venv`, `path: .venv` dentro do checkout do produto; `setup`: `python -m venv .venv` (e `pip install -e .`, se houver); comandos do projeto: `setup`, `test`, `build`, `smoke`.
- Execucao sempre por `run_in_environment` (variaveis em lista de permissao; sem `PYTHONPATH`, `VIRTUAL_ENV` ou `DARKFAC_*` do core; segredos mascarados).
- Python 3.12+ no PATH do host de execucao (Notebook, Desktop ou grokbox, conforme o roteador).

## 3. Validacao propria do produto (sem o portao do core)

- `test`: `python -m unittest discover -s tests -q`.
- `build`: `python -m compileall -q src` e `python -m build --wheel` quando `build` estiver no venv (senao, so compileall; ver D5).
- `smoke`: inicia o servidor em porta efemera, `GET /healthz` deve devolver 200 e `"status":"ok"`; encerra o processo.
- CI do produto: workflow proprio no repositorio do piloto (instala, testa, builda), gerado pelo bootstrap, nao copiado do core.

## 4. Destino permitido

Repositorio: privado, na conta GitHub do owner, criado pelo `GitHubProvider` do USR-274 (usa `gh` ja autenticado; nenhum repositorio e criado antes de M1).

Opcoes de destino de deploy encontradas no repositorio (infra existente, sem novo custo):
- A) **Dokploy existente** (https://dokploy.ggcampos.com, VPS Hetzner CX23, ADR-002/ADR-004): novo projeto Dokploy separado do `darkfac-core`, com uma aplicacao (Dockerfile) ligada ao repositorio privado; webhook GitHub conforme `docs/runbooks/github_dokploy_webhook.md`. Token em `DOKPLOY_API_URL`/`DOKPLOY_API_KEY` (padrao de `scripts/dokploy_redeploy.py` e `docs/runbooks/dokploy_redeploy.md`).
- B) Mesmo Dokploy, mas acessivel apenas via Tailscale (sem exposicao publica); smoke feito de dentro da tailnet.
- C) Execucao local no Notebook/Desktop (porta local) como ultimo recurso; prova de publicacao fraca, so se A e B forem inviaveis.

Recomendacao (nao decisao): A, com o dominio gerado pela infraestrutura ja existente (sem novo dominio pago). A escolha final e do owner (D3).

## 5. Decisoes do owner (nao decididas aqui)

- D1 Nome do repositorio privado. Opcoes: `pilot-hello` (recomendado) ou nome livre.
- D2 Texto da pagina. Opcoes: texto neutro "DarkFac pilot" (recomendado) ou texto do owner.
- D3 Destino de deploy: A, B ou C (secao 4). Se A/B: nome do projeto Dokploy separado (sugestao `pilot-hello`) e visibilidade (publica pelo dominio da infra vs. so tailnet).
- D4 Retencao apos M7: manter arquivado (recomendado) ou remover, por acao do proprio owner.
- D5 Build: aceitar `build` como ferramenta de desenvolvimento no venv (recomendado) ou usar so compileall mais build da imagem Docker.

## 6. Marcos executaveis (ate tres criterios cada)

M1 - Bootstrap (USR-274) - CONCLUIDO em 20/10/2026 (evidencia: docs/evidence/USR-279-M1.md; https://github.com/GCamposGit/pilot-hello)
1. Repositorio privado criado na conta do owner pelo `GitHubProvider`, com README, `pyproject.toml`, testes e CI do produto, e registro no registry de projetos.
2. Reexecucao do bootstrap e idempotente (sem novo repositorio nem commits duplicados).
3. Repositorio nao contem referencia a `C:\dev\DarkFac` nem import de `core.*`.

M2 - Ambiente (USR-275)
1. Descritor declara `environment` venv e comandos `setup/test/build/smoke`.
2. `setup_environment` cria o venv dentro do checkout do produto e os testes iniciais passam nele.
3. Ambiente do processo filho nao contem `PYTHONPATH`, `VIRTUAL_ENV` do core nem `DARKFAC_*`; segredos mascarados nos logs.

M3 - Demanda (USR-278)
1. Owner envia a demanda ("pagina e /healthz") por Telegram ou Hub e ela cai no projeto do piloto, nao em `darkfac`.
2. Gate G1 aplicado: ambiguidades do produto viram perguntas ao owner (D1-D5), sem suposicao.
3. Ticket do piloto registrado com ID, projeto e vinculo a demanda.

M4 - Linha ate merge
1. Implementacao, revisao em familia diferente do implementador e PR no repositorio privado.
2. CI do produto verde para o SHA do PR; sem conflito com `main`.
3. Merge squash e limpeza de branch/worktree, com SHA de merge registrado.

M5 - Deploy e smoke
1. Aplicacao implantada no destino escolhido em D3, separada de `darkfac-core`.
2. `GET /healthz` devolve 200 e a versao correspondente ao SHA mergeado.
3. Evidencia (URL, SHA, saida do smoke, horario) vinculada ao ticket.

M6 - Prova de independencia
1. Clone novo do repositorio, em diretorio sem checkout DarkFac, instala, testa e builda.
2. Servidor sobe e o smoke passa com PATH/variaveis sem nada do core e sem servico DarkFac acessivel.
3. Busca no clone nao encontra `darkfac` nem `core.` em codigo executavel (somente documentacao, se houver).

M7 - Relatorio final
1. Relatorio ao owner com linha do tempo, SHAs, PR, CI, deploy, smoke e intervencoes manuais contadas.
2. Falhas encontradas retomadas ou diagnosticadas, cada uma com commit/PR ou ticket formal.
3. D4 executada ou registrada como acao do owner; criterios 2 e 3 do USR-279 marcados com evidencia.

## 7. Acoes do owner necessarias (a registrar como OA pelo orquestrador)

1. Responder D1-D5 (secao 5) - prioridade high; bloqueia M1 e M5.
2. Confirmar `gh auth status` com escopo `repo` na conta do owner, para o `GitHubProvider` criar repositorio privado (sem expor token no chat) - high; bloqueia M1.
3. Se destino A/B: criar no Dokploy o projeto separado e a aplicacao ligada ao repositorio privado, concedendo acesso (GitHub App ou deploy key) somente a esse repositorio, e conferir `DOKPLOY_API_KEY` no ambiente de usuario - high; bloqueia M5.
4. Configurar o webhook GitHub para Dokploy no repositorio do piloto (runbook `github_dokploy_webhook.md`) - medium; bloqueia deploy automatico em M5.
5. Apos M7: decidir a retencao (D4) - low.
