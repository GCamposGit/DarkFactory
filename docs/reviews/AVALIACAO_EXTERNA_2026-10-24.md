# Avaliação externa da DarkFac (24/10/2026)

Avaliação independente feita numa sessão de planejamento com o owner. Método: grill com o owner,
leitura do código e dos dados de operação (309 demandas, 70 ações do owner, 991 commits, últimos
100 PRs), métricas estáticas, execução da suíte e pesquisa de práticas atuais com fontes. Cada
afirmação abaixo tem evidência no repositório; o detalhamento por frente fica no plano
`docs/plans/PLANO_V0_2026-10-24.md`, que é o roteiro executável.

## 1. Resumo para o owner

A fábrica tem as peças certas, mas elas não estão ligadas num caminho único. O caminho que de fato
entrega (`run_ticket.py`, 534 commits) pula grill, planejamento, pesquisa, revisão, learning pack e
deploy. O caminho que tem esses estágios garantidos por código (a linha DAG, 37 commits) quase não é
usado. Cerca de 80% das intervenções do owner vêm do atrito criado pela própria fábrica (guard de
arquivos protegidos, fila de tickets no git, VPS sobrecarregada), não de decisões que só o owner pode
tomar. Não é preciso reconstruir: é preciso ligar o que existe, cortar o que sobra e tirar o atrito.

## 2. Notas (1 a 5)

| Eixo | Nota | Evidência principal |
| --- | --- | --- |
| Autonomia real | 2 | 55-65% dos tickets sem toque; 24 de 39 PRs de implementação recentes saíram do chat; paralelismo efetivo 1 (USR-369); despachante só roda com o owner logado |
| Determinismo de processo | 1,5 | Planejamento, pesquisa, revisão e learning pack não existem no `run_ticket`; pesquisa parada desde 15/09; política Haiku sem chamador de produção; G1 só sintático |
| Confiabilidade e recuperação | 2,5 | Leases com fencing e heartbeats na linha são bons; VPS com limites de memória somando ~6 GB em 4 GB; 46% das demandas corrigem a própria fábrica |
| Eficiência de cota e custo | 2 | Opus do chat implementa (372 trailers Opus x 187 Sonnet); OpenRouter excluído de escrita; teto de gasto nunca aplicado; custo registrado como zero |
| Qualidade de código | 2 | 7 roteadores, 4 orquestradores, 3 control stores; 161 funções com CC > 20; ~12-15 mil linhas sem uso em produção; mypy com 678 erros fora do portão |
| Testes e portão | 3 | 5.120 testes, 166 s com `-n auto`, maioria de comportamento; suíte roda ~3 vezes por ticket; `affected.py` existe e não é usado |
| Segurança | 1,5 | Hub público entregava a sessão (corrigido no PR #653); worker com `/system/exec` e token opcional; agentes com `bypassPermissions` e acesso a segredos do owner |
| Experiência do owner | 1,5 | Learning pack sem estágio; relatórios do Telegram pouco úteis; ledgers de pesquisa e learning parados desde setembro |

## 3. O que está bom (manter)

- Ritmo e capacidade de entrega: ~20 commits por dia, portão determinístico com marcadores.
- Desenho da linha na nuvem: leases com fencing token, heartbeat, reconciliação de órfãos.
- Invocação headless dos três harnesses de escrita sem TTY (`core/line/agent_cli.py`).
- Revisão por família diferente implementada (`core/line/stage_review.py`), com teste.
- Scan de segredos antes do commit, Dockerfiles sem root, CI com `contents: read`.
- Backlog de ações do owner visível no Hub (o conceito funciona; o volume é que é excessivo).
- Testes de comportamento bons em `tests/line/`; setup de fixtures é só 5,6% do tempo da suíte.

## 4. O que está ruim, por dor declarada

**Babysitting.** Três motores sem dono único: linha DAG (Postgres, VPS, 1 slot), despachante de frota
(`core/line/fleet_dispatcher.py`, tarefa AtLogOn no notebook, chama `run_ticket.py --fleet`) e chat.
61% dos últimos 100 PRs são burocracia (39 de registro de fila, 22 de ações do owner). Ticket vindo
do Telegram é gravado dentro do container (efêmero) e o feeder lê a fila congelada na imagem.

**Combinados que se perdem.** O que está só em skill ou markdown depende do agente. Nenhuma skill é
carregada pela linha de produção. O `CLAUDE.md` não importa o `AGENTS.md`; o Antigravity lê um
segundo `AGENTS.md` em `.agents/rules/`. Hooks do Claude Code só registram telemetria. O learning
pack aparece no Antigravity por iniciativa do modelo e nunca é disparado no Claude Code.

**Grill invertido.** `core/demands/readiness.py:61-87` aprova "Quero um painel que mostre minhas
vendas do mes" como pronta. O grill legado (`core/demands/grill.py:136-240`) tem perguntas técnicas
fixas; o da linha depende da autoclassificação do LLM; `.agents/rules/GEMINI.md:32-34` manda não
assumir parâmetros de arquitetura, o que induz perguntas técnicas ao owner.

**Ações manuais.** Das 70 ações do owner (desde 09/10), ~15 eram de fato do owner. 23 foram o
ritual de publicar arquivo protegido (`--admin` ou push direto), porque `core/orchestrator/guard.py`
protege `core/harness/*`, que é código operacional. 18 foram máquinas e VPS. Em 5 de 12 decisões o
owner só aceitou a opção recomendada.

**VPS travando.** `deploy/dokploy/docker-compose.cloud.yml` ainda faz build de 4 serviços na VPS;
limites de memória somam ~6 GB numa VPS de 4 GB; o n8n (2 workflows inativos) reserva 1,28 GB.

**Roteamento.** Só `core/line/routing.pick` está em produção, mas há 6 roteadores paralelos. Com
`CLAUDECODE=1` o Claude é preferido em todo ticket. O Grok (plano de US$30) está fora da cascata de
desenvolvimento. OpenRouter só entra como último recurso de leitura. MiniMax não está no registro.
O "Jev" é o classificador TypeSafe Jev (`core/pilots/jev_adapter.py`), sem chamador; o próprio
experimento concluiu que não compensa. Ollama está fora do caminho de tickets.

**Testes.** A hipótese de setup repetido não se confirmou. O gargalo é a mesma suíte rodar local, no
PR e em main (Windows em 4 shards duas vezes), a fila do `suite_lock` e 3 testes que esperam 30 s
reais cada (`DEAD_PROBE_GAP_SEC` não simulado), somando 16% do tempo. Há ~350-450 itens de testes de
forma e ~600 testes de código sem uso em produção.

## 5. Matriz de benchmark (prática atual x DarkFac)

Fontes: Anthropic Engineering (building effective agents, 2024-12; harnesses for long-running agents,
2025-11; demystifying evals, 2026-01; harness design, 2026-03), OpenAI harness engineering (2026-02),
Simon Willison (agentic engineering patterns, 2026-02), Hamel Husain (evals, 2026-03), METR, DORA
2025, Thoughtworks Radar vol. 33, `coleam00/skills` e Archon (origem da DarkFac), Stripe Minions.

| Prática | Consenso | DarkFac hoje |
| --- | --- | --- |
| Orquestrador determinístico, agente como passo | Sim | Parcial: existe na linha, mas o caminho dominante é o `run_ticket` |
| Contrato de saída verificado por estágio | Sim | Falta: estágios do `run_ticket` não têm artefato obrigatório |
| Estado fora do contexto do modelo | Sim | Tem (Postgres, worktrees, ledgers) |
| Workspaces isolados e paralelos | Sim | Parcial: worktrees sim, paralelismo efetivo 1 |
| Fila durável com dono único | Sim | Falta: fila no git + Postgres + despachante concorrendo |
| Retries limitados | Sim | Tem (`agent_retry`, fix-agent no portão) |
| Portão executável que o modelo não convence | Sim | Tem (`runner.py`, marcadores, CI) |
| Suíte de evals com tickets reais (pass^k, custo/ticket) | Sim | Falta |
| Revisão por modelo independente como sinal | Sim | Parcial: só na linha |
| Humano só em intenção e aceite | Sim | Falta: G1 sintático, grill técnico, ritual de protegidos |
| AGENTS.md curto como mapa (~100 linhas) | Sim | Falta: regras repetidas em 3-4 arquivos longos |
| Hooks como segunda camada de enforcement | Sim | Falta (hooks só de telemetria) |
| Menor privilégio e segredos fora do alcance do agente | Sim | Falta: `bypassPermissions`, `.env` em texto, `gh` admin |
| Build fora do servidor de produção | Sim | Parcial: Hub usa GHCR, serviços cloud ainda buildam na VPS |
| Custo por ticket aceito, incluindo falhas | Sim | Falta: eventos sem `ticket_id`, custo zerado |
| Roteamento por tipo de tarefa com 1 escalada | Sim | Parcial: só para leitura; escrita presa às assinaturas |
| Pesquisa registrada e reutilizável | Prática do owner | Falta: sem estágio, ledger parado em 15/09 |
| Explicação ao humano derivada do diff | Sim (MSR'26: 45% das descrições de PR de agentes afirmam o que não fizeram) | Falta |
| Coleta de lixo agendada (refatoração, código morto) | Sim | Falta |
| Fronteiras de arquitetura checadas (import-linter, complexidade) | Sim | Falta |
| Simplificar a cada geração de modelo | Sim | Falta: código só cresce |
| Enxames de 20+ agentes, zero revisão, personas | Moda | Não adotado (correto) |

## 6. Riscos externos

- Gemini CLI deixou de atender contas Google AI Pro em 18/06/2026; o substituto é o Antigravity CLI
  (`agy`). Adaptadores que chamam `gemini` precisam ser conferidos.
- Termos da Anthropic: o binário oficial `claude -p` para uso pessoal é tolerado; extrair o token
  OAuth para cliente próprio não é. O probe de cota do Claude (`core/usage/adapters.py:1490-1515`)
  faz POST com User-Agent falsificado e deve ser trocado pela leitura de `rate_limits`.
- Grok por assinatura sem supervisão não tem confirmação oficial. Nenhum harness deve ser ponto único.

## 7. Defeitos encontrados (viram tickets no fim da sessão)

Segurança: worker com `/system/exec` e token opcional (`core/harness/remote_worker.py:246,1994`);
autorização de governança autoemitível (`core/governance/authorization.py`); filtro de ambiente dos
agentes deixa passar `GITHUB_PAT` e `N8N_API_KEY`; `GITHUB_PAT` na URL de build do Hub; defaults
de senha no compose do n8n; GETs com efeito colateral; assinatura do webhook do GitHub calculada
sobre JSON reserializado.

Runtime: feeder lê `demands.json` congelado na imagem; ticket do Telegram em arquivo efêmero;
`busy_ids_loader` vazio no despachante; falha de deploy ignorada no despachante; fallback de push
direto em main sem PR (`core/git/autonomy.py:1568`); job de CD do CI fica verde mesmo pulando o deploy por falta de `DOKPLOY_DEPLOY_URL` (confirmado no merge do PR #653); o botao Deploy/Start do Dokploy reimplanta o ultimo SHA renderizado, nao o de `main` (so `scripts/dokploy_redeploy.py` fixa o SHA certo).

Roteamento e custo: teto `openrouter_usd` nunca aplicado; eventos de uso sem `ticket_id`; custo
zerado na retrospectiva; limiar crítico 10% x 15%; mensagem enganosa de "sem rota" com cota
desconhecida; ids de gateway inconsistentes no registro.

Código e dados: `PostgresControlStore.materialize` definido duas vezes; `styles.css` duplicado
(16 mil linhas); 3 testes de 30 s; README com 17 skills; `docs/ARCHITECTURE.md` parado em 06/09;
89% dos SHAs de evidência fora de main; trailer zero-human-touch em tickets que tiveram ação do
owner; `AGENTS.md` declara V0 encerrada em 17/10 sem o critério atual de V0.
