# Fase atual da DarkFac

Fonte única do contexto que vale **hoje** e muda conforme a fábrica avança. A missão de longo prazo está em [`MISSION.md`](../MISSION.md); as regras para agentes em [`AGENTS.md`](../AGENTS.md); o plano de execução vigente em [`docs/plans/PLANO_V0_2026-10-24.md`](plans/PLANO_V0_2026-10-24.md).

Como manter: este arquivo não é protegido. O owner edita quando quiser, e a fábrica o atualiza na mesma entrega que muda um destes fatos (com a data no histórico ao final). Agentes consultam este arquivo antes de decidir rota, gasto, host ou quando acionar o owner.

Última revisão: 25/10/2026 (owner).

## Canais de entrada
- Telegram (bot do owner), DarkHub (darkhub.ggcampos.com) e chat com os harnesses (Claude Code, Codex, Grok Build).
- Os três devem funcionar sem erro e levar a demanda pelo mesmo motor (`run_ticket.py` / despachante).

## Ambiente e máquinas
- Windows é o ambiente principal; Linux roda no CI e na VM do Grok.
- Hosts da frota, por prioridade de uso (`.factory/config/line_fleet.json`): **grokbox** (VM do Grok, sujeita a quedas constantes já conhecidas: queda é evento normal, o ticket volta para a fila e retoma em outro host), **Notebook** (máquina do owner; RAM limitada decide quantos workers cabem) e **Desktop**.
- Para tirar um host da frota sem editar o config (ex.: Desktop ocupado com outra carga), o despachante lê `DARKFAC_FLEET_DISABLED_HOSTS` (ids separados por vírgula). Em 26/10 o Desktop está fora (indexação do Segundo Cérebro); Notebook dedicado à fábrica.
- VPS pequena (Dokploy) com o Hub em blue-green; nenhuma imagem é construída na VPS (tudo vem do GHCR).

## Orçamento
- Assinaturas básicas: Claude Pro, ChatGPT Plus (Codex), SuperGrok e Gemini Pro, consideradas 20 USD/mês cada no custo por ticket (`.factory/config/subscription_prices.json`).
- OpenRouter: perto de US$ 10/mês; pode subir se aumentar a vazão. Hoje o código aplica só um teto por execução de US$ 2 (`run_caps.openrouter_usd` em `.factory/config/line_routing.json`); o teto mensal em código é parte do bloco USR-378.
- Dentro desses tetos a fábrica decide sozinha; acima deles, pergunta ao owner (MISSION, princípio 2).
- Piso de cota: nenhuma assinatura é usada para escrever código com 15% ou menos de cota restante (`core/line/routing.py`).
- Claude Code: coletor versionado do statusLine e leitura de snapshots sanitizados estão disponíveis no código; a validação ponta a ponta no Notebook ainda está pendente. Em 26/10/2026, o preflight local com Claude Code 2.1.278 retornou Anthropic conectada via credenciais, sem quota (`quota_supported=false`) e sem janelas 5h/7d. Portanto, a fonte real do Desktop ainda não foi confirmada e o roteador deve tratar a quota como desconhecida até uma sessão autenticada acionar o statusLine. Evidência sanitizada: [`docs/evidence/USR-391-notebook-statusline.md`](evidence/USR-391-notebook-statusline.md).

## Tempo e participação do owner
- 30 minutos de manhã (plano do dia) e 30 à noite (o que foi entregue), mais perguntas pontuais pelo celular. O resumo diário chega pelo bot às 08:00 e às 21:00.
- O owner entra no início de entregas grandes (objetivo e critério de sucesso) e no aceite delas. Correções e funcionalidades pequenas vão direto para produção.
- Ações que só o owner pode fazer viram OA no DarkHub, com passo a passo; meta de menos de 3 por semana que não sejam decisão de produto, segredo novo ou aceite.

## Critério da V0 (marco atual)
- 5 demandas reais seguidas entram por Telegram, Hub ou chat e chegam em produção sem o owner tocar, cada uma com learning pack.
- Pelo menos 1 é de um produto real, não da própria fábrica.
- Os três canais funcionam sem erro.

## Piloto de produto (potencial, sujeito a mudança)
- Gestor de SEO e redes sociais da mansaodolago.com, em fases:
  - M1: auditoria de SEO e Search Console, relatório semanal e calendário de conteúdo (sem Instagram/Facebook: a empresa ainda não tem as contas).
  - M2: publicação recorrente e SEO on-page, aprovação por lote.
  - M3: ajustes no Google Ads com teto de gasto em código e aprovação obrigatória.
- Pré-requisitos do owner: Google Ads: developer token não é mais exigido (aviso do Google em 27/10, OA-066); o acesso à API agora vem de um projeto no Google Cloud com a API Google Ads ativada e OAuth, a pedir quando o piloto for escolhido. Instagram/Facebook cancelados em 27/10 (OA-067): a empresa ainda não tem as contas.

## Metas técnicas da fase (da fábrica, não dos produtos)
- Suíte com até 4.000 testes e até 120 s no Linux com `-n auto`.
- PR típico em menos de 5 minutos no CI.
- Núcleo (`core/`) com pelo menos 10% menos linhas que em 24/10/2026.
- Menos de 3 ações do owner por semana fora de decisão, segredo novo e aceite.
- Tickets com todos os artefatos de estágio (plano, pesquisa, veredito, deploy, learning pack): 100%.

## Portões técnicos de hoje
- `main` verde no CI (Linux em todo PR; Windows em `main` e à noite).
- PR: testes afetados, contratos de camada (`lint-imports`), ratchet de complexidade e docs-sync do manual.
- Canário diário de ponta a ponta passando e backup pós-deploy com prova de restauração.

## Histórico
- 25/10/2026: criado a partir da revisão do MISSION pelo owner (separação longo prazo x fase atual).
- 26/10/2026: Desktop fora da frota por `DARKFAC_FLEET_DISABLED_HOSTS=desktop` (pedido do owner).
- 27/10/2026: OA-066 encerrada (Google dispensou o developer token; acesso via Google Cloud); OA-067 cancelada (sem Instagram/Facebook), redes sociais fora do piloto por ora.
- 28/10/2026: suíte Windows limpa marcadores de execução herdados antes da coleta e dá a cada teste um `DARKFAC_STATE_ROOT` privado; classificação de worktree suja exige a linha completa do runner.
- 28/10/2026: retomada após REVIEW com JSON inválido preserva BUILD/VALIDATE apenas para worktree limpa e digests de candidato/política coincidentes; o gate continua vinculado ao hash da árvore.
