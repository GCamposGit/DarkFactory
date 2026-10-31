<!-- covers: core/ops/briefing.py, core/line/operating_harness.py, core/line/routing.py, core/line/feeder_cycle.py, core/line/stage_integration.py, run_ticket.py -->
# Operacao interativa por Antigravity (USR-433)

## Inicio e roteamento

Antigravity e uma interface de chat para coordenar a fabrica, nao um executor headless. No inicio de uma sessao:

```powershell
$env:DARKFAC_OPERATING_HARNESS = 'antigravity'
python -m core.ops.briefing
```

O briefing e UTF-8 e somente leitura. Mostra a fila do control store, acoes abertas do backlog e fontes dinamicas, estado dos workers sem executar atualizacao/convergencia, PRs abertos, snapshots de quota e o handoff versionado em `docs/operacao/HANDOFF.md`. Cada secao tem fallback independente: falha do banco usa a fila local em memoria; `gh` ausente, sem autenticacao, com erro ou timeout informa que PRs estao indisponiveis. Falha de uma secao nao interrompe as demais. O briefing nao executa probes de quota.

Com `DARKFAC_OPERATING_HARNESS=antigravity`, o roteador pode eleger Antigravity para `development` somente nesta interface interativa e somente quando a cota conhecida excede o piso de 15%. Se eleito, implemente no proprio chat. Quando outro harness for eleito, encaminhe o ticket ao fluxo canonico:

```powershell
python C:\dev\DarkFac\run_ticket.py USR-XXX
```

Antigravity nunca e elegivel em modo headless ou frota: `fleet_dispatcher`, workers, `--fleet` e pump nao podem usa-lo. Esses caminhos seguem a rota de desenvolvimento do despachante. Se harnesses locais de alta capacidade estiverem esgotados para o estagio `planning`, o roteador aciona a contingencia OpenRouter (`planning.openrouter`) de forma autonoma.

O `run_ticket.py` revalida a cota de `development` antes de cada fatia do plano, com um `pick` fresco sobre a leitura daquele momento. Um plano de uma fatia faz esse pick uma vez. O pin `--harness` e o `--force` do preflight nao iniciam, numa fatia, um harness com headroom conhecido de 15% ou menos, ou sem leitura. Sem rota elegivel o processo espera (codigo 75), nao chama o agente da fatia, nao comeca a entrega e a worktree permanece, inclusive o trabalho das fatias anteriores.

## Cota ausente ou antiga

O briefing le apenas snapshots locais e mostra idade e causa estruturada quando disponiveis. Snapshot ausente, invalido ou `STALE` nao confirma percentual. Na rota interativa, o harness de operacao Antigravity continua elegivel com aviso explicito de cota desconhecida/antiga enquanto a telemetria e reparada; esse fail-open e restrito a preferencia do operador no chat e nao muda o piso para qualquer snapshot atual conhecido. A causa da falha fica registrada pelo medidor `antigravity_rpc`. Execucao headless continua excluida mesmo que a cota esteja atual.

Para solicitar uma leitura atualizada fora do briefing, use o comando de contas autenticado conforme o runbook de quota; nao interprete uma falha do probe como percentual zero ou como amostra nova.

## Encerramento e continuidade

Antes de encerrar uma sessao, atualize `docs/operacao/HANDOFF.md`: incremente a versao, atualize a data e registre estado, decisoes, pendencias e o proximo passo para o operador seguinte. Nao inclua segredos, prompts ou dados pessoais. Mantenha `docs/FASE_ATUAL.md` alinhado quando mudar um fato operacional persistente.

## Respeito a hosts desabilitados (USR-430)

O operador interativo e os scripts de esteira (`run_ticket.py`) honram `DARKFAC_FLEET_DISABLED_HOSTS` tanto na frota de execucao quanto no despacho de workers remotos para teste oficial, prevenindo envio de jobs ou sondas a maquinas dedicadas a outras tarefas (ex.: Desktop indexando o Segundo Cerebro).

## Capacidade de escrita interativa no roteador (USR-436)

O roteador valida as capacidades com `agent_cli.supports(harness, mode)` antes de ranquear candidatos para o estagio `development`. Com `operating_harness=antigravity` em sessao interativa (fora do modo headless), o Antigravity declara capacidade de escrita para desenvolvimento no chat, viabilizando sua eleicao quando a cota estiver saudavel acima do piso de 15%. Em execucao headless, na frota ou com outro operador, Antigravity permanece estritamente somente-leitura.
