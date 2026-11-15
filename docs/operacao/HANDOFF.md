# Handoff operacional

Versao: 13
Atualizado em: 2026-11-15 (Claude Code, coordenador em 15/11; outra sessao assume)

## Estado

- Fase: V0 da Dark Factory (0/5 entregas qualificadas; piloto M1 bloqueado na OA-082).
- `main` em `060e4f41` (#854), CI verde.
- Cotas (15/11 ~16:20 BRT): Claude 83%, Codex 33%, Grok 27,6%. Roteador elege Claude/sonnet para `development`.
- Frota em DRENAGEM a pedido do owner: a tarefa agendada `DarkFac-Fleet-Dispatcher` foi DESATIVADA (Enabled=False) para nao relancar. O processo do despachante pode seguir vivo ate ser encerrado (o classificador negou encerrar o processo pelo agente); confira com o comando abaixo. Encerrar o despachante NAO derruba os runs em voo: o job do launcher nao tem KILL_ON_JOB_CLOSE.
- Em voo no momento da troca: USR-516 no Notebook (`run_ticket.py USR-516`, retomada de entrega da worktree `.worktrees/usr-516-20261114T232201Z`, log `.factory/logs/coord/USR-516.log`), USR-455 no Desktop, USR-457 no Grokbox.

## Incidente de 14-15/11: frota parada sem alerta

1. Edicoes nao commitadas da sessao Antigravity (14/11) no checkout compartilhado faziam o portao de TODA worktree falhar com `gate_dirty_worktree`. Causa raiz confirmada: o launcher do despachante injeta `DARKFAC_PROJECT_ROOT=C:\dev\DarkFac` no ambiente, e `core/harness/runner.py` usa `project_root()` para checar a arvore limpa, olhando o checkout compartilhado em vez da worktree candidata. Correcao: USR-516 (em curso). Confirme que o fix cobre o `DARKFAC_PROJECT_ROOT` herdado (o `gate_isolation` ja limpa essa variavel; o caminho da frota nao).
2. 154 falhas `error` com `main_sha` vazio em 51 tickets nunca fechavam (USR-497 nao alcanca). Correcao: USR-511, PR #857.
3. Sentinela marcava `healthy` com tudo parado: USR-523 (planned).

Acoes da coordenacao: edicoes preservadas e publicadas (OA-094, opcao A: PRs #855 e #856); backup do admission em `.factory/backups/coord-20261115T191712Z`; 179 falhas velhas fechadas e 10 holds `gate_dirty_worktree` removidos. A frota voltou a despachar nos tres hosts.

## Pendencias para a proxima sessao

- PR #855 (skills/docs/cofre SOPS cifrado, sem protegidos): conferir CI e mergear.
- PR #856 (`AGENTS.md`): espera a assinatura do owner (OA-095). Depois de assinado, publicar o registro `.worktrees/oa094-auth/.factory/governance/authorizations/AUTH-OA094-AGENTS.json` em PR proprio (como o #853) e mergear o #856. Nao fazer `update-branch` no #856.
- PR #857 (USR-511): entregar pelo caminho oficial com `python C:\dev\DarkFac\run_ticket.py --resume-delivery USR-511 --worktree C:\dev\DarkFac\.worktrees\usr-511-coord` quando a trava de linha do Notebook liberar.
- USR-516: acompanhar o fim da entrega; se falhar, retomar com o comando impresso no log.
- Depois do merge do USR-511 e USR-516: deploy (`python C:\dev\DarkFac\scripts\dokploy_redeploy.py`) e religar a frota (`Enable-ScheduledTask -TaskName DarkFac-Fleet-Dispatcher`, depois `Start-ScheduledTask`).
- Proxima prioridade: USR-523, USR-503/USR-521 (xdist sob carga), USR-498, USR-506; retomar PRs de CI vermelho #840, #844-#847.
- O `git stash` "antigravity-gemini-models+sops-vault 2026-11-14" pode ser descartado depois do merge de #855 e #856.

## Comandos uteis

- Processos da frota: `Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match 'fleet_dispatcher|run_ticket' } | Select-Object ProcessId,CommandLine`
- Estado: `.factory/runs/fleet_dispatcher_state.json` (`in_flight`, `skipped`, `idle_reasons`) e `.factory/admission.sqlite3` (`failures`, `ticket_holds`).

## Armadilhas novas

1. Checkout compartilhado sujo derruba a frota inteira ate o USR-516 entrar. Ao assumir: `git -C C:\dev\DarkFac status --short` deve sair vazio.
2. Trava de linha: um `run_ticket` por maquina; o segundo sai com codigo 2 sem consumir cota.
3. Ticket novo com `--create --queue-only` entra na fila e o despachante vivo pega na hora.
4. Encerrar processo do despachante pelo agente foi negado pelo classificador; peca ao owner ou use a tarefa agendada.

As armadilhas da versao 12 seguem validas (recusa nao e falha, teto de frota 1 em recuperacao, cota do Grok suspeita, CI de PR leva 3-5 min, `gh pr close` pode ser negado, id literal de modelo quebra o censo).
