<!-- covers: core/ops/briefing.py, core/ops/turn_guard.py, core/line/operating_harness.py, core/line/routing.py, core/line/feeder_cycle.py, core/line/stage_integration.py, core/line/sentinel.py, run_ticket.py -->
# Operacao interativa por Antigravity e Codex (USR-431, USR-433)

Sem assinatura elegível, usar a contingência OpenRouter com saldo e orçamento
confirmados, preservando revisão por outra família. `route_wait` fica persistido
para o dispatcher retomar automaticamente; o operador não precisa voltar ao chat.
Durante recuperação manter `--resume-only` ativo, conforme [frota.md](frota.md),
em vez de desligar o controlador junto com os heartbeats de LLM (USR-473).

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

Antigravity nunca e elegivel em modo headless ou frota: `fleet_dispatcher`, workers, `--fleet` e pump nao podem usa-lo. Esses caminhos seguem a rota de desenvolvimento do despachante. Se harnesses locais de alta capacidade estiverem esgotados para o estagio `planning` ou `development`, o roteador aciona dinamicamente a contingencia OpenRouter via Fronteira de Pareto (`resolve_pareto_openrouter_model`, USR-440/USR-444) sem modelos hard-coded, garantindo classe GPT Sol / Claude Opus para planejamento, resolucao automatica de wire IDs de API e governanca estrita de orcamento.

O `run_ticket.py` revalida a cota de `development` antes de cada fatia do plano, com um `pick` fresco sobre a leitura daquele momento. Um plano de uma fatia faz esse pick uma vez. Um plano de várias fatias repete o pick antes de cada agente, e a leitura pode trocar o harness write entre fatias. O pin `--harness` e o `--force` do preflight nao iniciam, numa fatia, um harness com headroom conhecido de 15% ou menos, ou sem leitura. Com `--fleet`, esse pick usa `execution_context` headless: Antigravity nao inicia a fatia mesmo sendo o harness de operacao, com Google acima de 15% e os demais no piso. Sem rota elegivel o processo espera (codigo 75, JSON com `cause` `route_wait`, `stage` `BUILD` e o caminho da worktree), nao chama o agente da fatia, nao comeca a entrega e a worktree permanece, inclusive um arquivo gravado durante o PLAN e o trabalho das fatias anteriores. `_discard_untouched_workspace` e `_report_workspace_fate` nao removem esse diretorio.

Depois da worktree e antes de PLAN, o mesmo launcher recusa o interpretador fora do contrato do portao (USR-489): Python anterior a 3.12, modulo ausente em `requirements.txt` (incluindo `importlinter`, pacote `import-linter`) ou arquivo ilegivel. A saida e 2, PLAN nao comeca, e o stderr traz `host_preflight_failed` com o comando de correcao.

Antes dessa worktree e do PLAN, a admissao (`core.line.admission`) confirma rota de planning, de development e de review em outra familia, alem de um worker de gate com capacidade explicita (`workers` inteiro maior que zero, `busy` false e leitura com menos de 90 segundos). Falta de rota, capacidade zero, worker ocupado, leitura stale, colecao vazia ou health generico (`status=ok` sem esses campos) encerra com exit 75, JSON `route_wait` e o ticket em `planned`, sem criar worktree. Um `allow` nao substitui o `pick` de cada estagio. Com `--fleet`, a admissao de build tambem e headless e deixa Antigravity fora das caps de escrita.

### Wake do grokbox e comunicacao externa (USR-456)

No preflight, o launcher consulta a saude do grokbox. Se estiver indisponivel, registra a supressao com a causa `external_communication_not_authorized` e continua a execucao sem enviar email. Prompt, variavel de ambiente e outras opcoes nao concedem autorizacao. O envio exige o opt-in dedicado na propria chamada:

```powershell
python C:\dev\DarkFac\run_ticket.py USR-XXX --allow-grokbox-wake-email
```

Esse argumento autoriza somente o email de wake do grokbox. Envios autorizados continuam limitados a uma tentativa por janela compartilhada de 30 minutos entre worktrees; falha no estado do throttle ou no envio suprime o email sem interromper o launcher.

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
- **Fila ociosa (USR-501)**: host livre, rota de desenvolvimento e ticket elegivel (`select_tickets` ou `queue_remaining` do ciclo da frota), com `running_fleet_runs` e `in_flight` vazios, gravam `idle_since_epoch`. O achado `idle_queue` e o alerta no Hub e no Telegram (`NotificationService`, canal ALL) saem so depois de passar de 15 minutos (900s). A mesma chave de episodio nao reenvia. Com a condicao zerada, um episodio novo pode alertar. Os status `healthy`, `idle`, `degraded`, `stalled_no_route` e `stalled_in_flight` permanecem.
- **Amostra leve (USR-501 T2)**: dentro dos 2700s, `maybe_run_sentinel` nao chama `check_factory_status`, `pick` nem `read_quotas`. O relogio de 15 min avanca com o estado do despachante, a rota em cache e `running_fleet_runs`. O ticket elegivel dessa amostra sai de `queue_remaining`; estado ausente nao faz `git fetch` nem trata `pending_tickets` como fila. A sonda de 45 min e `--force` seguem com o relatorio de rotas e cotas.
- **Status ativo sem processo (USR-501 T3)**: `in_flight` de host local, `pending_delivery` com status `running` e reserva local com status `active` geram o achado `status_without_process` quando o ticket e o pid nao estao em `running_fleet_runs` e o heartbeat passou de 120s. Processo vivo ou heartbeat com ate 120s nao alerta. Run cujo host esta fora de grokbox, notebook e desktop nao entra na amostra. O alerta vai ao Hub e ao Telegram (`NotificationService`, canal ALL) uma vez por episodio. O sentinela nao grava reserva, entrega pendente, trava nem estado do despachante, e nao muda o status da linha. No briefing esse achado aparece como `active_without_process`.
- **Trava anterior ao merge (USR-501 T4)**: `line.lock` e `line.fleet-*.lock` geram o achado `stale_lock` quando `LineLockInfo.started_at` e anterior ao committer time do `merge_sha` em `merge.json` do mesmo ticket. Trava adquirida depois desse merge, trava sem merge resolvivel e merge de outro ticket ficam em silencio. Se a leitura de git falha, o mtime do arquivo nao substitui o committer time. O alerta vai ao Hub e ao Telegram (`NotificationService`, canal ALL) uma vez por episodio e nao apaga a trava.
- **Amostra de 15 minutos sem sonda de cota (USR-501)**: dentro da janela de 45 minutos, a amostra leve avalia `idle_queue`, o status ativo sem processo e `stale_lock` sem `pick` nem `read_quotas`. O briefing lista `idle_queue`, `active_without_process` e `stale_lock` quando esses achados estao presentes.

## Capacidade de escrita interativa no roteador (USR-436)

O roteador valida as capacidades com `agent_cli.supports(harness, mode)` antes de ranquear candidatos para o estagio `development`. Com `operating_harness=antigravity` em sessao interativa (fora do modo headless), o Antigravity declara capacidade de escrita para desenvolvimento no chat, viabilizando sua eleicao quando a cota estiver saudavel acima do piso de 15%. Em execucao headless, na frota ou com outro operador, Antigravity permanece estritamente somente-leitura.

Na recuperacao USR-452, uma falha real de teste com contagem no HARNESS_RESULT nao e tratada como worker indisponivel por conter essa mensagem no traceback. O launcher corrige o candidato antes de repetir o portao; nao chama o owner para resolver essa classificacao tecnica.

## Negação ao alterar o motor no chat (USR-494 T2)

Self-Modification, ou `permission denied` ao editar `core/line`, `core/harness`, `run_ticket.py` ou `scripts/line_validate.py`, grava `self_modification` e a tag `line-block` no control store, uma vez. O run não relança essa edição. `protected_path` e `permission denied` de banco ou ACL não entram nessa causa. A linha impressa é `[ERRO NA ENTREGA] causa: self_modification`. A fábrica abre uma ação do owner com um único comando, `python -m core.line.express_lane run --ticket <ID>`. Registrar de novo devolve a mesma ação aberta. O comando lista todas as falhas do host, inclusive o piso de cota, e só então executa o ticket.

## Diagnostico de holds por ticket (USR-484)

`python -m core.ops.briefing` inclui holds duraveis por ticket com a causa e a
quantidade de ocorrencias. Um hold de falha repetida fica restrito ao candidato
afetado; digest novo de candidato ou contexto o libera. O briefing tambem continua
mostrando o estado da recuperacao global separadamente, para distinguir um ticket
retido de uma frota em recuperacao serial.

## Recuperacao de CI sem repetir BUILD (USR-473)

CI vermelho permite uma correcao tecnica na mesma worktree. O orcamento fica em
`ci_recovery.json` e sobrevive a retomadas. Uma alteracao retorna codigo 75 e
retoma a entrega pelo portao completo e pela revisao independente do novo candidato;
nenhum push ou merge da correcao acontece antes dessas provas. Sem rota, a worktree
fica preservada para retomada automatica. Esgotar essa tentativa registra falha
tecnica em `validating`; nao solicita autorizacao de negocio ao owner.
`run_ticket.py --recovery --run-id <id>` ativa a reserva canonica de uma entrega global e usa a identidade recebida do dispatcher. O launcher verifica o gate antes de iniciar qualquer agente; `gate_busy` retorna como espera estruturada.

## Revisao sem outra familia disponivel

A sessao interativa e a frota usam a mesma cascata de revisao (USR-488): outra familia por harness, outra familia pelo OpenRouter, mesma familia com outro modelo e, por fim, o mesmo modelo com persona adversarial. O nivel usado fica registrado no veredito; so sem nenhum nivel a admissao espera. Detalhes em [roteamento](roteamento.md).
