# USR-279 - Piloto greenfield stand alone (Fatia 1: plano)

Status: execucao retomada. M1 (bootstrap) e M2 (ambiente) possuem evidencias historicas em `docs/evidence/USR-279-M1.md` e `docs/evidence/USR-279-M2.md`. Esta atualizacao nao declara M3 concluido: a mensagem real do owner ainda nao foi comprovada (OA-047 aberta).
Decisoes do owner (OA-042, 20/10/2026): D1 `pilot-hello`; D2 texto da pagina `DarkFac pilot`; D3 opcao A, Dokploy existente em projeto separado; D4 manter arquivado; D5 build no venv. Registro: `.factory/owner_actions/owner_actions.json`.

Dependencias historicas: USR-274 (bootstrap), USR-275 (contrato de ambiente), USR-278 (roteamento Telegram por projeto). A verificacao atual da demanda real esta pendente de OA-043 (acessos/projeto Dokploy/webhook) e OA-047 (mensagem do owner pelo Telegram); nao simular nem inferir a entrega dessas acoes.

## 1. Piloto e fronteiras do produto

Produto proposto: **pilot-hello** - pagina estatica unica mais servidor HTTP minimo com `/healthz`, somente biblioteca padrao do Python 3.12 (`http.server`).

Justificativa: zero dependencias externas (nada para instalar ou auditar); uma unica porta; healthcheck e smoke triviais e deterministicos; testavel com `unittest`; empacotavel em Dockerfile curto para o Dokploy; prova a cadeia completa sem risco de negocio.

Dentro do escopo:
- `GET /` devolve HTML estatico com titulo e versao; `GET /healthz` devolve JSON `{"status":"ok","version":"<semver>"}`.
- `python -m pilot_hello` sobe o servidor (porta pela variavel `PORT`, padrao 8080).
- Testes `unittest`, build e smoke por requisicao HTTP ao `/healthz`.

Fora do escopo: banco de dados, autenticacao, dominio proprio, servicos pagos, qualquer import de `core.*` ou chamada a servico DarkFac em runtime, regras de negocio alem do texto da pagina.

Nome do repositorio e texto da pagina foram aprovados pelo owner em OA-042 (D1/D2 acima).

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

## 5. Decisoes do owner (OA-042, respondida)

- D1 Nome do repositorio privado: `pilot-hello`.
- D2 Texto da pagina: `DarkFac pilot`.
- D3 Destino: opcao A, projeto separado no Dokploy existente, usando dominio ja existente.
- D4 Retencao apos M7: manter arquivado.
- D5 Build: build no venv.

## 6. Marcos executaveis (ate tres criterios cada)

M1 - Bootstrap (USR-274) - CONCLUIDO conforme evidencia historica de 20/10/2026 (docs/evidence/USR-279-M1.md; https://github.com/GCamposGit/pilot-hello). Acesso atual ao repositorio nao foi confirmado nesta retomada; ver M3.
1. Repositorio privado criado na conta do owner pelo `GitHubProvider`, com README, `pyproject.toml`, testes e CI do produto, e registro no registry de projetos.
2. Reexecucao do bootstrap e idempotente (sem novo repositorio nem commits duplicados).
3. Repositorio nao contem referencia a `C:\dev\DarkFac` nem import de `core.*`.

M2 - Ambiente (USR-275) - CONCLUIDO conforme evidencia historica de 20/10/2026 (docs/evidence/USR-279-M2.md)
1. Descritor declara `environment` venv e comandos `setup/validate/build/smoke`.
2. `setup_environment` cria o venv dentro do checkout do produto e os testes iniciais passam nele.
3. Ambiente do processo filho nao contem `PYTHONPATH`, `VIRTUAL_ENV` do core nem `DARKFAC_*`; segredos mascarados nos logs.

M3 - Demanda (USR-278) - PENDENTE: a evidencia de intake real ainda falta (OA-047 aberta; docs/evidence/USR-279-M3.md).
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

## 7. Acoes do owner necessarias (registradas no backlog)

1. OA-042 (D1-D5): respondida em 20/10/2026; escolhas registradas na secao 5.
2. OA-043 (acesso `gh`, projeto Dokploy separado e webhook): aberta; bloqueia confirmar acesso/publicacao e M5.
3. OA-047 (enviar a demanda pelo Telegram e confirmar ticket PH): aberta e dependente de OA-043; bloqueia concluir M3.
4. Apos M7, aplicar D4 (manter arquivado) e registrar a verificacao correspondente.
