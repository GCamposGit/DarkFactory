# DEMAND

- projeto: darkfac
- canal: line

## Texto original

# Motor unico: todo canal executa a mesma sequencia de estagios com artefatos obrigatorios e grill de owner

## Problema
Existem tres motores sem dono unico: a linha DAG (core/line, Postgres, 37 commits), o run_ticket.py chamado pelo despachante (534 commits) e o chat. O caminho dominante vai direto do ticket ao desenvolvimento, sem grill, planejamento, revisao nem deploy; pesquisa e learning pack nao tem estagio em nenhum caminho. O Gate G1 e sintatico (core/demands/readiness.py aprova 'Quero um painel que mostre minhas vendas do mes'), o grill legado faz perguntas tecnicas fixas e .agents/rules/GEMINI.md induz perguntas de arquitetura ao owner. Ticket vindo do Telegram e gravado dentro do container e o feeder le a fila congelada na imagem. Este bloco absorve USR-355 (run retomado volta a waiting_human) e USR-371 (propagar project_id ate pick e deliver_branch), necessarios para projetos externos. Plano: docs/plans/PLANO_V0_2026-10-24.md.

## Jornada
Definir a sequencia canonica: intake, grill do owner (entregas grandes ou ambiguas), planejamento com modelo de nivel alto, pesquisa (estagio do USR-376), desenvolvimento, validacao, revisao por outra familia, merge, deploy e smoke, documentacao e learning pack (estagios do USR-376)
Cada estagio grava um artefato com schema Pydantic validado antes do proximo: plano (SPEC.md + tickets.json), veredito de revisao, relatorio de deploy; ausencia ou schema invalido bloqueia o avanco
run_ticket.py e core/line/fleet_dispatcher.py passam a executar essa sequencia reaproveitando core/line/stage_*.py; o caminho direto para desenvolvimento e removido; run_ticket._run e quebrado em funcoes por estagio
Planejamento so usa modelos de nivel alto do registro; sem nenhum disponivel o ticket espera em vez de cair para modelo barato; o plano fatia blocos grandes em sub-tarefas executadas em sequencia dentro do mesmo ticket
G1 avalia conteudo: exige objetivo, criterio de sucesso verificavel e modo de aceite (automatico ou do owner); pergunta ao owner so nas categorias objetivo, sucesso, negocio, segredo novo e aceite (lista fechada em codigo); decisao tecnica vira ADR curto; cada pergunta traz opcao recomendada respondivel com um toque no Telegram ou no Hub; correcoes e entregas pequenas passam sem grill
Telegram, Hub e chat submetem ao mesmo motor pela API da fila do USR-343 (ou, antes dele, pelo arquivo canonico versionado); um unico alimentador; o chat vira console (submeter, acompanhar, responder)
Remover: perguntas tecnicas fixas de core/demands/grill.py, trecho de GEMINI.md sobre nao assumir parametros tecnicos, handlers de core/workflow sem chamador, segundo alimentador da fila

## Fora de escopo
- Nao reescrever a linha DAG nem trocar Postgres por outro motor de workflow
- Nao adotar framework externo de orquestracao (Archon, Temporal, LangGraph) neste bloco
- Nao mudar o portao oficial

## Criterios de aceite
- Teste de ponta a ponta nos tres pontos de entrada (run_ticket, despachante, linha): ticket sem plano, sem veredito de revisao ou sem relatorio de deploy nao chega ao merge
- Demanda vaga ('Quero um painel que mostre minhas vendas do mes') vai para grill com no maximo 3 perguntas de owner, cada uma com opcao recomendada; nenhuma pergunta tecnica chega ao owner (teste com 10 demandas de exemplo)
- Correcao pequena passa direto, sem grill, ate producao
- Ticket criado pelo Telegram e executado sem passar por arquivo efemero do container (teste com redeploy no meio)
- Ticket de projeto externo carrega project_id ate roteamento e entrega (absorve USR-371); run retomado apos 6h nao volta a waiting_human por falta de rota transitoria (absorve USR-355)
- Planejamento com modelo barato e impossivel (teste do roteador de planejamento)
- Nenhuma funcao do caminho de execucao com mais de 150 linhas
- Ao entrar em producao, remove a tag planning-only dos blocos restantes do plano para que o despachante passe a executa-los com planejamento e fatiamento pelo motor
- Manual do usuario e documentacao tecnica (docs/manual/) atualizados na mesma entrega, conferidos contra o codigo
- Saldo de testes da suite nao aumenta: teste novo de comportamento substitui ou consolida testes existentes; nenhum teste de forma entra no portao
- Learning pack da entrega (leigo + dev) publicado e enviado ao owner
- Portao oficial verde, revisao por outra familia, merge e deploy com smoke verde
