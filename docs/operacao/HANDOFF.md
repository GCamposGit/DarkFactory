# Handoff operacional

Versao: 9
Atualizado em: 2026-10-31

## Estado

- Fase: V0 da Dark Factory.
- Canais: Telegram, DarkHub e interfaces de chat; demandas seguem por `run_ticket.py` e pelo registro compartilhado no control store.
- Operacao: Antigravity como interface interativa de operacao (`DARKFAC_OPERATING_HARNESS=antigravity`); tarefas headless/frota sao executadas pelo harness eleito pelo roteador (Grokbox e Notebook). Antigravity nunca e usado por workers, `--fleet` ou pump.
- USR-431: Concluido e entregue no `main` (PR #811, merge commit `b25b5fc5`), imagem GHCR publicada, deploy Dokploy concluido com verificacao ao vivo no `/health`, marcado `completed` no demands store.
- USR-424: Concluido e entregue no `main` (PR #809, merge commit `9842a697`), imagem GHCR publicada, deploy Dokploy concluido com verificacao ao vivo no `/health`, marcado `completed` no demands store.
- USR-420: Concluido e entregue no `main` (PR #808, merge commit `d2092f0e`), convergencia pos-deploy com candidato fixo e isolamento de ambiente no conftest, marcado `completed`.
- USR-428: Concluido e entregue no `main` (PR #806, merge commit `c7c0fb70`).
- USR-429: Concluido e entregue no `main` (PR #807, merge commit `dccb44a6`).
- USR-436: Concluido e entregue no `main` (PR #805, merge commit `90390014`).
- USR-430: Concluido e entregue no `main` (PR #803, merge commit `ed6cf52d`).
- Proximos: Marcos estruturais do Plano V0 (USR-343 e USR-375 em implementing; USR-369, USR-376, USR-377, USR-378, USR-379 e USR-279 em planned).

## Decisoes

- Consultar estado operacional pelo briefing compartilhado `python -m core.ops.briefing`. Cotas exibidas sao snapshots locais. Valor ausente, invalido ou `STALE` e desconhecido; na rota interativa do operador Antigravity isso nao o exclui silenciosamente: segue elegivel com aviso e causa registrada. O piso de 15% continua valendo para cotas conhecidas. O briefing nao dispara atualizacao de nos nem probes de cotas.
- Se PostgreSQL ou `gh` estiver indisponivel, o briefing informa o fallback da fila ou a indisponibilidade dos PRs; nao presume esses dados completos.
- Grok entra na cascata da etapa `planning` como contingencia quando Claude e Codex estiverem esgotados ou criticos (<=15%).
- Quando Claude, Codex e Grok estiverem sem cota ou indisponiveis, o fallback de leitura vai para OpenRouter pelo alias `distill.openrouter` do registro de modelos. O id concreto fica so no registro.
- O snapshot diario do ledger (`scripts/snapshot_queue.py`) aborta se qualquer ID existente for perdido, mesmo se substituido por novo ID com contagem igual. `next_action_id` consulta todo o historico git para impedir reutilizacao de ID.
- Textos operacionais citam alias (`distill.openrouter`, `core.models.registry`). Nao colar id literal de modelo neste handoff.

## O que foi entregue nesta sessao

- **USR-431**: Paridade do harness de operacao para Codex e Antigravity: implementado `core/ops/turn_guard.py` centralizando guardas agnosticas (learning pack obrigatorio para alteracoes de codigo, rastreamento de ciclo de vida de sessao no registro unificado de execucoes e handoff); `scripts/stop_hook_artifacts.py` unificado para delegar ao turn_guard. Portao oficial aprovado [HARNESS_PASS] em `60f17473` (4.805 testes, 0 falhas). PR #811 mesclado no `main` (commit `b25b5fc5`), imagem GHCR publicada, deploy Dokploy concluido com verificacao ao vivo no DarkHub (`https://darkhub.ggcampos.com/health` retornando `b25b5fc5`), ticket marcado `completed` no demands store.
- **USR-424**: Resolvido desacoplamento entre fila duravel (control store) e ledger local durante entrega autonoma. Portao oficial aprovado com [HARNESS_PASS] em `9c58d863` (4.796 testes, 0 falhas). PR #809 mesclado no `main` (commit `9842a697`), imagem GHCR publicada, deploy Dokploy concluido com verificacao ao vivo no DarkHub (`https://darkhub.ggcampos.com/health` retornando `9842a697`), ticket marcado `completed` no demands store.
- **USR-420**: Convergencia pos-deploy usa candidato fixo e respeita hosts desabilitados (`DARKFAC_FLEET_DISABLED_HOSTS=desktop`), isolando o ambiente em `tests/conftest.py`. Portao oficial aprovado [HARNESS_PASS] remotamente. PR #808 mesclado no `main` (commit `d2092f0e`), deploy Dokploy com health verificado ao vivo e ticket marcado `completed`.
- **USR-430**: Implementado filtro de hosts desabilitados (`DARKFAC_FLEET_DISABLED_HOSTS`) no despachante remoto (`core/harness/remote_dispatch.py`) e na rotacao de retentativas (`run_ticket.py`). Portao oficial rodado remotamente com 100% de sucesso (`[HARNESS_PASS]`, 4646 passed, 30 skipped, 0 failed). PR #803 mesclado no `main` (commit `ed6cf52d`), CI no `main` verde, deploy Dokploy concluido (darkfac-cloud e Darkhub blue-green no slot blue), ticket marcado `completed` no PostgreSQL e demands store com zero toque humano.
- **USR-436**: Roteador atualizado para permitir escrita interativa do Antigravity como operating harness (`DARKFAC_OPERATING_HARNESS=antigravity`). PR #805 mesclado (`90390014`), deploy Dokploy e ticket marcado `completed`.
- **USR-433**: Briefing de sessao agnostico de harness (`core.ops.briefing`), `docs/operacao/HANDOFF.md`, eleicao do Antigravity como operador no roteador, estabilizacao do medidor RPC de cota Antigravity. PR #798 mesclado e implantado.
- **USR-434**: Prevencao de regressao no snapshot diario do ledger; importacao de OAs git-only para a control store antes da exportacao do snapshot; unificacao e protecao de IDs no `core/owner_actions/store.py`; restauracao da acao do Desktop como `OA-081` e correcao do item #82 de decision sem opcoes para action. PR #799 mesclado (commit `3b655f03`); deploy Dokploy com DarkHub blue-green no slot blue.
- **USR-421**: Redirecionamento do cache do pytest por worktree, reaplicado no inicio de `pytest_sessionfinish`, para o foco do `fleet_dispatcher` terminar com codigo de saida e resumo `N passed in Xs` mesmo quando `%LOCALAPPDATA%` nao e gravavel. PR #801 mesclado no `main` (commit `58b4b2aa`), CI no `main` verde, deploy Dokploy concluido e ticket marcado `completed` no PostgreSQL e ledger.
- **USR-435**: CI no `main` verificado e 100% verde apos o merge do USR-421 (run 41130641330), ticket marcado `completed`.
- **Restauracao do Grokbox**: Disparo do wake email para o Grokbot via probe throttled, reativando a VM Linux no Tailscale e restabelecendo o worker oficial remoto de 95-143 segundos.

## O que esta em voo

- **Fila da frota**: tickets no control store: USR-343 e USR-375 em implementing (marcos estruturais V0); USR-369, USR-376, USR-377, USR-378, USR-379 e USR-279 em planned.
- **Monitoramento em background**: tarefa `task-506` ativa a cada 30 min para vigiar a saude da sessao e notificar o Owner via Telegram em caso de deadlock.

## Pendencias

- `OA-078` (opcional): Ligar aviso de PR protegido no Telegram: chat id no Hub e chave de assinatura [USR-395].
- `OA-081`: Destravar atualizacao do Desktop uma unica vez (quando concluir a indexacao do Segundo Cerebro).
- `OA-077`: Publicar consulta git de `core/governance/authorization.py` (USR-386).

## Armadilhas novas

1. **Ratchet de complexidade em funcoes orquestradoras**: `maybe_dispatch_remote` possui limite maximo de 178 linhas. Novas condicoes de validacao de host devem ser encapsuladas em funcoes auxiliares externas (ex.: `_probe_eligible_worker`) para evitar reprovacao no `complexity_ratchet.py`.
2. **Capacidade de reserva de host na frota**: `acquire_fleet_reservation` impoe limite estrito de 1 reserva ativa por host. Retomadas de entrega (`--resume-delivery`) aguardam a liberacao da reserva do ticket anterior.
3. **Wait de worker remoto ocupado no `runner.py`**: quando `grokbox` reporta `busy=True`, o dispatch remoto aguarda em polling de ate 300 s (5 min) antes de cair para o runner local. Para rodar testes locais imediatamente sem tentar o worker remoto nem esperar busy wait, passe `--local`.
4. **Capacidade de workers xdist no Windows**: hosts com RAM livre proxima a 5 GiB alocam apenas 1 worker por conservadorismo de memoria (demorando ~40 min para 4.700+ testes). Definir `$env:DARKFAC_MAX_WORKERS = '3'` reduz o tempo para ~23 min com total estabilidade e ~1.6 GiB de RAM consumida.
5. **Buffering de subprocessos Python no Windows**: streams redirecionadas para pipes seguram saida ate preencher buffer de 4/8 KB; para ver logs em tempo real em tarefas background, use `PYTHONUNBUFFERED=1` ou `python -u`.
6. **Reservas de frota zumbis**: se um canary ou run retentar planning sem rota disponivel, ele pode reter uma reserva `fres-...` no PostgreSQL. Ao diagnosticar fila ociosa com Grokbox saudavel, verifique a tabela `fleet_reservations`.
7. **Id literal no handoff**: o censo de modelos conta prefixos de modelos fora da allowlist. Este arquivo tem baseline 0. Use alias ou marque id de fio com `census: wire-id` na mesma linha.
8. **Ticket so na fila**: `complete_ticket` e `--resume-delivery` resolvem o id na control store quando o `demands.json` da worktree nao tem a linha (USR-424) e gravam o mesmo `(project_id, ticket_id)`. So id ausente na worktree, na fila, no checkout compartilhado e em `origin/main` devolve `cause=ticket_absent`, sem criar ticket. Nao copie o ledger compartilhado para a worktree so para a entrega passar.
9. **Resume do mesmo candidato (USR-424)**: no projeto `darkfac`, `validation.json` `passed` com `ValidationReport.candidate_digest` igual ao candidato atual permanece e o portao nao roda, tambem quando so a politica de revisao mudou.
10. **Corrida de CI do merge anterior no check-main**: ao disparar `dokploy_redeploy.py` imediatamente apos o merge, `check_main` pode inspecionar o ultimo run *concluido* do commit anterior enquanto o run do novo commit ainda esta `in_progress`.
11. **Subprocessos sem mock em loops de sync (`sync_usage_to_cloud.py`)**: chamadas como `run_daily_queue_snapshot` executam `git` no filesystem. Sob carga pesada, a execucao excede o intervalo de sync gerando `delay <= 0`. Nao encapsular `time.sleep(delay)` sob `if delay:` para garantir que `time.sleep(0.0)` sempre seja chamado (cedendo a CPU e disparando interceptadores em testes). Em testes de loop, mockar `run_daily_queue_snapshot` obrigatoriamente.
12. **Identificadores de nos no censo de modelos**: identificadores de host como o hostname do Grokbox iniciam com prefixo rastreado pelo regex do censo. Linhas de configuracao que contenham esses identificadores literais exigem o comentario `# census: wire-id` para nao reprovar no `test_model_id_ratchet_usr185.py`.

## Proximo passo

- Executar USR-431 em worktree dedicada: paridade do harness de operacao (Codex e Antigravity como interface de chat).
