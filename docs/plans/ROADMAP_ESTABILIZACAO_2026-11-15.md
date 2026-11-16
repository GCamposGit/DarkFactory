# Roadmap de estabilização do core (15/11/2026)

Pedido do owner em 15/11: a fábrica "corre atrás do rabo". Este roadmap conserta o core e **tem prioridade sobre qualquer feature, produto, segurança ou piloto** até as frentes E1 a E5 estarem em produção. Foi escrito para ser retomado pela própria fábrica, por modelos mais leves: cada frente diz o estado, onde está o trabalho e o próximo passo.

## Diagnóstico (com números)

1. **A fila se autoalimenta.** Dos 55 tickets abertos em 15/11, cerca de 40 eram a fábrica consertando a si mesma, cada um pelo pipeline pesado inteiro. A regra "todo defeito vira ticket" gerou seis tickets novos em dois dias a partir de falhas da própria esteira.
2. **Recusar em vez de degradar.** Cota desconhecida vale zero; a admissão exigia rota de planejamento, build, revisão de outra família e portão livre antes de começar; o portão espera 30 min por 4 GiB e sai com `gate_insufficient_ram`. Entre 09 e 11/11 foram 481 esperas de admissão (374 `review_same_family`, 68 `gate_capacity_zero`, 39 `no_planning_route`).
3. **Falha sem classe e sem orçamento.** 357 de 426 falhas gravadas eram `error` genérico (o motivo era o fim do log). O cooldown vivia em memória e sumia a cada reinício do despachante; o hold exigia três ocorrências do mesmo fingerprint, que muda a cada ciclo (USR-469: 19 fingerprints, 28 ocorrências, nunca em hold). Desde 04/11: 2.980 lançamentos falhos em 95 tickets.
4. **Suíte inteira no lugar errado.** O portão de cada ticket rodava os ~5.100 testes no Notebook (48 min, RAM livre abaixo de 4 GiB). No CI Linux do GitHub a mesma suíte leva 2 min, e o repositório é público: Actions é gratuito e ilimitado. Com um worker o runner ainda abria xdist (`-n auto`) e o worker morria (`EOFError`).
5. **DarkHub com cinco fontes de verdade.** O retrato do GitHub descartava o estado vivo do Postgres (o Hub mostrava cinco OAs já fechadas e omitia duas novas); a tarefa que publica cotas estava desabilitada desde 06/11 e dependia de sessão interativa.
6. **Custo sem dono.** 83% dos runs de telemetria (88,5 M tokens) não têm `ticket_id`. A contingência OpenRouter usava o modelo mais caro e registrou 450 execuções que não editaram nenhum arquivo; o teto mensal só alertava (US$ 18,32 gastos contra US$ 10).

## Princípios (valem para todo ticket deste roadmap)

- **Degradar, não recusar.** Todo ponto de recusa tem uma alternativa mais barata ou mais lenta. Esperar é o último degrau, com `not_before` curto.
- **Infra não é culpa do candidato.** Falha de infra espera com backoff persistido e não chama modelo. Falha do candidato gasta 1 de 3 tentativas por ticket; ao estourar, hold terminal e um único alerta ao owner.
- **Nenhum ticket novo nasce de falha da esteira.** A falha fica no orçamento do próprio ticket; o que for defeito do core entra neste roadmap.
- **Teste no lugar certo.** Ticket: testes afetados e contratos, local e em série se preciso. Suíte completa: CI Linux do GitHub. Windows: push no `main` e noturno.
- **Uma fonte de verdade por dado**, com frescor visível. Erro explícito em vez de dado velho ou vazio.
- **Saldo de código.** Cada frente apaga mais do que cria quando possível.

## Frentes

| Frente | Ticket | Estado em 15/11 | Onde está |
|---|---|---|---|
| E1 Rotas degradam com cota baixa ou desconhecida | USR-504 | **Entregue** (PR #861, em produção em 15/11) | falta ligar `plan_exists` no launcher (ver E5) |
| E2 Falha classificada, orçamento durável, backoff persistido | USR-490 | PR #862 (mesclar com CI verde) | branch `fix/falha-classificada-orcamento` |
| E3 Portão elástico: ticket roda afetados, suíte no CI | USR-502 | **Entregue** (PR #863) | `main` |
| E4 DarkHub: fonte única e frescor | USR-513 | **Entregue** (PR #859, verificado em produção em 15/11) | `main` |
| E5 Retomar na etapa certa | USR-447 | Não iniciado | ver abaixo |
| E6 Dieta do pipeline de entrega | USR-499, USR-500 | Não iniciado | ver abaixo |
| E7 Custo e contingência | USR-445, USR-512 | Não iniciado | ver abaixo |
| E8 Hosts e reconciliação | USR-498, USR-492 | Não iniciado | ver abaixo |
| E9 Poda da suíte | (sem ticket) | Auditoria incompleta | ver abaixo |

Como entregar cada frente restante: worktree nova a partir de `origin/main`, rodar só os arquivos de teste alterados (`python -m pytest <arquivos> -q -n 0 -p no:cacheprovider`), depois `lint-imports`, `python scripts/complexity_ratchet.py`, commitar, `python -m core.harness.docs_sync --base origin/main --head HEAD`, `git push` e `gh pr create`. O CI do PR roda a suíte em Linux em cerca de 3 minutos; mesclar com squash quando verde. Não rodar `runner.py --quick` no Notebook.

Lições desta rodada, para quem retomar:
- Mudou código de saída do launcher? Rode também `tests/test_run_ticket*.py`, `tests/test_ticket_workspace.py`, `tests/test_line_fleet_execution.py`: vários testes fixam o código de saída.
- Timeout de agente é falha do **candidato** (gasta orçamento), nunca infra: é inferência paga.
- Teste novo com relógio precisa de tolerância: o shard Windows do `main` reprovou por 200,00000000000006 contra 200.
- Antes de `git checkout` em uma worktree, confirme que a pasta existe; se não existir, o comando roda na pasta atual.

### E1 Rotas (USR-504)
Comportamento alvo: ordem de escrita é assinatura com cota conhecida acima de 15%, depois assinatura com cota desconhecida, depois OpenRouter barato dentro do teto, e só então esperar. Leitura velha usa a última válida com margem de 10 pontos até o reset (todos os provedores). Leitura "suspeita" repetida duas vezes é aceita; antes disso vale a mais pessimista. Planejamento aceita modelo intermediário quando não há alto nível. Teto mensal do OpenRouter bloqueia de verdade (`run_caps.openrouter_monthly_cap_usd`; 28,13 em novembro pela decisão OA-090, **voltar a 10,0 em 01/12/2026**). Admissão exige só rota de desenvolvimento (e de planejamento quando não há plano); revisão e portão decidem na hora deles.
Arquivos: `core/line/routing.py`, `core/line/admission.py`, `core/usage/history.py`, `core/usage/adapters.py`, `core/line/review_cascade.py`, `.factory/config/line_routing.json`.

### E2 Falhas (USR-490)
Comportamento alvo: `core/line/failure_taxonomy.py` classifica causa em infra, candidato, owner ou desconhecida. O launcher grava `outcome.json` por tentativa e sai com 75 em toda causa de infra; o despachante lê esse arquivo, não o fim do log. Tabela `ticket_budget` na admission store: infra aplica backoff de 10 min dobrando até 2 h, persistido; candidato conta por ticket e na terceira falha vira hold com um alerta. Conserto de CI e revisão esgotada contam no mesmo orçamento. A sentinela não diz `healthy` quando todos os pendentes estão em hold ou backoff.
Pendência conhecida: o agente parou ao tratar "espera sem rota não pode consumir a correção de CI disponível". Conferir esse caso em `run_ticket.py` antes do PR.

### E3 Portão (USR-502)
Comportamento alvo: `_gate_build_command` usa `runner.py --profile pr` local (afetados, compileall, tipos, fumaça, `lint-imports`, ratchet). `DARKFAC_GATE_SCOPE=full` restaura a suíte completa. Um worker usa `-n 0`. Mínimo de 1,5 GiB livres para afetados em série; espera de RAM cai de 1800 s para 300 s. Queda de worker xdist reexecuta uma vez em série. Remoto inalcançável falha em 5 s e cai para local.

### E4 DarkHub (USR-513)
Entregue e verificado em 15/11: `/api/owner-actions` lê do Postgres (2 abertas, 52 fechadas) e `/api/usage/accounts` recebe as cotas por `core.usage.sync_task.publish_once()`, sem sonda paga. O PR #862 liga essa publicação no ciclo do despachante (a cada 5 min). Resta: remover a tarefa agendada `DarkFac-Usage-Cloud-Sync` (desabilitada desde 06/11) depois de um dia com o despachante publicando.

### E5 Retomar na etapa certa (USR-447; absorve parte do USR-475)
Problema: o despachante sempre relança `run_ticket.py <ID>` completo. `_stage_plan` chama `ticket_engine.reset`, que apaga o plano; `_can_resume_built_candidate` exige que `build_binding.json` bata com o HEAD, e qualquer commit do agente de conserto invalida o vínculo. Resultado: replanejamento com modelo de alto nível e novo build para um candidato que só precisava repetir portão ou entrega.
Fazer: (0) `evaluate_admission` em `run_ticket.py` passa `plan_exists=True` a `admission.admit` quando a worktree preservada do ticket já tem `SPEC.md` e `tickets.json`; (1) se a worktree preservada tem commits à frente do `origin/main` e plano válido, o despachante lança `--resume-delivery --worktree <caminho>` (mesmo caminho de `pending_delivery`); (2) regravar o vínculo de build após cada commit do agente de conserto; (3) `reset` nunca apaga plano com candidato existente; (4) BUILD retoma só as fatias que faltam. Fechar o PR #845 se o trabalho for refeito.
Aceite: teste de comportamento em que falha de portão seguida de relançamento não chama planejamento nem build.

### E6 Dieta do pipeline de entrega
- USR-499: entregas param de escrever histórico por ticket em `docs/FASE_ATUAL.md` (causa recorrente de conflito de rebase); ajustar a regra do docs-sync.
- USR-500: limpar a worktree ao encerrar o ticket e varrer diariamente (há mais de 100 worktrees acumuladas, incluindo ~40 `line-validate-wt-*` no diretório temporário).
- Learning pack deixa de bloquear fim de turno e entrega: é gerado automaticamente na entrega (`python -m core.learning_pack.delivery`); o hook `scripts/stop_hook_artifacts.py` só avisa. Absorve USR-522.
- Deploy: prova de SHA em execução basta para seguir ao backup (USR-518); entrega direta deixa pedido de deploy consumível (USR-519).
- **Ação do owner (OA):** a seção 7 do `AGENTS.md` ("todo defeito vira ticket") alimenta a avalanche. Proposta: defeito do core entra como item deste roadmap; ticket só para defeito de produto. `AGENTS.md` é protegido e exige assinatura.

### E7 Custo e contingência
- USR-445: o modo de escrita via OpenRouter não recebe o conteúdo real da worktree (450 execuções sem edição). Sem isso a contingência paga não funciona. Enviar os arquivos relevantes no prompt com limites e exigir patch aplicável; falha de contexto gera causa `context` (infra), sem repetir chamada paga.
- USR-512: a cascata de revisão pode devolver harness e modelo incompatíveis (modelo de uma família pelo CLI de outra). Validar o par em `core/line/review_cascade.py`.
- Propagar `DARKFAC_TICKET_ID` ao registro de telemetria e aplicar `run_caps.agent_calls` por ticket no launcher (era parte do USR-480).
- USR-458: alias do modelo leve da sonda do Claude não reconhecido pelo CLI.

### E8 Hosts e reconciliação
- USR-498: status `implementing` ou `validating` sem processo vivo nem reserva volta a `planned` no ciclo do despachante.
- USR-492 (em validação): ordem Notebook, Grokbox oportunista, Desktop.
- Com o portão local em perfil `pr` (E3), "host" passa a ser só uma vaga de concorrência no Notebook. Simplificar `default_probe` e `default_launch` em `core/line/fleet_dispatcher.py`: host remoto fora do ar não bloqueia ticket; dimensionar as vagas pela RAM livre (uma vaga a cada ~3 GiB livres) em vez de três fixas.
- Menores, reavaliar depois de E2: USR-454, USR-455, USR-457, USR-463, USR-471, USR-485, USR-487, USR-491, USR-448 (remover a tarefa agendada que aponta para módulo inexistente; fechar PR #822), USR-486 (PR #847), USR-496 (PR #840), USR-516.

### E9 Poda da suíte
A auditoria de testes não terminou. Roteiro: (1) `python -m pytest --collect-only -q` para contar por arquivo; (2) `--durations=50` no CI para achar os lentos; (3) apagar testes de forma (presença de texto em script ou doc, como o que quebrou no PR #859) e testes de módulos que o motor não importa (`core/benchmarks`, `core/visual`, `core/content`, `core/archetypes`: confirmar com `lint-imports` e grep antes); (4) meta da fase: até 4.000 testes.

## Fila em 15/11

- Cancelados por obsolescência, duplicidade ou absorção: USR-453, 459, 460, 464, 465, 466, 467, 469, 472, 481, 482, 495, 503, 506, 507, 511, 521, 523.
- Congelados (tags `not-authorized-for-execution` e `congelado-core-20261115`), fora do foco: USR-446, 450, 451, 462, 470, 476, 480, 483, 505, 514, 517, 518, 519, 520, 522.
- PRs a fechar: #821, #822, #844, #845 (se E5 refizer), #846 (feature congelada), #825.
- Para descongelar: remover as duas tags do ticket no control store, só depois de E1 a E5 em produção e cinco entregas seguidas sem resgate.

## Ordem de execução

E2 (mesclar #862) → religar o despachante e observar um dia → E5 → E8 (USR-498 e vagas por RAM) → E7 (USR-445) → E6 → E9.
