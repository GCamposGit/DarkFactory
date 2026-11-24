# Fase atual da DarkFac

Fonte única do contexto que vale **hoje** e muda conforme a fábrica avança. A missão de longo prazo está em [`MISSION.md`](../MISSION.md); as regras para agentes em [`AGENTS.md`](../AGENTS.md); o plano de execução vigente em [`docs/plans/PLANO_V0_2026-10-24.md`](plans/PLANO_V0_2026-10-24.md).

Como manter: este arquivo é o snapshot da fase atual (canais, máquinas, orçamento, metas, piloto). Não é protegido. Não recebe histórico, changelog nem entrada por ticket: cada linha datada acrescentada aqui disputa o mesmo trecho no rebase. Quando um fato desta fase muda, edite a seção consolidada. O registro de cada entrega fica no manual (`docs/manual/`) e no learning pack (`.factory/learning_packs/`). Coordenador, cotas, runs vivos e PRs correntes vêm do estado gerado (`python C:\dev\DarkFac\scripts\line_briefing.py`) e não são copiados para cá. Agentes consultam este arquivo antes de decidir rota, gasto, host ou quando acionar o owner, e leem o estado gerado antes de tratar coordenação ou fila corrente.

## Canais de entrada

Sessões de coordenação podem iniciar pelo pedido **“assuma a fábrica”** com a skill `coordenar-fabrica`. O [manual de operação](manual/operacao_interativa.md) inclui a entrada explícita para harnesses que leem arquivos do projeto.

- Telegram (bot do owner), DarkHub (darkhub.ggcampos.com) e chat com os harnesses (Claude Code, Codex, Grok Build e Antigravity).
- Holds de `HumanRequest` e `notify_ticket_hold` selecionam o papel `owner` e exigem `TELEGRAM_OWNER_CHAT_ID`, sem fallback para destinos compartilhados. Sem destino, o primeiro mantém a ação `OA-TELEGRAM-OWNER-DESTINATION`; o segundo persiste no alerta do DarkHub a instrução de configurar a variável e registra `missing_destination`, sem reportar entrega Telegram ou criar outbox.
- Os três devem funcionar sem erro e levar a demanda pelo mesmo motor (`run_ticket.py` / despachante).
- Qualquer harness suportado abre sessão pelo sinal do próprio processo. Identidade da sessão, sessão ativa e coordenador são registros diferentes. Consulta e troca: `python C:\dev\DarkFac\scripts\line_briefing.py` e, quando o owner decide, `assumir --decisao-owner`. Antigravity é proibido em modo headless/frota, incluindo workers, `--fleet` e pump. No chat interativo pode ser eleito para `development` quando é o harness de operação da sessão e a cota conhecida passa de 15%. Quando o harness do Antigravity for usado, adota-se o **Gemini 4 Neon** (idealmente **Orchestrator (Neon)**) como modelo principal para coordenação da fábrica e tarefas de alta inteligência, e o **Gemini 4 Hydrogen High** ou **Gemini 4 Argon High** para tarefas específicas em subagentes.

## Política de dados entre hosts
- `.factory/config/data_policies.json` é a referência compartilhada e versionada do projeto. Uma política legada divergente em `.factory/enterprise/configs.json` é tratada como override local e gera diagnóstico estruturado `data_policy_host_divergence`; quando ela remove todas as rotas, a admissão retorna `route_wait` com `policy_cause` contendo a fonte efetiva e o diagnóstico com ambos os caminhos. Testes usam raízes temporárias e verificam por cenário que toda a árvore `.factory/enterprise` do checkout permanece inalterada.

## Ambiente e máquinas
- Recuperação em vigor: testes remotos vão para o Desktop saudável (`100.78.181.90:8080`); Grokbox indisponível fica excluído. `DARKFAC_FLEET_RESUME_ONLY=1` mantém retomadas sem novos BUILDs até a saída estabilizar. A exclusão do Desktop descrita abaixo, por outra carga, não é esta recuperação.
- Windows é o ambiente principal; Linux roda no CI e na VM do Grok.
- Hosts da frota, por prioridade de uso (`.factory/config/line_fleet.json`): **Notebook** (prioridade 1, máquina do owner, dedicado à fábrica; RAM limitada decide quantos workers cabem), **Grokbox** (prioridade 2, VM do Grok tentada quando está no ar; queda é evento normal, o ticket volta para a fila e retoma em outro host) e **Desktop** (prioridade 3, por último).
- A varredura diária de worktrees do despachante executa em processo separado, permitindo publicar ciclos e recarregar o código durante a manutenção. O [manual da frota](manual/frota.md#varredura-de-worktrees-sem-owner-usr-500) descreve a trava, o log e a retomada.
- Para tirar um host da frota sem editar o config (ex.: Desktop ocupado com outra carga), o despachante lê `DARKFAC_FLEET_DISABLED_HOSTS` (ids separados por vírgula). Em 26/10 o Desktop está fora (indexação do Segundo Cérebro); Notebook dedicado à fábrica. O harness de teste remoto (`core/harness/remote_dispatch.py`) e a rotação de workers (`run_ticket.py`) também respeitam `DARKFAC_FLEET_DISABLED_HOSTS`, filtrando proativamente e nunca enviando tráfego ou sondas para hosts desabilitados (USR-430). O dispatcher lê as reservas canônicas uma vez por ciclo, revalida imediatamente antes de cada lançamento e publica `free_hosts` vazio se alguma consulta falhar (USR-454).
- Contexto operacional e auditoria de modelos (`USR-471`): serviços headless usam identidade própria de processo sem herdar `DARKFAC_OPERATING_HARNESS` nem sessão de chat interativo (`core/line/fleet_dispatcher.py`, `core/line/operational_context.py`); política persistida de hosts (`.factory/config/line_fleet.json`) e snapshot/recibos em `STATE_ROOT/runs` (`core/line/fleet_policy.py`, `core/models/audit.py`, `core/line/stage_review.py`) separam exclusão de política de disponibilidade observada e auditam execução real contra a rota configurada.
- VPS pequena (Dokploy) com o Hub em blue-green; nenhuma imagem é construída na VPS (tudo vem do GHCR).

## Orçamento
- Contingência OpenRouter confirmada pelo owner quando não há assinatura elegível, com recarga de US$10. Os tetos vigentes permanecem; crédito disponível não aumenta o teto mensal. Esperas de rota ficam persistidas e o dispatcher as retoma em `--resume-only`, sem heartbeat de LLM. A retomada não fica só em comando ou log manual.
- Assinaturas básicas: Claude Pro, ChatGPT Plus (Codex), SuperGrok e Gemini Pro, consideradas 20 USD/mês cada no custo por ticket (`.factory/config/subscription_prices.json`).
- OpenRouter: teto mensal de US$ 28,13 neste mês por verba adicional autorizada pelo owner (OA-090); volta a US$ 10,00 em 01/12/2026. O código bloqueia a rota quando o gasto do mês alcança `run_caps.openrouter_monthly_cap_usd` e aplica o teto por execução de US$ 2 (`run_caps.openrouter_usd`), lendo o gasto do ticket no ledger de uso (`.factory/config/line_routing.json`).
- Dentro desses tetos a fábrica decide sozinha; acima deles, pergunta ao owner (MISSION, princípio 2).
- Piso de cota: nenhuma assinatura é usada para escrever código com 15% ou menos de cota restante (`core/line/routing.py`). Antes da worktree e do PLAN, a admissão (USR-468 T1, `core/line/admission.py`) confirma rota de planning, development e revisão de outra família, e um worker de gate com `workers` > 0, `busy` false e leitura fresca. Coleção vazia, health só `status=ok`, capacidade zero, worker ocupado ou leitura com 90 segundos ou mais esperam (código 75, JSON `route_wait`, ticket `planned`) sem criar worktree. Um allow não substitui o `pick` de cada estágio. Depois do PLAN, cada fatia de development pede um `pick` fresco antes do agente; a leitura pode eleger outro harness write que a fatia anterior. A rota do preflight e o `--force` não iniciam harness nesse piso nem com cota desconhecida. Um `--harness` que permanece acima do piso usa o modelo declarado para esse harness. Com `--fleet` esse pick é headless e não elege Antigravity. Sem rota a fatia espera (código 75, JSON `route_wait` / `BUILD` / caminho da worktree), o ticket volta a `planned`, a entrega não começa e a worktree fica, inclusive o arquivo gravado no PLAN e o que as fatias anteriores já gravaram (USR-428).
- Contingência OpenRouter para alta inteligência: quando nenhum harness local de alta inteligência estiver disponível para estágios que exigem alta capacidade (ex.: planejamento/high-tier com Claude e Codex abaixo do piso crítico de 15%), é permitido e correto utilizar o OpenRouter com modelo de alta inteligência dentro do teto por execução (`run_caps.openrouter_usd`), garantindo que a fábrica nunca trave por falta de rota viável.
- Cota do Antigravity: snapshot ausente, inválido ou `STALE` não é percentual confirmado. No chat interativo, com a sessão reconhecida como Antigravity, a preferência segue elegível com aviso e causa estruturada. Execução headless exclui Antigravity. O percentual corrente sai do estado gerado.
- O panorama somente leitura `python -m core.ops.briefing` continua reunindo fila, ações abertas, nós e a seção do sentinela, com fallback por seção. Ele não escolhe coordenador.
- Coordenação interativa (USR-539, USR-556): pedido curto e leitura explícita da skill passam por `python -m core.ops.coordination`, que persiste o recibo antes do briefing e das lições. Runs e entregas em recuperação bloqueiam nova admissão; o recibo recupera run_id, etapa e evidências persistidas. A rota usa a etapa do ticket. A coordenação acusa `deploy_convergence_exhausted` quando `DARKFAC_STATE_ROOT/runs/deploy_deferrals.json` registra `recovery_exhausted`, incluindo sha, nó pendente, motivo e tentativas; durante um deploy em voo com `delivery_wait`, conserva o run como `in_flight` e prioriza ler `[NODE SYNC]`, limpar a causa e executar o redeploy canônico antes de admitir trabalho novo.
- Claude Code: coletor versionado do statusLine e leitura de snapshots sanitizados estão disponíveis no código; a validação ponta a ponta no Notebook ainda está pendente. Em 26/10/2026, o preflight local com Claude Code 2.1.278 retornou Anthropic conectada via credenciais, sem quota (`quota_supported=false`) e sem janelas 5h/7d. Portanto, a fonte real do Desktop ainda não foi confirmada e o roteador deve tratar a quota como desconhecida até uma sessão autenticada acionar o statusLine. Evidência sanitizada: [`docs/evidence/USR-391-notebook-statusline.md`](evidence/USR-391-notebook-statusline.md).

## Coordenação da fábrica (USR-483)

Trocar de harness, de modelo ou de PC e trabalhar em paralelo são comportamento esperado. Várias sessões, no mesmo harness ou em outro, no mesmo modelo ou em outro, no mesmo PC ou em outro, planejam, tratam ticket com pendência humana, analisam, diagnosticam e tocam projetos feitos pela fábrica ao mesmo tempo.

- **Identidade da sessão.** O harness que executa o processo (`core/line/session_identity.py`). Codex confirma por `CODEX_THREAD_ID`. Antigravity confirma por `ANTIGRAVITY_SESSION_ID` e por `TERM_PROGRAM` antigravity. Claude usa `CLAUDECODE` ou `CLAUDE_CODE_SESSION_ID`. Grok usa `GROK_SESSION_ID` ou `GROK_AGENT`. Um valor antigo de `DARKFAC_OPERATING_HARNESS` não define a identidade da sessão nem o coordenador. Sem sinal confiável o registro grava `unknown`; o briefing não reaproveita valor antigo para escolher harness. Override explícito vale só para a chamada atual.
- **Sessão ativa.** Linha em `line_sessions`, com heartbeat de 900 segundos (15 minutos, `LEASE_TTL_SECONDS`). Registrar a sessão ativa não cria coordenador.
- **Coordenador.** No máximo uma lease, com sessão, harness, modelo, host, horário, origem `owner_decision` e heartbeat. O arquivo compartilhado vale entre PCs (`DARKFAC_SHARED_STATE_PATH` ou `<state root>/state/shared.sqlite3`).

Consulta e troca usam `scripts/line_briefing.py`:

```powershell
python C:\dev\DarkFac\scripts\line_briefing.py
python C:\dev\DarkFac\scripts\line_briefing.py --json
python C:\dev\DarkFac\scripts\line_briefing.py assumir --decisao-owner
```

O procedimento não grava `DARKFAC_OPERATING_HARNESS` no ambiente do usuário. `assumir --decisao-owner` troca a lease na hora, de qualquer harness, PC ou sessão, sem pedido de confirmação. Sem a marca, o processo sai com código 2 e o registro não muda. Consultar deixa a lease como está. Quem já coordena renova o heartbeat vigente. Lease vencida não se renova. A fábrica não elege outro coordenador. A causa fica `no_coordinator` ou `heartbeat_expired`, e a frota segue.

Ficam reservadas ao coordenador: operar o despachante e a fila global, alterar a política da frota e publicar o handoff de coordenação. Sessão paralela segue o fluxo normal de ticket, worktree própria, `suite_lock` e reservas, sem aviso de erro, sem bloqueio e sem pedido de confirmação.

Fatos correntes (coordenador, sessões paralelas, fila, cotas, runs vivos e PRs) saem do estado gerado por esse comando. Este arquivo não os congela. O piso de 15%, a revisão em outra família e o limite do Antigravity fora de headless/frota permanecem.

## Tempo e participação do owner
- 30 minutos de manhã (plano do dia) e 30 à noite (o que foi entregue), mais perguntas pontuais pelo celular. O resumo diário chega pelo bot às 08:00 e às 21:00.
- O owner entra no início de entregas grandes (objetivo e critério de sucesso) e no aceite delas. Correções e funcionalidades pequenas vão direto para produção.
- Ações que só o owner pode fazer viram OA no DarkHub, com passo a passo; meta de menos de 3 por semana que não sejam decisão de produto, segredo novo ou aceite.

## Critério da V0 (marco atual)
- 5 demandas reais seguidas entram por Telegram, Hub ou chat e chegam em produção sem o owner tocar, cada uma com learning pack.
- Pelo menos 1 é de um produto real, não da própria fábrica.
- Os três canais funcionam sem erro.

## Piloto de produto (M1 escolhido em 24/11 para a prova V0)
- Mansão do Lago é **cliente real** (não o owner). Projeto `mansaodolago` no registro, prefixo MDL, repo existente `GCamposGit/Site_Mansaodolago` (clone em `C:\dev\Site_Mansaodolago`), `requires_commercial_acceptance: true`: push em `master` publica o site por FTP, então merge, push em master e deploy só com aceite expresso do owner. O código do M1 vive na branch `darkfac/m1-seo`, que é a `default_branch` do projeto no registro: os PRs da frota miram essa branch, nunca `master`. A validação declarada é `python -m compileall -q .`; a adoção completa ficou de fora porque copiaria o runtime da fábrica para um repositório público. Relatórios vão por e-mail ao administrador (destinatários no cofre, `MANSAODOLAGO_REPORT_RECIPIENTS`); seção admin no próprio site fica para depois.
- Gestor de SEO e redes sociais da mansaodolago.com, em fases:
  - M1: auditoria de SEO e Search Console, relatório semanal e calendário de conteúdo (sem Instagram/Facebook: a empresa ainda não tem as contas).
  - M2: publicação recorrente e SEO on-page, aprovação por lote.
  - M3: ajustes no Google Ads com teto de gasto em código e aprovação obrigatória.
- Pré-requisitos do owner: Google Ads: developer token não é mais exigido (aviso do Google em 27/10, OA-066); o acesso à API agora vem de um projeto no Google Cloud com a API Google Ads ativada e OAuth, a pedir quando o piloto for escolhido. Instagram/Facebook cancelados em 27/10 (OA-067): a empresa ainda não tem as contas.
- Segredos do piloto (USR-279 T4): três chaves no mesmo cofre, prefixo `MANSAODOLAGO_` em `.factory/config/secrets_required.json` (Search Console, Instagram Graph, Google Ads), com prontidão por `python -m core.line.pilot_secrets` (presente/ausente por chave, nunca o valor; ver `docs/manual/segredos.md`). `MANSAODOLAGO_SEARCH_CONSOLE_CREDENTIALS_JSON` está presente no cofre e validada com leitura real em `sc-domain:mansaodolago.com` (`siteFullUser`), destravando o M1; Instagram (M2) e Google Ads (M3) seguem ausentes para as fases posteriores.
- Ações do owner com prazo externo: **OA-082** (Search Console de mansaodolago.com) concluída; segue aberta **OA-083** (medium; criar projeto Google Cloud com a API Google Ads ativada e OAuth — adianta o M3, não bloqueia M1). Instagram (M2) segue sem OA nova: a OA-067 já registrou que a empresa não tem conta, e abrir outra agora repetiria uma pergunta já respondida pelo owner.

## Metas técnicas da fase (da fábrica, não dos produtos)
- Suíte com até 4.000 testes e até 120 s no Linux com `-n auto`.
- PR típico em menos de 5 minutos no CI.
- Núcleo (`core/`) com pelo menos 10% menos linhas que em 24/10/2026.
- Menos de 3 ações do owner por semana fora de decisão, segredo novo e aceite.
- Tickets com todos os artefatos de estágio (plano, pesquisa, veredito, deploy, learning pack): 100%.

## Portões técnicos de hoje
- `main` verde no CI (Linux em todo PR; Windows em `main` e à noite).
- PR: testes afetados, contratos de camada (`lint-imports`), ratchet de complexidade e docs-sync do manual.
- Um lock canônico preexistente é aberto para leitura e travado sem alterar seus metadados. Se a ACL impedir essa leitura, a criação exclusiva de um lock ausente ou a gravação do sidecar de metadados, o harness usa um estado degradado por identidade: primeiro junto ao diretório configurado e, se essa árvore também não permitir o lock, em `%TEMP%`; o registro do gate informa o diretório efetivamente usado.
- No escopo PR, a coleta dos testes afetados tem prazo `min(prazo_restante, 600, 120 + 10 * arquivos_selecionados)`; timeout de coleta é reportado como `[STEP_TIMEOUT] affected tests phase=collection` para que o dispatcher retenha a causa observada no log.
- Canário diário de ponta a ponta passando e backup pós-deploy com prova de restauração.
- Contrato de encerramento do foco: `python -m pytest tests/test_fleet_dispatcher.py -q` termina sozinho, com código de saída e uma linha de resumo do pytest no formato `N passed in Xs`, dentro do prazo configurado em `pytest.ini` (`timeout = 120`, `timeout_method = thread`). O cache por worktree fica em `DarkFac/pytest-cache` no diretório temporário do usuário. `redirect_cache_dir` grava esse destino antes de o cache plugin ler `cache_dir` e reaplica o caminho no `Cache` já criado, no início de `pytest_sessionfinish`, antes de `Cache.set`. Um `%LOCALAPPDATA%` não gravável não deixa o processo aberto depois de `100%`.
- Encerramento e varredura de worktrees usam `core.git.ticket_workspace.cleanup`: um arquivo gerado só pode ser restaurado quando o SHA-256 ainda corresponde ao conteúdo registrado pelo chamador; qualquer divergência bloqueia a remoção. Status `completed` e `cancelled` disparam uma tentativa após a gravação do ledger; bloqueios ficam em `.factory/runs/cleanup_pending/` e são excluídos da classificação de órfãos e da varredura diária. A varredura considera worktrees sem ticket ativo, sem registro `cleanup_pending` e com idade superior a 48 horas, depois de confirmar que não há processo ou lock ativo. Em Windows, `AccessDenied` por processo isolado não interrompe toda a inspeção.
- `USR-475`: `progress.json` grava checkpoint atômico por fatia vinculado ao digest do plano, do candidato e da worktree (`build_evidence_mismatch` falha fechado preservando worktree); `pending_delivery` reconcilia esperas terminais e registra eventos em `reconciliations/`; `core/line/rebase_recovery.py` aplica recuperação determinística e inferência limitada por fingerprint mantendo caminhos protegidos intactos; `actionable_failures.json` isola tickets bloqueados, deduplica alertas e exige progresso real do filho na janela `progress_window_sec` (120s).
