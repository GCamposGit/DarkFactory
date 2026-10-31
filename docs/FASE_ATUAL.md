# Fase atual da DarkFac

Fonte única do contexto que vale **hoje** e muda conforme a fábrica avança. A missão de longo prazo está em [`MISSION.md`](../MISSION.md); as regras para agentes em [`AGENTS.md`](../AGENTS.md); o plano de execução vigente em [`docs/plans/PLANO_V0_2026-10-24.md`](plans/PLANO_V0_2026-10-24.md).

Como manter: este arquivo não é protegido. O owner edita quando quiser, e a fábrica o atualiza na mesma entrega que muda um destes fatos (com a data no histórico ao final). Agentes consultam este arquivo antes de decidir rota, gasto, host ou quando acionar o owner.

Última revisão: 31/10/2026 (fábrica; pick fresco de development em contexto headless na frota, USR-428).

## Canais de entrada
- Telegram (bot do owner), DarkHub (darkhub.ggcampos.com) e chat com os harnesses (Claude Code, Codex, Grok Build e Antigravity).
- Os três devem funcionar sem erro e levar a demanda pelo mesmo motor (`run_ticket.py` / despachante).
- Antigravity atua como interface interativa: inicia sessão pelo `python -m core.ops.briefing`, define `DARKFAC_OPERATING_HARNESS=antigravity` e implementa no chat se o roteador o eleger. Se outro harness for eleito, o ticket segue pelo `run_ticket.py`. Antigravity é proibido em modo headless/frota, incluindo workers, `--fleet` e pump.

## Ambiente e máquinas
- Windows é o ambiente principal; Linux roda no CI e na VM do Grok.
- Hosts da frota, por prioridade de uso (`.factory/config/line_fleet.json`): **grokbox** (VM do Grok, sujeita a quedas constantes já conhecidas: queda é evento normal, o ticket volta para a fila e retoma em outro host), **Notebook** (máquina do owner; RAM limitada decide quantos workers cabem) e **Desktop**.
- Para tirar um host da frota sem editar o config (ex.: Desktop ocupado com outra carga), o despachante lê `DARKFAC_FLEET_DISABLED_HOSTS` (ids separados por vírgula). Em 26/10 o Desktop está fora (indexação do Segundo Cérebro); Notebook dedicado à fábrica. O harness de teste remoto (`core/harness/remote_dispatch.py`) e a rotação de workers (`run_ticket.py`) também respeitam `DARKFAC_FLEET_DISABLED_HOSTS`, filtrando proativamente e nunca enviando tráfego ou sondas para hosts desabilitados (USR-430).
- VPS pequena (Dokploy) com o Hub em blue-green; nenhuma imagem é construída na VPS (tudo vem do GHCR).

## Orçamento
- Assinaturas básicas: Claude Pro, ChatGPT Plus (Codex), SuperGrok e Gemini Pro, consideradas 20 USD/mês cada no custo por ticket (`.factory/config/subscription_prices.json`).
- OpenRouter: perto de US$ 10/mês; pode subir se aumentar a vazão. Hoje o código aplica só um teto por execução de US$ 2 (`run_caps.openrouter_usd` em `.factory/config/line_routing.json`); o teto mensal em código é parte do bloco USR-378.
- Dentro desses tetos a fábrica decide sozinha; acima deles, pergunta ao owner (MISSION, princípio 2).
- Piso de cota: nenhuma assinatura é usada para escrever código com 15% ou menos de cota restante (`core/line/routing.py`). Depois do PLAN, cada fatia de development pede um `pick` fresco antes do agente; a leitura pode eleger outro harness write que a fatia anterior. A rota do preflight e o `--force` não iniciam harness nesse piso nem com cota desconhecida. Um `--harness` que permanece acima do piso usa o modelo declarado para esse harness. Com `--fleet` esse pick é headless e não elege Antigravity. Sem rota a fatia espera (código 75, JSON `route_wait` / `BUILD` / caminho da worktree), o ticket volta a `planned`, a entrega não começa e a worktree fica, inclusive o arquivo gravado no PLAN e o que as fatias anteriores já gravaram (USR-428).
- Contingência OpenRouter para alta inteligência: quando nenhum harness local de alta inteligência estiver disponível para estágios que exigem alta capacidade (ex.: planejamento/high-tier com Claude e Codex abaixo do piso crítico de 15%), é permitido e correto utilizar o OpenRouter com modelo de alta inteligência dentro do teto por execução (`run_caps.openrouter_usd`), garantindo que a fábrica nunca trave por falta de rota viável.
- Cota Antigravity: o briefing de 29/10/2026 leu snapshot local atual, com 98,91% restante semanal e 96,0% na janela móvel de 5h; essa amostra estava atual (menos de 1 minuto). Snapshot ausente, inválido ou `STALE` não é percentual confirmado: a preferência do operador Antigravity segue elegível apenas no chat interativo, com aviso e causa estruturada; qualquer execução headless exclui Antigravity.
- Briefing operacional: `python -m core.ops.briefing` reúne fila, OAs abertas de todas as fontes, nós sem atualização, PRs com fallback quando `gh` falha, snapshots de cota e `docs/operacao/HANDOFF.md`. Em 29/10, o control store PostgreSQL estava inacessível e a leitura da fila usou o fallback em memória; `gh` não estava disponível.
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
- Contrato de encerramento do foco: `python -m pytest tests/test_fleet_dispatcher.py -q` termina sozinho, com código de saída e uma linha de resumo do pytest no formato `N passed in Xs`, dentro do prazo configurado em `pytest.ini` (`timeout = 120`, `timeout_method = thread`). O cache por worktree fica em `DarkFac/pytest-cache` no diretório temporário do usuário. `redirect_cache_dir` grava esse destino antes de o cache plugin ler `cache_dir` e reaplica o caminho no `Cache` já criado, no início de `pytest_sessionfinish`, antes de `Cache.set`. Um `%LOCALAPPDATA%` não gravável não deixa o processo aberto depois de `100%`.

## Histórico
- 30/10/2026: o estado temporário do harness em /.tmp/ sai do índice (git rm --cached, a cópia local permanece) e fica ignorado pelo .gitignore ancorado e pelo exclude local, para não voltar ao staging.
- 30/10/2026: commit_ticket, commit, commit_paths e o snapshot do portão (uild_snapshot_commit) deixam .tmp/ fora da árvore publicada mesmo sem ignore e mesmo já staged. Os arquivos permanecem no disco. O snapshot mexe só no índice descartável; 
ew.txt entra e o ignorado continua de fora.
- 31/10/2026: sem rota de development o ticket volta a `planned` e a worktree fica; um `--harness` acima do piso usa o modelo declarado para esse harness (USR-428).
- 31/10/2026: sem rota na primeira fatia de development o launcher espera (código 75), não chama o agente nem a entrega, e deixa no disco a worktree com o arquivo gravado no PLAN (USR-428).
- 31/10/2026: o pick fresco de cada fatia com `--fleet` usa contexto headless e não inicia Antigravity, mesmo como harness de operação (USR-428).
- 30/10/2026: o primeiro development depois do PLAN revalida a cota com um pick fresco; headroom de 15% ou cota desconhecida não herda a rota do preflight nem o override (USR-428).
- 30/10/2026: cada fatia do plano repete esse pick antes do agente; a leitura pode trocar o harness entre fatias. Uma fatia posterior sem rota não inicia o agente dela, a entrega não começa e a worktree com o trabalho anterior permanece (USR-428).
- 30/10/2026: o foco `python -m pytest tests/test_fleet_dispatcher.py -q` segue o contrato de encerramento acima (`timeout = 120`, `timeout_method = thread`, resumo `N passed in Xs`).
- 29/10/2026: Antigravity formalizado como interface interativa com briefing compartilhado e handoff versionado; cota atual confirmada pelo snapshot local, com causa estruturada para falhas futuras; execução headless/frota continua excluída.
- 29/10/2026: higiene da suíte ignora só o timestamp `heartbeat_at` das travas operacionais conhecidas, mantendo mudanças nos demais campos e remoções detectáveis.
- 29/10/2026: cache de pytest por worktree usa o temp do usuário e é reaplicado no início de `pytest_sessionfinish`, antes de `Cache.set`, para o focal concluir quando `%LOCALAPPDATA%` não está gravável.
- 28/10/2026: suíte Windows limpa marcadores de execução herdados antes da coleta e dá a cada teste um `DARKFAC_STATE_ROOT` privado; classificação de worktree suja exige a linha completa do runner.
- 28/10/2026: retomada após REVIEW com JSON inválido preserva BUILD/VALIDATE apenas para worktree limpa e digests de candidato/política coincidentes; o gate continua vinculado ao hash da árvore.
- 28/10/2026: limpeza dos checkouts operacionais da frota passa pelo `ticket_workspace.cleanup`; processo vivo com cwd no checkout preserva o registro e os arquivos, com caminho, holder e recuperação posterior registrados no log.
- 27/10/2026: OA-066 encerrada (Google dispensou o developer token; acesso via Google Cloud); OA-067 cancelada (sem Instagram/Facebook), redes sociais fora do piloto por ora.
- 26/10/2026: Desktop fora da frota por DARKFAC_FLEET_DISABLED_HOSTS=desktop (pedido do owner).
- 25/10/2026: criado a partir da revisão do MISSION pelo owner (separação longo prazo x fase atual).
