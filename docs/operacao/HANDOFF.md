# Handoff operacional

Versao: 2
Atualizado em: 2026-10-29

## Estado

- Fase: V0 da Dark Factory.
- Canais: Telegram, DarkHub e interfaces de chat; demandas seguem por `run_ticket.py` e pelo registro compartilhado.
- Operacao: Antigravity e interface interativa; tarefas headless/frota sao executadas pelo harness eleito pelo roteador. Antigravity nunca e usado por workers, `--fleet` ou pump.

## Decisoes

- Consultar estado operacional pelo briefing compartilhado `python -m core.ops.briefing`.
- Cotas exibidas pelo briefing sao snapshots locais. Valor ausente, invalido ou `STALE` e desconhecido; na rota interativa do operador Antigravity isso nao o exclui silenciosamente: segue elegivel com aviso e causa registrada. O piso de 15% continua valendo para cotas conhecidas. Em 29/10, snapshot Antigravity atual (menos de 1 min) mostrou 98,91% semanal e 96,0% em 5h.
- O briefing nao dispara atualizacao de nos nem probes de cotas.
- Se PostgreSQL ou `gh` estiver indisponivel, o briefing informa o fallback da fila ou a indisponibilidade dos PRs; nao presume esses dados completos.

## Estado do medidor

- `antigravity_rpc` voltou a produzir snapshot atual e o adaptador persiste categoria/codigo/mensagem sanitizada da causa quando uma tentativa falha. A observacao desta versao confirma o snapshot local de 29/10, nao uma auditoria independente do painel.
- Manter este handoff atualizado ao encerrar sessoes de operacao.

## Pendencias

- Confirmar a telemetria de cota Antigravity na interface autenticada e manter este handoff atualizado ao encerrar sessoes de operacao.

## Proximo passo

- No inicio da sessao, executar `python -m core.ops.briefing`, definir `DARKFAC_OPERATING_HARNESS=antigravity` na interface e respeitar o harness eleito. Registrar aqui mudancas de estado, decisoes e pendencias antes de encerrar.
