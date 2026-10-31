<!-- covers: core/ops/briefing.py, core/ops/turn_guard.py, core/line/operating_harness.py, core/line/routing.py, core/line/feeder_cycle.py, core/line/stage_integration.py, run_ticket.py -->
# Operacao interativa por Antigravity e Codex (USR-431, USR-433)

## Inicio e roteamento

Antigravity e Codex atuam como interfaces de operacao interativa para coordenar a fabrica pelo chat. No inicio de uma sessao:

```powershell
$env:DARKFAC_OPERATING_HARNESS = 'antigravity' # ou 'codex'
python -m core.ops.briefing
```

O briefing e UTF-8 e somente leitura. Mostra a fila do control store, acoes abertas do backlog e fontes dinamicas, estado dos workers sem executar atualizacao/convergencia, PRs abertos, snapshots de quota e o handoff versionado em `docs/operacao/HANDOFF.md`. Cada secao tem fallback independente: falha do banco usa a fila local em memoria; `gh` ausente, sem autenticacao, com erro ou timeout informa que PRs estao indisponiveis. Falha de uma secao nao interrompe as demais. O briefing nao executa probes de quota.

Com `DARKFAC_OPERATING_HARNESS=antigravity`, o roteador pode eleger Antigravity para `development` somente nesta interface interativa e somente quando a cota conhecida excede o piso de 15%. Se eleito, implemente no proprio chat. Quando outro harness for eleito, encaminhe o ticket ao fluxo canonico:

```powershell
python C:\dev\DarkFac\run_ticket.py USR-XXX
```

Antigravity nunca e elegivel em modo headless ou frota: `fleet_dispatcher`, workers, `--fleet` e pump nao podem usa-lo. Esses caminhos seguem a rota de desenvolvimento do despachante. Se harnesses locais de alta capacidade estiverem esgotados para o estagio `planning`, o roteador aciona a contingencia OpenRouter (`planning.openrouter`) de forma autonoma.

O `run_ticket.py` revalida a cota de `development` antes de cada fatia do plano, com um `pick` fresco sobre a leitura daquele momento. Um plano de uma fatia faz esse pick uma vez. Um plano de várias fatias repete o pick antes de cada agente, e a leitura pode trocar o harness write entre fatias. O pin `--harness` e o `--force` do preflight nao iniciam, numa fatia, um harness com headroom conhecido de 15% ou menos, ou sem leitura. Com `--fleet`, esse pick usa `execution_context` headless: Antigravity nao inicia a fatia mesmo sendo o harness de operacao, com Google acima de 15% e os demais no piso. Sem rota elegivel o processo espera (codigo 75, JSON com `cause` `route_wait`, `stage` `BUILD` e o caminho da worktree), nao chama o agente da fatia, nao comeca a entrega e a worktree permanece, inclusive um arquivo gravado durante o PLAN e o trabalho das fatias anteriores. `_discard_untouched_workspace` e `_report_workspace_fate` nao removem esse diretorio.

## Entrega de ticket so na fila (USR-424)

`--resume-delivery` resolve o id antes de exigir o `demands.json` da worktree, nesta ordem: ledger da worktree, fila duravel do control store, arquivo do checkout compartilhado e `origin/main`. A fila materializa o mesmo id no ledger da worktree, sem `next_ticket_id`, sem o agente de correcao e sem copiar o ledger compartilhado. O arquivo do checkout compartilhado segue para a entrega sem gravar a worktree e sem criar linha na fila. Id so em `origin/main` continua encontrado; o rebase posterior traz a linha. Id ausente nos quatro locais encerra com exit 1, `ok=false`, `cause=ticket_absent` e `error` no formato `Ticket <ID> not found in demands store`. O stderr cita o ledger da worktree, a control store, o checkout compartilhado e `origin/main`. O ledger e o `control.db` permanecem como estavam. `run_ticket.py USR-XXX` carrega o id no ledger do checkout (a fila entra nessa leitura) e, se faltar, em `origin/main`.

No projeto `darkfac`, validacao e revisao ja vigentes para o mesmo candidato nao rodam de novo. `ValidationReport.candidate_digest` fica em `validation.json`. Um relatorio `passed` cujo digest coincide com o candidato atual permanece e o portao nao roda, tambem quando so a politica de revisao mudou. Digest vazio permanece so quando a ultima rodada de `review_state` e `approve`, `approval_binding_valid` aceita o candidato e a politica, e `forced` e false. Digest diferente, `passed=false`, veredito diferente de `approve` ou digest vazio sem esse binding apagam `validation.json` e o portao roda. O veredito so apaga digest vazio; digest coincidente permanece. Aprovacao vigente nao chama o revisor: `review_verdict.json` approve e mantido ou reescrito como `approve`. `merge.json` e o relatorio de deploy do attempt anterior sao removidos mesmo quando a validacao e reaproveitada. Mudanca de `candidate_digest` roda o portao e a revisao de novo.

## Cota ausente ou antiga

O briefing le apenas snapshots locais e mostra idade e causa estruturada quando disponiveis. Snapshot ausente, invalido ou `STALE` nao confirma percentual. Na rota interativa, o harness de operacao Antigravity continua elegivel com aviso explicito de cota desconhecida/antiga enquanto a telemetria e reparada; esse fail-open e restrito a preferencia do operador no chat e nao muda o piso para qualquer snapshot atual conhecido. A causa da falha fica registrada pelo medidor `antigravity_rpc`. Execucao headless continua excluida mesmo que a cota esteja atual.

Para solicitar uma leitura atualizada fora do briefing, use o comando de contas autenticado conforme o runbook de quota; nao interprete uma falha do probe como percentual zero ou como amostra nova.

## Guardas de fim de turno agnosticas (USR-431)

Para garantir a disciplina de entrega e continuidade independentemente do harness (Claude Code, Codex, Antigravity ou Grok Build), as guardas de fim de turno sao centralizadas em `core/ops/turn_guard.py`:

```powershell
# Verificar invariantes de encerramento de turno (learning pack, handoff e sessao)
python -m core.ops.turn_guard --check

# Encerramento de sessao no registro unificado de execucoes
python -m core.ops.turn_guard --finish-session --harness codex --status succeeded --evidence "PR #810"
```

O `turn_guard` valida se alteracoes em arquivos de codigo (`core/`, `hub/`, `scripts/`, `tests/`, `run_ticket.py`) possuem o respectivo learning pack em `.factory/learning_packs/`. Em caso de violacao, encerra com codigo de saida `2` e mensagem orientando a geracao do artefato. O hook `stop_hook_artifacts.py` delega a mesma verificacao ao `turn_guard`, assegurando fonte unica das regras.

## Encerramento e continuidade

Antes de encerrar uma sessao, atualize `docs/operacao/HANDOFF.md`: incremente a versao, atualize a data e registre estado, decisoes, pendencias e o proximo passo para o operador seguinte. Nao inclua segredos, prompts ou dados pessoais. Mantenha `docs/FASE_ATUAL.md` alinhado quando mudar um fato operacional persistente.

## Respeito a hosts desabilitados (USR-430)

O operador interativo e os scripts de esteira (`run_ticket.py`) honram `DARKFAC_FLEET_DISABLED_HOSTS` tanto na frota de execucao quanto no despacho de workers remotos para teste oficial, prevenindo envio de jobs ou sondas a maquinas dedicadas a outras tarefas (ex.: Desktop indexando o Segundo Cerebro).

## Sentinela da Linha e monitoramento autonomo a cada 45m (USR-439)

O monitoramento da fabrica opera de forma agnostica e centralizada em `core/line/sentinel.py`, garantindo deteccao perene de deadlocks e estagnacoes sem depender exclusivamente da sessao interativa do chat:

- **Intervalo deterministico de 45 minutos (2700s)**: calculado por delta de timestamp (`last_run_epoch`), eliminando a assimetria do cron standard 5-campos (`*/45` que dispararia aos :00 e :45, gerando intervalo de 15m).
- **Cobertura multi-harness**:
  - `fleet_dispatcher.py`: executa `sentinel_hook` a cada ciclo (com guarda temporal de 45m) na frota headless contínua 24/7.
  - `core/ops/briefing.py`: exibe a secao `## Sentinela da Linha` e dispara verificacao automatica no inicio de sessao para Claude Code, Codex, Antigravity e Grok.
  - `core/ops/turn_guard.py`: executa checagem nao-bloqueante nos encerramentos de turno.
  - `scripts/run_sentinel.ps1`: script PowerShell para execucao direta ou agendamento no Task Scheduler do Windows.
- **Alertas claros no Telegram**: em caso de parada por falta de rota (todas as contas <= 15%) ou jobs travados em voo sem escrita em log ha mais de 45m, dispara notificacao clara em linguagem para leigo conforme padrao USR-438 via `NotificationService`.

## Capacidade de escrita interativa no roteador (USR-436)

O roteador valida as capacidades com `agent_cli.supports(harness, mode)` antes de ranquear candidatos para o estagio `development`. Com `operating_harness=antigravity` em sessao interativa (fora do modo headless), o Antigravity declara capacidade de escrita para desenvolvimento no chat, viabilizando sua eleicao quando a cota estiver saudavel acima do piso de 15%. Em execucao headless, na frota ou com outro operador, Antigravity permanece estritamente somente-leitura.
