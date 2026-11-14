<!-- covers: core/learning_pack/delivery.py -->
# Learning pack da entrega

Gerado por codigo, a partir do diff `base..head`, dos testes alterados e do ADR (se houver). Tem dois niveis: leigo (o que mudou, em linguagem simples) e dev (arquivos, funcoes, testes).

Uso:

```powershell
python -m core.learning_pack.delivery --base origin/main --head HEAD --ticket USR-XXX --out C:\caminho\saida
```

Gera `<ticket>.json` (schema Pydantic `LearningPackArtifact`) e `<ticket>.md`.

Verificacao: se um redator (`llm`) for injetado em `build_for_delivery`, cada frase que cita caminho ou simbolo e conferida contra o diff e a arvore do head. Frase com arquivo inexistente, arquivo nao alterado citado como alterado ou simbolo inexistente e removida e registrada em `rejected_claims`. Sem `llm`, o texto vem so dos fatos.

## Hook Stop (USR-401)

`scripts/stop_hook_artifacts.py`, registrado em `.claude/settings.json` (hooks.Stop), impede o Claude Code de encerrar quando o diff contra `origin/main` (ou a worktree suja) toca `core/`, `hub/`, `scripts/`, `tests/` ou `run_ticket.py` e a branch/ultimo commit cita `USR-NNN`, sem `.factory/learning_packs/USR-NNN.md` (ou `.json`). Bloqueia com exit 2 e o comando de geracao em stderr. Com `stop_hook_active` ou falha inesperada (git ausente etc.) libera com exit 0.

## Aprendizado da entrega USR-456 T2

O preflight continua consultando a saúde do grokbox, mas o wake por email só ocorre com autorização
explícita propagada pelo launcher. Worktrees ligadas ao mesmo checkout compartilham o estado em
`.factory/grokbox_wake/state.json`; `DARKFAC_STATE_ROOT` explícito mantém seu próprio destino. Um lock
entre processos protege a decisão, reserva antes do envio e gravação do timestamp. Assim, até respostas
ambíguas do provedor ficam limitadas a uma tentativa por janela de 30 minutos. Causas estruturadas de
supressão incluem `external_communication_not_authorized`, `throttle_active`,
`shared_state_root_unavailable`, `state_unreadable`, `state_write_failed` e `lock_unavailable`; essas
falhas não interrompem a sondagem nem o launcher.

## Aprendizado da entrega USR-474 T2

O OpenRouter não abre arquivo. A revisão paga só vale se o prompt do `generate` já trouxer os critérios, o trecho sanitizado e os hashes do manifesto. A SPEC sanitizada aparece uma vez. Sem SPEC ou sem tickets, a esteira devolve `review_context_unavailable` e não chama o modelo. A resposta precisa estar completa: saída no teto de 4096 tokens ou `finish_reason` `length` devolve `review_output_truncated` e não aprova. O `ticket_id` sai do `run_id` quando ele é o ticket, ou de `DARKFAC_TICKET_ID`; sem id o evento continua nulo. Prompt e resposta não viram identidade. A resposta normalizada guarda `finish_reason`, `completion_tokens` e `truncated`. Família independente e `validation.json` ficam como estavam. Quem tem filesystem continua com o diff integral e o `candidate.diff`.

## Aprendizado da entrega USR-474 T4

O contrato documentado para OpenRouter exige contexto verificável no próprio payload: critérios, conteúdo sanitizado e manifesto com hashes. Falta de contexto encerra a revisão com `review_context_unavailable`; saída incompleta encerra com `review_output_truncated`. Na recuperação da frota, a lista de tickets é um único argumento separado por vírgulas, como `--recovery-tickets "USR-1,USR-2"`. Consulte `--help` antes de executar; IDs posicionais separados são recusados antes do ciclo. Este registro descreve a documentação do contrato e não afirma recibos de gate, CI, merge ou deploy.

## Aprendizado da entrega USR-474 T1

A revisão precisa do texto da SPEC, dos critérios e do conteúdo dos arquivos, com um manifesto que dá para conferir. `build_review_payload` lê esses arquivos no disco, redacta segredos antes de cortar e registra o sha256 da fonte, quantos bytes entraram e se o texto foi cortado. Os arquivos alterados saem em ordem de caminho. Os limites contam bytes UTF-8 e não partem um caractere. Se a SPEC ou os tickets faltam, vêm vazios, inválidos ou não dá para ler, o payload fica incompleto. Uma SPEC cortada até sobrar só espaço em branco também não fica pronta. Contexto vazio não é tratado como pronto. O `line_validate` também deixa de herdar `DARKFAC_PROJECT_ROOT`: o portão do snapshot mede a árvore que acabou de montar.

## Aprendizado da entrega USR-468 T1

Antes de criar a worktree ou rodar o PLAN, `run_ticket.py` pede uma admissão somente leitura em
`core.line.admission`. Ela confirma rota de planning, de build e de revisão em outro harness, e um
worker de gate com capacidade explícita. Se faltar rota, ou se o gate estiver em zero, ocupado, stale,
vazio ou só com health genérico, o processo sai com código 75, JSON `route_wait` e o ticket segue
`planned`, sem agente e sem worktree. Um allow não guarda a rota: cada estágio chama `pick` de novo.

### USR-468 T3 — Recovery com uma entrega por vez

No modo recovery, a esteira trabalha como uma fila de faixa única: o SQLite mantém o latch e `AdmissionStore.reserve_recovery()` guarda a reserva global entre reinícios. O gate continua sendo o `suite_lock` oficial. Espera por gate, host ou rota não conta como sucesso; recovery só fecha quando `verify_recovery_receipt()` aprova um receipt recente, ligado ao candidato e ao `run_id`, com gate, revisão independente, CI, merge, deploy, smoke e learning pack comprovados.

## Aprendizado da entrega USR-484 T5

Uma lista `--recovery-tickets` ordena a prioridade, mas só serializa quando o recovery
global está ativo. Com `recovery.active=false`, a fila regular continua usando os hosts
livres, respeitando reservas e `suite_lock`. `last_cycle.recovery_list` mostra itens
`pending` e a causa; a lista termina ao ficar toda terminal ou ao expirar, e
`recovery_list_no_admissible` informa que nenhum item pode avançar sem parar a fila.
Um candidato preservado com sinal confiável de desenvolvimento concluído retoma pela
entrega, sem novo PLAN ou BUILD. Causas repetidas de gate ou agente podem reter apenas o
ticket: `ticket_hold` fica visível no ciclo e no briefing, e só um digest novo de candidato
ou contexto libera. O runner aponta caminhos sujos em `[DIRTY_PATHS]`, preservando a linha
canônica de erro usada pelo classificador. Esta lição registra o comportamento operacional,
sem afirmar CI, merge ou deploy.

## Aprendizado da entrega USR-494 T1

A tag `line-block` é a via expressa da frota. O despachante lê só essa tag no ticket `planned`:
título, campo extra ou tag parecida não furam a fila. Quem está elegível sai no primeiro host
livre, antes da lista `--recovery-tickets` e do restante. Tag excluída, dependência aberta e
cooldown continuam de fora. Com `recovery.active=false`, os hosts seguintes seguem a lista e a
fila regular. Com `recovery.active=true`, há um único slot: o `line-block` o ocupa se ninguém
está em voo, e não abre outra entrega se já houver uma. O lançamento segue
`run_ticket.py <id> --fleet --host-id <host>`. Portão, revisão e piso de cota não mudam. Esta
lição não afirma CI, merge ou deploy.

## Aprendizado da entrega USR-494 T2

Negar a edição do motor no chat manda o ticket para a via expressa. Self-Modification, ou
permission denied em `core/line`, `core/harness`, `run_ticket.py` ou `scripts/line_validate.py`,
grava a causa `self_modification` e a tag `line-block` no control store. Repetir a gravação não
duplica tag nem causa. `protected_path` e permission denied de banco ou ACL ficam nas causas
deles. O run que recebeu a negação não relança a mesma edição. A frota lê `self_modification` na
linha `[ERRO NA ENTREGA] causa:`, não numa menção perdida na cauda do log. Esta lição não afirma
CI, merge ou deploy.

## Aprendizado da entrega USR-494 T3

A causa `self_modification` entrega um único comando ao owner:
`python -m core.line.express_lane run --ticket <ID>`. A ação aberta guarda só esse
comando; registrar de novo devolve a mesma ação. O comando lê o host inteiro e
imprime cada requisito ausente, inclusive o piso de cota de 15%. Com falha, não
lança o ticket. Com o host válido, lança uma vez
`run_ticket.py <id> --fleet --host-id <host deste host>`. Ticket já em voo ou
terminal termina sem criar outro processo. Portão, revisão e piso do lançamento
não mudam. Esta lição não afirma CI, merge ou deploy.

## Aprendizado da entrega USR-494 T4

A segunda falha classificada `self_modification` do mesmo ticket avisa o owner
uma vez. A primeira só grava o episódio. O aviso sai no canal ALL com a causa
e o comando `python -m core.line.express_lane run --ticket <ID>`. Repetir o
mesmo episódio não manda outro aviso. A chave é o ticket e a causa: a cauda do
log não abre episódio. Entrega concluída, ou outra causa classificada, encerra
o episódio e uma repetição nova pode avisar uma vez. O aviso não liga
`recovery.active`. O hold do ticket segue o limiar que já estava configurado.
Esta lição não afirma CI, merge ou deploy.
