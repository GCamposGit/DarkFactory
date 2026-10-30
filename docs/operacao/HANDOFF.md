# Handoff operacional

Versao: 4
Atualizado em: 2026-10-30

## Estado

- Fase: V0 da Dark Factory.
- Canais: Telegram, DarkHub e interfaces de chat; demandas seguem por `run_ticket.py` e pelo registro compartilhado no control store.
- Operacao: Antigravity como interface interativa de operacao (`DARKFAC_OPERATING_HARNESS=antigravity`); tarefas headless/frota sao executadas pelo harness eleito pelo roteador (Grokbox e Notebook). Antigravity nunca e usado por workers, `--fleet` ou pump.
- USR-421: o foco `python -m pytest tests/test_fleet_dispatcher.py -q` encerra sozinho no Windows depois de 100%, com cache de pytest por worktree. O contrato esta em `docs/manual/testes.md` e `docs/FASE_ATUAL.md`.

## Decisoes

- Consultar estado operacional pelo briefing compartilhado `python -m core.ops.briefing`. Cotas exibidas sao snapshots locais. Valor ausente, invalido ou `STALE` e desconhecido; na rota interativa do operador Antigravity isso nao o exclui silenciosamente: segue elegivel com aviso e causa registrada. O piso de 15% continua valendo para cotas conhecidas. O briefing nao dispara atualizacao de nos nem probes de cotas.
- Se PostgreSQL ou `gh` estiver indisponivel, o briefing informa o fallback da fila ou a indisponibilidade dos PRs; nao presume esses dados completos.
- Grok entra na cascata da etapa `planning` como contingencia quando Claude e Codex estiverem esgotados ou criticos (<=15%).
- Quando Claude, Codex e Grok estiverem sem cota ou indisponiveis, o fallback de leitura vai para OpenRouter pelo alias `distill.openrouter` do registro de modelos. O id concreto fica so no registro.
- O snapshot diario do ledger (`scripts/snapshot_queue.py`) aborta se qualquer ID existente for perdido, mesmo se substituido por novo ID com contagem igual. `next_action_id` consulta todo o historico git para impedir reutilizacao de ID.
- Textos operacionais citam alias (`distill.openrouter`, `core.models.registry`). Nao colar id literal de modelo neste handoff.

## O que foi entregue nesta sessao

- **USR-433**: Briefing de sessao agnostico de harness (`core.ops.briefing`), `docs/operacao/HANDOFF.md`, eleicao do Antigravity como operador no roteador, estabilizacao do medidor RPC de cota Antigravity. PR #798 mesclado e implantado.
- **USR-434**: Prevencao de regressao no snapshot diario do ledger; importacao de OAs git-only para a control store antes da exportacao do snapshot; unificacao e protecao de IDs no `core/owner_actions/store.py`; restauracao da acao do Desktop como `OA-081` e correcao do item #82 de decision sem opcoes para action. PR #799 mesclado (commit `3b655f03949eeb1cc945c6107d73d68d64ef4d96`); deploy Dokploy com DarkHub blue-green no slot blue.
- **USR-421**: redirecionamento do cache do pytest por worktree, reaplicado no inicio de `pytest_sessionfinish`, para o foco do `fleet_dispatcher` terminar com codigo de saida e resumo `N passed in Xs` mesmo quando `%LOCALAPPDATA%` nao e gravavel.
- **Estabilidade de testes**: ajuste de margem no heartbeat do wrapper Claude (`tests/test_usr209_session_registration.py`) eliminando flake sob alta carga sustentada de CPU.
- **Destravamento da frota**: cancelamento da reserva `fres-2c869b3d2606` que mantinha Grokbox preso em retry de canary sem rota, liberando Grokbox para o `DarkFac-Fleet-Dispatcher`.
- **Handoff**: a versao 3 removeu o titulo `## Decisoes` e gravou um id literal de modelo. A versao 4 devolve as secoes do contrato e cita so o alias.

## O que esta em voo

- **Fila da frota**: tickets planejados no control store: USR-420 (sendo puxado pelo Grokbox), USR-424, USR-428, USR-429, USR-430, USR-431. USR-421 esta neste candidato, ainda sem merge.
- **Monitoramento em background**: tarefa `task-506` ativa a cada 30 min para vigiar a saude da sessao e notificar o Owner via Telegram em caso de deadlock.

## Pendencias

- `OA-078` (opcional): Ligar aviso de PR protegido no Telegram: chat id no Hub e chave de assinatura [USR-395].
- `OA-081`: Destravar atualizacao do Desktop uma unica vez (quando concluir a indexacao do Segundo Cerebro).
- `OA-077`: Publicar consulta git de `core/governance/authorization.py` (USR-386).

## Armadilhas novas

1. **Wait de worker remoto ocupado no `runner.py`**: quando `grokbox` reporta `busy=True`, o dispatch remoto aguarda em polling de ate 300 s (5 min) antes de cair para o runner local. Para rodar testes locais imediatamente sem tentar o worker remoto nem esperar busy wait, passe `--local`.
2. **Capacidade de workers xdist no Windows**: hosts com RAM livre proxima a 4 GiB alocam apenas 1 worker por conservadorismo de memoria (demorando ~40 min para 4.670+ testes). Definir `$env:DARKFAC_MAX_WORKERS = '2'` reduz o tempo para ~20 min com total estabilidade.
3. **Buffering de subprocessos Python no Windows**: streams redirecionadas para pipes seguram saida ate preencher buffer de 4/8 KB; para ver logs em tempo real em tarefas background, use `PYTHONUNBUFFERED=1` ou `python -u`.
4. **Reservas de frota zumbis**: se um canary ou run retentar planning sem rota disponivel, ele pode reter uma reserva `fres-...` no PostgreSQL. Ao diagnosticar fila ociosa com Grokbox saudavel, verifique a tabela `fleet_reservations`.
5. **Id literal no handoff**: o censo de modelos conta `claude-`, `gpt-`, `grok-`, `gemini-` e `deepseek-` fora da allowlist. Este arquivo tem baseline 0. Use alias ou marque id de fio com `census: wire-id` na mesma linha.

## Proximo passo

- No inicio da sessao, executar `python -m core.ops.briefing`, confirmar `DARKFAC_OPERATING_HARNESS=antigravity` e acompanhar o avanco da frota headless nos tickets planejados.
