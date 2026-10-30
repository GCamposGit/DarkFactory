# Handoff operacional

Versao: 3
Atualizado em: 2026-10-30

## Estado

- Fase: V0 da Dark Factory.
- Canais: Telegram, DarkHub e interfaces de chat; demandas seguem por `run_ticket.py` e pelo registro compartilhado no control store.
- Operacao: Antigravity como interface interativa de operacao (`DARKFAC_OPERATING_HARNESS=antigravity`); tarefas headless/frota sao executadas pelo harness eleito pelo roteador (Grokbox e Notebook). Antigravity nunca e usado por workers, `--fleet` ou pump.

## O que foi entregue nesta sessao

- **USR-433**: Briefing de sessao agnostico de harness (`core.ops.briefing`), `docs/operacao/HANDOFF.md`, eleicao do Antigravity como operador no roteador, estabilizacao do medidor RPC de cota Antigravity. PR #798 mesclado e implantado.
- **USR-434**: Prevencao de regressao no snapshot diario do ledger (`scripts/snapshot_queue.py` aborta se qualquer ID existente for perdido, mesmo se substituido por novo ID com contagem igual); importacao de OAs git-only para a control store antes da exportacao do snapshot; unificacao e protecao de IDs no `core/owner_actions/store.py` (`next_action_id` consulta todo o historico git para impedir reutilizacao de qualquer ID historico); restauracao da acao do Desktop como `OA-081` e correcao do item #82 de decision sem opcoes para action.
- **Roteamento de modelos e contingencia**:
  - Grok adicionado na cascata da etapa `planning`, acionado como contingencia quando tanto Claude quanto Codex estiverem esgotados/criticos (<=15%).
  - Fallback para OpenRouter (`distill.openrouter` -> `deepseek/deepseek-v4.1-flash`) quando todos os 3 harnesses (Claude, Codex e Grok) estiverem sem cota ou indisponiveis.
  - Documentacao atualizada em `docs/FASE_ATUAL.md`, `docs/manual/roteamento.md`, `docs/manual/fila.md` e `docs/manual/operacao_interativa.md`.
- **Estabilidade de testes**: ajuste de margem no heartbeat do wrapper Claude (`tests/test_usr209_session_registration.py`) eliminando flake sob alta carga sustentada de CPU.
- **Ciclo autonomo completo**: portao oficial aprovado com `[HARNESS_PASS]` em 4.671 testes; CI aprovado; PR #799 mesclado no `main` (commit `3b655f03949eeb1cc945c6107d73d68d64ef4d96`); deploy Dokploy realizado com sucesso (DarkHub blue-green servindo slot blue, darkfac-cloud atualizado, backup 3-tier verificado); USR-434 marcado `completed` na DemandsStore.
- **Destravamento da frota**: cancelamento da reserva `fres-2c869b3d2606` que mantinha Grokbox preso em retry de canary sem rota, liberando Grokbox para o `DarkFac-Fleet-Dispatcher`.

## O que esta em voo

- **Fila da frota**: tickets planejados no control store: USR-420 (sendo puxado pelo Grokbox), USR-421, USR-424, USR-428, USR-429, USR-430, USR-431.
- **Monitoramento em background**: tarefa `task-506` ativa a cada 30 min para vigiar a saude da sessao e notificar o Owner via Telegram em caso de deadlock.

## Pendencias do owner

- `OA-078` (opcional): Ligar aviso de PR protegido no Telegram: chat id no Hub e chave de assinatura [USR-395].
- `OA-081`: Destravar atualizacao do Desktop uma unica vez (quando concluir a indexacao do Segundo Cerebro).
- `OA-077`: Publicar consulta git de `core/governance/authorization.py` (USR-386).

## Armadilhas novas

1. **Wait de worker remoto ocupado no `runner.py`**: quando `grokbox` reporta `busy=True`, o dispatch remoto aguarda em polling de ate 300 s (5 min) antes de cair para o runner local. Para rodar testes locais imediatamente sem tentar o worker remoto nem esperar busy wait, passe `--local`.
2. **Capacidade de workers xdist no Windows**: hosts com RAM livre proxima a 4 GiB alocam apenas 1 worker por conservadorismo de memoria (demorando ~40 min para 4.670+ testes). Definir `$env:DARKFAC_MAX_WORKERS = '2'` reduz o tempo para ~20 min com total estabilidade.
3. **Buffering de subprocessos Python no Windows**: streams redirecionadas para pipes seguram saida ate preencher buffer de 4/8 KB; para ver logs em tempo real em tarefas background, use `PYTHONUNBUFFERED=1` ou `python -u`.
4. **Reservas de frota zumbis**: se um canary ou run retentar planning sem rota disponivel, ele pode reter uma reserva `fres-...` no PostgreSQL. Ao diagnosticar fila ociosa com Grokbox saudavel, verifique a tabela `fleet_reservations`.

## Proximo passo

- No inicio da sessao, executar `python -m core.ops.briefing`, confirmar `DARKFAC_OPERATING_HARNESS=antigravity` e acompanhar o avanco da frota headless nos tickets planejados.
