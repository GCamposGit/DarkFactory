# SPEC

## Objetivo
Fechar a execucao do marco M3 do piloto greenfield: fazer a demanda real do owner (`/demand pilot-hello: ...`, OA-047) entrar de fato no projeto pilot-hello com ticket de prefixo PH vinculado a mensagem, garantir que o Gate G1 nao reabra as decisoes ja respondidas na OA-042, provar que nenhum ticket USR- foi criado para essa entrada e registrar evidencia real (horario, id do ticket, projeto, trecho do roteamento), resolvendo a OA-047 ou reabrindo-a com diagnostico.

## Fora de escopo
- Marcos M4 a M7 do USR-279 (implementacao, revisao, PR, merge, deploy no Dokploy, prova de independencia e relatorio final)
- Codigo do produto pilot-hello (pagina, /healthz) e qualquer commit no repositorio do piloto
- Mudar o fluxo de onboarding de projeto novo (/newproject) ou o roteamento de projeto desconhecido do USR-278
- Alterar a politica de autosubmit da linha para todos os projetos registrados (DARKFAC_LINE_INTAKE_PROJECTS continua por ambiente; leftovers viram OA)
- Configurar variaveis de ambiente ou webhook no Dokploy pela mao do agente

## Desenho
Tres defeitos reais bloqueiam o M3 hoje. (1) `core/intake/project_routing.resolve_demand` ja roteia corretamente (`accept`, project_id=pilot-hello), mas em `core/integrations/telegram.py` (~l.773-860) o projeto roteado so e usado no caminho `intake_service`; o gateway que o DarkHub constroi (`hub/backend/service.py:3059`, `demand_handler=_handle_demand`, sem intake_service) cai no branch `demand_project != 'darkfac' and self.intake_service is None` e responde 'nada foi registrado' - a mensagem real do owner nao registra nada. (2) `_handle_demand` (hub/backend/service.py:2980) cria o ticket com `project_id='darkfac'` fixo, ou seja, se o branch de recusa fosse removido o resultado seria exatamente o ticket USR- que o M3 proibe; o prefixo correto ja existe (`DemandsStore.next_ticket_id` -> `get_ticket_prefix`, pilot-hello = PH em .factory/projects.json). Correcao: passar o project_id roteado ao handler (assinatura compativel com os handlers de 2 argumentos usados nos testes atuais) e o handler usar `DemandInput(project_id=<rota>)`, gravando o external_id da mensagem (`tg-msg-<chat>-<msg>`) em tag/`core_journey` - UserTicket tem `extra='forbid'` e nao possui campo external_id. (3) Gate G1: o grill da linha (`core/line/stage_grill.run_grill`) preenche o slot `{grill}` do prompt apenas com `parent_grill`; nada leva as decisoes ja respondidas da OA-042 ao agente. Correcao minima e generica: helper que renderiza as decisoes respondidas (`OwnerActionStore.answered_decisions()`, ja existente) relevantes ao projeto/ticket, campo opcional `project_id` em `OwnerAction` (model tem `extra='ignore'`, leitores antigos sobrevivem) com a OA-042 marcada como `pilot-hello`, injecao no slot `{grill}` por `core/line/bindings.py` e uma regra no prompt proibindo reabrir o que estiver listado. Por fim, um verificador read-only (`scripts/pilot_m3_verify.py`) correlaciona mensagem -> demanda -> ticket PH, confirma ausencia de ticket USR- para a entrada e imprime o trecho do roteamento (resolve_demand em state dir temporario, sem escrever onboarding), alimentando docs/evidence/USR-279-M3.md e a decisao sobre a OA-047.

## Arquivos a tocar
- core/integrations/telegram.py
- hub/backend/service.py
- core/owner_actions/models.py
- core/owner_actions/decisions.py (novo)
- core/line/stage_grill.py
- core/line/prompts/grill.md
- core/line/bindings.py
- scripts/pilot_m3_verify.py (novo)
- .factory/owner_actions/owner_actions.json
- docs/evidence/USR-279-M3.md (novo)
- docs/plans/USR-279-greenfield-pilot.md
- tests/test_telegram_project_demand.py (novo)
- tests/test_grill_prior_decisions.py (novo)
- tests/test_pilot_m3_verify.py (novo)

## Riscos
- A mensagem real do owner pode ter sido enviada antes da correcao e recusada ('nada foi registrado'); nesse caso nada foi registrado e a OA-047 precisa de passo explicito de reenvio, nao de reabertura silenciosa.
- M3 nao pode ser declarado concluido sem a mensagem real: os tickets de codigo terminam verdes por teste, mas a evidencia fica honesta (entrada ausente = OA-047 aberta com diagnostico).
- pilot-hello nao esta em DARKFAC_LINE_INTAKE_PROJECTS (default darkfac), logo o ticket PH e registrado mas nao gera run/grill na linha; isso e config de ambiente do Dokploy (owner) e deve virar OA, nao mudanca de politica global aqui.
- Mudar a assinatura de demand_handler pode quebrar chamadores/testes existentes (tests/test_audio_grill_and_demands.py, test_telegram_dual_bot.py, test_hf14_telegram_n8n_integration.py usam 2 argumentos): a mudanca precisa ser retrocompativel.
- Injetar decisoes respondidas no prompt do grill afeta todos os projetos: filtro por projeto/ticket, limite de itens e truncamento para nao inflar o prompt nem vazar segredo (usar redacao ja existente em core.owner_actions).
- Edicao de .factory/owner_actions/owner_actions.json concorre com outras runs; usar o store/merge oficiais (core.owner_actions.store / merge) e nunca reescrever historico.
