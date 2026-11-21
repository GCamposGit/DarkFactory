---
name: coordenar-fabrica
description: Assuma uma sessão de coordenação da DarkFac ou de um produto adotado quando o operador pedir “assuma a fábrica”, “coordene este projeto” ou a retomada da coordenação. Conduza fila, delegação por etapa, recuperação de falhas e aprendizagem entre harnesses. Um executor com uma fatia atribuída mantém seu papel.
---

# Coordenar a fábrica

## Assumir o turno

1. Identifique o harness, o papel do processo e o projeto pelo registro da fábrica, checkout, missão e ticket. Em produto adotado, use o contrato e o gate do produto. Se a identidade conflitar, consulte o registro antes de qualquer despacho. Reserve `darkfac` para trabalho da própria fábrica.
2. Use os comandos Python a partir do runtime da DarkFac. Para aprender em um produto, confirme que DARKFAC_PROJECT_ROOT e DARKFAC_STATE_ROOT apontam para o projeto e seu estado autorizado nas duas sessões. Na raiz da DarkFac, leia `MISSION.md`, a fase e o plano vigentes em `docs/FASE_ATUAL.md`. No produto, leia também sua missão e roadmap. Execute `python -m core.ops.briefing` e `python -m core.learning.cli prime --domain coordenacao`. O comando prime sintetiza regras e preferências. Para recuperar intervenções ainda sem regra, leia os turnos recentes com target_skill igual a coordenar-fabrica no learning/learning_ledger.json da raiz de estado daquele projeto. Se o estado for de outro host, use o handoff versionado do projeto e os recibos compartilhados. Confronte o handoff do projeto (na fábrica, `docs/operacao/HANDOFF.md`) e os incidentes dos últimos runs, inclusive de outros harnesses, com reservas, PRs, evidências e política de frota atuais. Uma fonte indisponível deixa aquela parte do estado desconhecida.
3. Identifique objetivo, trabalho em voo, próximo item elegível, capacidade e modo efetivo do despachante. Comunique a primeira **ação tomada** e seu identificador. Se a entrega já tem executor, acompanhe a reserva ou o PR existente. Preserve a precedência de recuperação e o modo `resume-only`.

## Conduzir uma entrega

| Etapa | Ação do coordenador e evidência para avançar |
|---|---|
| Entrada e projeto | Resolva o `project_id` explicitamente. Procure demanda equivalente. Registre objetivo, problema e aceite observável pelo `run_ticket.py`. Para ambiguidade material de negócio, siga o Grill e vincule a decisão do owner ao ticket. Enquanto aguarda, prossiga com outro item elegível. |
| Planejamento e pesquisa | Delegue a leitura de contexto e pesquisa necessária com escopo, pergunta e fonte esperada. Peça um plano em fatias verificáveis e declare dependências reais. |
| Desenvolvimento | Use o launcher e o roteador por etapa, com registro de modelos, capacidade de escrita, privacidade, orçamento e cota efetiva. Cada executor recebe projeto, ticket, worktree, escopo e aceite. Paralelize fatias independentes dentro da capacidade admitida. Subagentes do chat consomem sua cota. Quando pressionada, use a frota. |
| Validação e revisão | Deixe o harness executar as verificações no candidato. Delegue a triagem de logs a um subagente econômico e peça causa, trecho de evidência e ação. Confira as checagens executadas e o marcador de sucesso. Siga a cascata de revisão com preferência por família independente. |
| Integração e produção | Siga o contrato do projeto para PR, CI, merge, deploy, verificação real e aceite aplicável. Vincule evidência ao candidato. Para verificar saúde, use o runbook de canário e as evidências de backup e restauração. |
| Fechamento | Registre resultado, custo disponível, learning pack, pendências e próximo responsável. Reavalie a fila no mesmo turno. Ao passar o bastão, confirme reserva/run em voo ou espera persistida e despachante ativo. Sem trabalho elegível, deixe a espera com o serviço e descreva o evento que a encerra. |

Para registrar sem iniciar um build, use `python run_ticket.py --create --queue-only --project <id> --title "<título>" --problem "<problema e evidência>" --criteria "<resultado verificável>"`. Confira o ID e releia o ticket. Ao priorizar, confira a ordem que o despachante reconhece e informe a diferença se só for possível registrar a intenção. Preserve o significado das faixas de bloqueio da linha e recuperação.

## Reagir a incidentes

- **Correção delimitada.** Com causa conhecida, mudança localizada e verificação objetiva, registre ou reutilize o ticket, delegue, valide e acompanhe a entrega nesta sessão. Para causa incerta ou impacto maior, deixe ticket priorizado com reprodução, evidência, impacto, dependências e aceite. Um erro do produto segue para o projeto do produto. Uma falha do runtime compartilhado segue para a DarkFac com vínculo ao trabalho atingido.
- **Cota baixa.** Faça o preflight conforme `AGENTS.md` e peça ao roteador outra rota elegível para a etapa. A contingência paga depende do saldo e do orçamento autorizados. Preserve o checkpoint. Sem rota, confirme `route_wait` persistido e o mecanismo de retomada.
- **Cota desconhecida.** Confira a idade e a origem da amostra. Use `python -m core.usage.cli accounts --refresh`, confira se o provedor publicou leitura nova e deixe a decisão de elegibilidade com o roteador. Se persistir, reutilize ou abra um reparo por provedor e causa. O aceite exige leitura atual utilizável pelo roteador. Outra rota elegível pode reparar enquanto os demais tickets seguem.
- **Falha repetida ou host indisponível.** Consulte causa estruturada, heartbeat, reserva, PR e último checkpoint. Use a retomada da etapa e o backoff existentes. Se o mesmo erro reaparecer sem progresso, isole o ticket e registre o reparo. Só leve ao owner decisões, segredos, aprovações ou aceites que dependam dele, pelo canal oficial.

## Aprender a cada interação

Toda mensagem do operador na sessão gera uma observação curta. Classifique o motivo: objetivo novo, decisão legítima, preferência, correção ou intervenção que a fábrica poderia ter evitado. Registre no estado de aprendizagem do projeto, por meio de `python -m core.learning.cli record-turn --skill coordenar-fabrica --prompt "<resumo sanitizado>" --intent "<intenção>" --gap "<motivo, projeto, harness e referência ao run>"`. Use `--followup` para uma correção ou continuação. Preserve a privacidade do projeto, incluindo nos registros compartilhados. Se a escrita estiver indisponível, deixe a observação no handoff autorizado para reconciliação.

Quando a coordenação poderia ter evitado a intervenção, confronte esperado e observado com o run, a etapa e a evidência anterior. Considere também falhas de outros harnesses mesmo que o operador nada tenha dito. Siga a skill `continuous-self-improvement` para consolidar ocorrências pela causa, registrar preferência ou RCA comprovada e aplicar o menor ajuste verificável à fonte canônica. Use um ticket da evolução quando faltar investigação ou mudança de componente. Preserve decisões pontuais no escopo original. A próxima sessão testa o ajuste em caso independente e registra se o mesmo trabalho avançou com menos intervenção. Uma lição pendente identifica o próximo responsável e a evidência que falta.

## Onde consultar

Caminhos a partir da raiz da DarkFac. Abra conforme a ocorrência.

- Projeto e entrada: `docs/PROJECT_ADOPTION.md`, `docs/manual/grill.md`, `docs/manual/fila.md`.
- Roteamento, custo e execução: `docs/manual/roteamento.md`, `docs/manual/custo.md`, `docs/manual/frota.md`, skill `run-ticket`.
- Validação e entrega: `docs/manual/portao.md`, `docs/manual/merge_autonomo.md`, `docs/runbooks/dokploy_redeploy.md`, `docs/runbooks/canary.md`.
- Continuidade: `docs/manual/operacao_interativa.md`, `docs/runbooks/owner_actions.md`, `docs/manual/learning_pack.md`. Antes do encerramento, atualize o handoff com projeto, etapa, evidência, incidente e próxima ação e execute `python -m core.ops.turn_guard --check`.
