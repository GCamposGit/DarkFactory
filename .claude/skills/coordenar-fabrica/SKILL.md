---
name: coordenar-fabrica
description: Assuma uma sessão de coordenação da DarkFac ou de um produto adotado quando o operador pedir “assuma a fábrica”, “coordene este projeto” ou a retomada da coordenação. Conduza fila, delegação por etapa, recuperação de falhas e aprendizagem entre harnesses. Um executor com uma fatia atribuída mantém seu papel.
---

# Coordenar a fábrica

## Assumir o turno

Para o pedido curto, rode `python -m core.ops.coordination` a partir do runtime. Quando o operador pedir para ler esta skill explicitamente, rode `python -m core.ops.coordination --entry skill-file`. Se a identidade não puder ser detectada, informe o harness nesse comando com a opção --harness. Na raiz de estado `DARKFAC_STATE_ROOT`, leia `last_receipt.json` no diretório `coordination` antes de qualquer despacho e siga a recuperação registrada nele.

1. Identifique o harness, o papel do processo e o projeto pelo registro da fábrica, checkout, missão e ticket. Em produto adotado, use o contrato e o gate do produto. Se a identidade conflitar, consulte o registro antes de qualquer despacho. Reserve `darkfac` para trabalho da própria fábrica.
2. Use os comandos Python a partir do runtime da DarkFac. Para aprender em um produto, confirme que DARKFAC_PROJECT_ROOT e DARKFAC_STATE_ROOT apontam para o projeto e seu estado autorizado nas duas sessões. Na raiz da DarkFac, leia `MISSION.md`, a fase e o plano vigentes em `docs/FASE_ATUAL.md`. No produto, leia também sua missão e roadmap. Execute `python -m core.ops.briefing` e `python -m core.learning.cli prime --domain coordenacao`. O comando prime sintetiza regras e preferências. Para recuperar intervenções ainda sem regra, leia os turnos recentes com target_skill igual a coordenar-fabrica no overlay vivo (arquivo learning_ledger.live.json em learning/, na raiz de estado daquele projeto; se não existir, na semente learning_ledger.json do mesmo diretório). Se o estado for de outro host, use o handoff versionado do projeto e os recibos compartilhados. Confronte o handoff do projeto (na fábrica, `docs/operacao/HANDOFF.md`) e os incidentes dos últimos runs, inclusive de outros harnesses, com reservas, PRs, evidências e política de frota atuais. Uma fonte indisponível deixa aquela parte do estado desconhecida.
3. Identifique objetivo, trabalho em voo, próximo item elegível, capacidade e modo efetivo do despachante. Comunique a primeira **ação tomada** e seu identificador. Se a entrega já tem executor, acompanhe a reserva ou o PR existente. Preserve a precedência de recuperação e o modo `resume-only`.

## Fábrica parada vem antes de trabalho novo

O recibo lista em `stalls` o que impede a fila de andar. Trate cada item antes de admitir ou planejar outra demanda.

| Sinal | Ação |
|---|---|
| `deploy_convergence_exhausted` | `DARKFAC_STATE_ROOT/runs/deploy_deferrals.json` registra `recovery_exhausted`. Leia as linhas `[NODE SYNC]` para o sha e o nó pendente, limpe a causa e execute `python C:\dev\DarkFac\scripts\dokploy_redeploy.py`. Se `delivery_wait` mantiver o deploy em voo, preserve o run e priorize essa ação de convergência em `first_action` antes de admitir trabalho novo. |
| `dispatcher_blocked_with_free_host` | A frota tem host livre e nada executável. Leia `skipped` e `idle_reasons` em `fleet_dispatcher_state.json`, no diretório `runs` da raiz de estado, e resolva a causa dominante. |
| `ticket_pr_open` com CI verde | Revise com família diferente da do implementador, promova o rascunho (`gh pr ready`) e mescle em série. Uma recusa repetida "já possui PR ativo" (`refusal_loop`) é laço, não espera. |
| `ticket_pr_open` com CI vermelho ou conflito | Delegue a correção ou o rebase com escopo do PR e aceite do ticket. |
| `hold_already_delivered` | Confira o aceite contra o diff mesclado e feche com o comando `complete` da seção Fechamento. |
| `hold` | O hold só sai com merge comprovado. Delegue a triagem com leitura econômica. Para cada ticket, o subagente traz o achado da última revisão ou do log, o trabalho pronto e a recomendação: mesclar, retrabalho delimitado, fechar como superado ou investigação. Um defeito da esteira repetido em vários tickets vira um ticket próprio. |
| `source_unavailable` | Aquela parte do estado fica desconhecida. Não conclua que a fila está vazia. |

## Conduzir uma entrega

| Etapa | Ação do coordenador e evidência para avançar |
|---|---|
| Entrada e projeto | Resolva o `project_id` explicitamente. Procure demanda equivalente. Registre objetivo, problema e aceite observável pelo `run_ticket.py`. Para ambiguidade material de negócio, siga o Grill e vincule a decisão do owner ao ticket. Enquanto aguarda, prossiga com outro item elegível. |
| Planejamento e pesquisa | Delegue a leitura de contexto e pesquisa necessária com escopo, pergunta e fonte esperada. Peça um plano em fatias verificáveis e declare dependências reais. |
| Desenvolvimento | Use o launcher e o roteador por etapa, com registro de modelos, capacidade de escrita, privacidade, orçamento e cota efetiva. Cada executor recebe projeto, ticket, worktree, escopo e aceite. Paralelize fatias independentes dentro da capacidade admitida. Subagentes do chat consomem sua cota. Quando pressionada, use a frota. Trabalho aberto fora da esteira segue o mesmo fluxo: PR pronto para revisão, nunca rascunho esquecido. |
| Validação e revisão | Deixe o harness executar as verificações no candidato. Delegue a triagem de logs a um subagente econômico e peça causa, trecho de evidência e ação. Confira as checagens executadas e o marcador de sucesso. Siga a cascata de revisão com preferência por família independente. |
| Integração e produção | Siga o contrato do projeto para PR, CI, merge, deploy, verificação real e aceite aplicável. Vincule evidência ao candidato. No deploy da fábrica, o código de saída diferente de zero pode vir só da convergência de um nó. Leia as linhas `[RUNTIME IMAGE]` por serviço e relate cada um. Para verificar saúde, use o runbook de canário e as evidências de backup e restauração. |
| Fechamento | Antes de concluir, compare cada critério de aceite com o diff mesclado. Um PR que cobre só parte deixa o ticket aberto, com a lacuna na evidência. Para entrega comprovada fora do launcher, use `python -m core.demands.cli complete <TICKET> --sha <sha no origin/main> --evidence "<PR, revisão e verificação>"`, que recusa sha fora do main e libera o hold. Registre resultado, custo disponível, learning pack, pendências e próximo responsável. Reavalie a fila no mesmo turno. Ao passar o bastão, confirme reserva/run em voo ou espera persistida e despachante ativo. Sem trabalho elegível, deixe a espera com o serviço e descreva o evento que a encerra. |

Para registrar sem iniciar um build, use `python run_ticket.py --create --queue-only --project <id> --title "<título>" --problem "<problema e evidência>" --criteria "<resultado verificável>"`. Confira o ID e releia o ticket. Ao priorizar, confira a ordem que o despachante reconhece e informe a diferença se só for possível registrar a intenção. Preserve o significado das faixas de bloqueio da linha e recuperação.

## Reagir a incidentes

- **Causa raiz antes do remendo.** Nomeie a classe do defeito, isto é, o que gera novas ocorrências. Para lentidão, meça com perfil antes de propor a correção. Confira no caminho de produção, de ponta a ponta, que o mecanismo realmente é acionado. Uma correção que só lista a ocorrência observada não fecha um defeito recorrente.
- **Correção delimitada.** Corrija na própria sessão, num PR só, todo defeito pequeno ou médio com causa conhecida. Não abra um ticket por sintoma: cada ticket vira um run de frota que gera novos erros. O que não couber vai para **um** ticket consolidado por sessão, com reprodução, evidência e aceite de cada item. Um erro do produto segue para o projeto do produto. Uma falha do runtime compartilhado segue para a DarkFac com vínculo ao trabalho atingido.
- **Cota baixa.** Faça o preflight conforme `AGENTS.md` e peça ao roteador outra rota elegível para a etapa. A contingência paga depende do saldo e do orçamento autorizados. Preserve o checkpoint. Sem rota, confirme `route_wait` persistido e o mecanismo de retomada.
- **Cota desconhecida.** Confira a idade e a origem da amostra. Use `python -m core.usage.cli accounts --refresh`, confira se o provedor publicou leitura nova e deixe a decisão de elegibilidade com o roteador. Se persistir, reutilize ou abra um reparo por provedor e causa. O aceite exige leitura atual utilizável pelo roteador. Outra rota elegível pode reparar enquanto os demais tickets seguem.
- **Falha repetida ou host indisponível.** Consulte causa estruturada, heartbeat, reserva, PR e último checkpoint. Use a retomada da etapa e o backoff existentes. Se o mesmo erro reaparecer sem progresso, isole o ticket e registre o reparo. Só leve ao owner decisões, segredos, aprovações ou aceites que dependam dele, pelo canal oficial.
- **Checkout compartilhado.** O `record-turn` altera o ledger de aprendizagem rastreado e o nó passa a ser visto como sujo. Para avançar o checkout, guarde só esse arquivo num stash nomeado, faça o fast-forward e reaplique o stash pela referência (pendência USR-535).

## Aprender a cada interação

Toda mensagem do operador na sessão gera uma observação curta. Classifique o motivo como objetivo novo, decisão legítima, preferência, correção ou intervenção que a fábrica poderia ter evitado. Registre com `python -m core.learning.cli record-turn --skill coordenar-fabrica --project "<projeto do recibo>" --harness "<harness do recibo>" --run-id "<run associado>" --cause "<motivo>" --identity "<identificador estável da interação>" --prompt "<resumo sanitizado>" --intent "<intenção>"`. Para uma correção ou continuação, inclua --followup no comando record-turn e mantenha a mesma identidade ao repetir o registro daquela interação. Preserve a privacidade do projeto, incluindo nos registros compartilhados. Se a escrita estiver indisponível, deixe a observação no handoff autorizado para reconciliação.

Quando a coordenação poderia ter evitado a intervenção, confronte esperado e observado com o run, a etapa e a evidência anterior. Considere também falhas de outros harnesses mesmo que o operador nada tenha dito. Siga a skill `continuous-self-improvement` para consolidar ocorrências pela causa, registrar preferência ou RCA comprovada e aplicar o menor ajuste verificável à fonte canônica. Use um ticket da evolução quando faltar investigação ou mudança de componente. Preserve decisões pontuais no escopo original. A próxima sessão testa o ajuste em caso independente e registra se o mesmo trabalho avançou com menos intervenção. Uma lição pendente identifica o próximo responsável e a evidência que falta.

## Onde consultar

Caminhos a partir da raiz da DarkFac. Abra conforme a ocorrência.

- Projeto e entrada: `docs/PROJECT_ADOPTION.md`, `docs/manual/grill.md`, `docs/manual/fila.md`.
- Roteamento, custo e execução: `docs/manual/roteamento.md`, `docs/manual/custo.md`, `docs/manual/frota.md`, skill `run-ticket`.
- Validação e entrega: `docs/manual/portao.md`, `docs/manual/merge_autonomo.md`, `docs/runbooks/dokploy_redeploy.md`, `docs/runbooks/canary.md`.
- Continuidade: `docs/manual/operacao_interativa.md`, `docs/runbooks/owner_actions.md`, `docs/manual/learning_pack.md`. Antes do encerramento, atualize o handoff com projeto, etapa, evidência, incidente e próxima ação e execute `python -m core.ops.turn_guard --check`.
