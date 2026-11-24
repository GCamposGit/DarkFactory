# Handoff operacional

Versao: 14
Atualizado em: 2026-11-24

Este arquivo guarda o procedimento de coordenacao. Fatos correntes nao sao copiados para ca: coordenador, sessoes paralelas, fila, cotas, runs vivos e PRs saem do estado gerado.

```powershell
python C:\dev\DarkFac\scripts\line_briefing.py
python C:\dev\DarkFac\scripts\line_briefing.py --json
python C:\dev\DarkFac\scripts\line_briefing.py assumir --decisao-owner
```

## Papeis

Trocar de harness, de modelo ou de PC e trabalhar em paralelo sao comportamento esperado. Uma sessao coordena. As outras seguem.

- **Identidade da sessao.** O harness que executa o processo, em `core/line/session_identity.py`. Codex confirma por `CODEX_THREAD_ID`. Antigravity confirma por `ANTIGRAVITY_SESSION_ID` (e por `TERM_PROGRAM` antigravity). Claude usa `CLAUDECODE` ou `CLAUDE_CODE_SESSION_ID`. Grok usa `GROK_SESSION_ID` ou `GROK_AGENT`. Um valor antigo de `DARKFAC_OPERATING_HARNESS` nao define a identidade da sessao nem o coordenador. Sem sinal confiavel o registro grava `unknown` e o briefing nao escolhe harness a partir de valor antigo. Override explicito vale so para a chamada atual.
- **Sessao ativa.** Linha em `line_sessions`, renovada pela consulta ou pela assuncao, com sessao, harness, modelo, host e heartbeat de 900 segundos (15 minutos, `LEASE_TTL_SECONDS`). Registrar a sessao ativa nao cria coordenador.
- **Coordenador.** No maximo uma lease (`coordination_lease`, `singleton_id = 1`), com sessao, harness, modelo, host, horario, origem `owner_decision` e heartbeat. O arquivo compartilhado e `DARKFAC_SHARED_STATE_PATH` ou `<state root>/state/shared.sqlite3` e vale entre PCs.

`assumir --decisao-owner` troca a lease na hora, de qualquer harness, PC ou sessao, sem gravar variavel de ambiente e sem pergunta de confirmacao. Sem essa marca o processo sai com codigo 2 e o registro nao muda. Consultar deixa a lease como esta. A sessao que ja coordena renova o heartbeat vigente ao consultar. Lease vencida nao se renova. A fabrica nao elege outro coordenador. A causa fica `heartbeat_expired` ou `no_coordinator`, e a frota segue.

## Acoes exclusivas

Ficam reservadas ao coordenador: operar o despachante e a fila global, alterar a politica da frota e publicar o handoff de coordenacao.

Sessao paralela, no mesmo harness ou em outro, no mesmo modelo ou em outro, no mesmo PC ou em outro, segue o fluxo normal de ticket, worktree propria, `suite_lock` e reservas. Planejamento, ticket com pendencia humana, analise, diagnostico e projeto feito pela fabrica seguem sem aviso de erro, sem bloqueio e sem pedido de confirmacao.

## Estado

- Fase duravel: V0 da Dark Factory. Canais de entrada: Telegram, DarkHub e chat dos harnesses. A demanda segue por `run_ticket.py` e pelo registro do control store.
- Fatos correntes (coordenador, sessoes paralelas, fila, cotas, runs vivos e PRs) saem do estado gerado pelo comando acima. Este handoff nao nomeia coordenador, nao congela cota e nao lista run nem PR.
- Antigravity permanece fora de headless e da frota: workers, `--fleet` e pump nao o usam para escrever. No chat interativo ele pode ser eleito para `development` quando e o harness de operacao da sessao e a cota conhecida passa de 15%. O piso de 15% e a revisao em outra familia permanecem.
- O procedimento de coordenacao nao grava `DARKFAC_OPERATING_HARNESS` no ambiente do usuario.

## Decisoes do owner em vigor (09-10/11)

- Consulta e troca usam `scripts/line_briefing.py`. A troca so ocorre com decisao explicita do owner (`assumir --decisao-owner`).
- Sem essa decisao, a fabrica mantem o coordenador com heartbeat vigente. Se o heartbeat expirou, segue sem coordenador, com a causa registrada, e a frota segue. Nao ha eleicao automatica.
- Cotas exibidas pelo comando sao leituras sincronizadas. Valor ausente fica indisponivel. Valor acima de 600 segundos fica stale e nao confirma percentual. Na rota interativa, Antigravity com cota desconhecida segue elegivel com aviso e causa registrada. O piso de 15% continua valendo para cota conhecida. O comando nao dispara probe de cota.
- Revisao, piso de cota e o limite do Antigravity fora de headless/frota permanecem os que o roteador ja aplica.
- Textos operacionais citam alias. O id concreto de modelo fica no registro.

## Pendencias

- As pendencias humanas correntes estao no estado gerado e no backlog de acoes do owner no DarkHub. Este arquivo nao lista identificadores de acao, coordenador, cotas, runs nem PRs.

## Licoes de 22-23/11

- PR em rascunho de ticket fora da esteira trava a frota: o despachante relanca o ticket e o `run_ticket` recusa com `[RECUSA] ja possui PR ativo` (codigo 75, conta como espera). Ao assumir, cruze `gh pr list --json number,isDraft` com `recent_failures` em `.factory/runs/fleet_dispatcher_state.json`; PR de ticket com CI verde se revisa, promove e mescla.
- Recusa nao e falha, mas recusa repetida no mesmo sha e laco.
- Ticket entregue fora da esteira fecha com `register_ticket` (`status=completed` e evidencia) mais `AdmissionStore(state_root() / "admission.sqlite3").reset_budget("darkfac", id)`.

## Armadilhas

1. **Ratchet de complexidade em funcoes orquestradoras**: condicoes novas de validacao de host devem ser encapsuladas em funcoes auxiliares para o `complexity_ratchet.py` nao reprovar o orquestrador.
2. **Capacidade de reserva de host na frota**: `acquire_fleet_reservation` impoe limite estrito de 1 reserva ativa por host. Retomada de entrega aguarda a liberacao da reserva do ticket anterior.
3. **Wait de worker remoto ocupado**: quando o worker reporta `busy=true`, o dispatch remoto aguarda em polling ate `DARKFAC_REMOTE_BUSY_WAIT_SEC` (padrao 300 s) antes de cair para o runner local. Para rodar testes locais imediatamente, passe `--local`.
4. **Capacidade de workers xdist no Windows**: hosts com pouca RAM livre alocam poucos workers. `DARKFAC_MAX_WORKERS` limita o paralelismo local.
5. **Buffering de subprocessos Python no Windows**: streams redirecionadas seguram saida ate encher o buffer. Para log ao vivo, use `PYTHONUNBUFFERED=1` ou `python -u`.
6. **Reservas de frota zumbis**: um run que retenta planning sem rota pode reter reserva no control store. Fila ociosa com host saudavel pede olhar `fleet_reservations`.
7. **Id literal no handoff**: o censo de modelos conta prefixos fora da allowlist. Este arquivo tem baseline 0. Use alias ou marque id de fio com `census: wire-id` na mesma linha.
8. **Ticket so na fila**: `complete_ticket` e `--resume-delivery` resolvem o id na control store quando o `demands.json` da worktree nao tem a linha (USR-424). So id ausente na worktree, na fila, no checkout compartilhado e em `origin/main` devolve `cause=ticket_absent`.
9. **Resume do mesmo candidato (USR-424)**: no projeto `darkfac`, `validation.json` `passed` com o mesmo `candidate_digest` permanece e o portao nao roda de novo so porque a politica de revisao mudou.
10. **Corrida de CI do merge anterior no check-main**: `dokploy_redeploy.py` logo apos o merge pode inspecionar o ultimo run concluido do commit anterior enquanto o run novo ainda esta em progresso.
11. **Subprocessos sem mock em loops de sync**: em teste de loop, mockar o snapshot diario. Nao encapsular `time.sleep(delay)` sob `if delay:`, para `time.sleep(0.0)` continuar visivel aos testes.
12. **Identificadores de nos no censo de modelos**: hostname que coincide com prefixo rastreado pede o comentario `census: wire-id` na mesma linha.
13. **Recusa nao e falha**: saidas 75, `[TRAVA DE LINHA]`, `[RESERVA DE FROTA]` e "ja possui PR ativo" sao espera e nao devem acender recuperacao. Se surgir outro tipo de recusa contando como falha, adicione o marcador em `LOCK_MARKERS` de `core/line/fleet_dispatcher.py`.
14. **Teto de frota na recuperacao**: com recuperacao ativa, `run_ticket.py` usa teto de frota 1; a recuperacao global so deve ser acionada explicitamente.
15. **Cota do Grok**: queda de uso no mesmo periodo e marcada como leitura suspeita e o roteador exclui o Grok. O mecanismo de confirmacao e `.factory/usage/history/xai.confirmation.json` (quota_id, period_start, used_percent da leitura atual).
16. **`admission.sqlite3`**: falhas antigas de causa ja corrigida podem ser fechadas (`update failures set open=0`) e `dispatcher_recovery.active=0`, sempre com backup em `.factory/backups/`. O fechamento e automatico quando o `main` muda.
17. **CI de PR**: leva de 3 a 5 minutos para criar o run do `DarkFac CI`; antes disso so aparece `trusted-pr-policy`.
18. **`gh pr close`** pode ser negado pelo classificador de permissoes; peca autorizacao explicita ao owner.

19. `learning.cli record-turn` altera o `learning_ledger.json` rastreado no checkout compartilhado. Antes de avancar o checkout: `git stash push -- .factory/learning/learning_ledger.json`, depois `merge --ff-only` e `stash pop`.
20. `dokploy_redeploy.py` sai com codigo 3 quando so o Notebook nao converge; os servicos podem estar corretos. Confira as linhas `[RUNTIME IMAGE]`.

## Proximo passo

- Consultar o estado gerado com `python C:\dev\DarkFac\scripts\line_briefing.py` e seguir a fila, as cotas, os runs e os PRs que o comando mostrar. A troca de coordenador, quando o owner decide, e `python C:\dev\DarkFac\scripts\line_briefing.py assumir --decisao-owner`.
