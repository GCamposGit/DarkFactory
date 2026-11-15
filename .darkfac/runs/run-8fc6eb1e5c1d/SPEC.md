# SPEC

## Objetivo
Mostrar custo e consumo por projeto e por execução (ticket ou tentativa) usando o contador de orçamento do portfólio (`PortfolioBudgetManager`) e os ledgers de uso que já existem (`core/usage/ticket_cost.py` e `core/usage/ledger.py`). Cada valor leva o rótulo `origem` (`medido`, `estimado` ou `ausente`) e é comparado com o limite autorizado do projeto (uso medido, uso estimado, limite e percentual). Projeto sem limite bloqueia (fail-closed) com motivo visível. Ao atingir o limite do projeto ou o teto global, novas execuções são bloqueadas e o owner recebe alerta (DarkHub e Telegram) sem repetição. Nenhum limiar de cobertura novo é criado.

## Fora de escopo
- Remediação de vulnerabilidades, campanhas novas ou mudanças no escaneamento de dependências
- Contador de custo paralelo ao PortfolioBudgetManager e aos ledgers de core/usage
- Limiar de cobertura ou baseline novos; os limiares atuais de 80%/100% do budget manager ficam como estão
- Aumentar limites automaticamente; limite mais alto só entra com decisão do owner
- Acesso, segredo, dependência ou destino de armazenamento novos
- Tela nova fora do DarkHub; o trabalho só amplia as rotas e telas de portfólio que já existem
- Mudar preços de assinatura ou a fórmula de implicit_cost_usd

## Desenho
1) Contrato (core/portfolio/models.py): Enum `CostOrigin` = medido | estimado | ausente. `UsageEntry` com project_id, execution_id (ticket/attempt), harness, model, origem, cost_usd (Optional, None quando não há valor em moeda), tokens/quota_delta (Optional) e timestamp. `BudgetComparison` com measured_usd, estimated_usd, limit_usd (Optional), pct_measured, pct_total, status e reason. O total de medido e estimado nunca vira um campo único sem rótulo: medido e estimado ficam em campos separados.
2) Classificação (core/usage/ticket_cost.py): reaproveitar TicketCostRecord e cost_by_ticket. api_cost_usd vindo de telemetria do provedor ou do harness conta como `medido`. implicit_cost_usd (tabela de preço de assinatura) conta como `estimado`. Harness de assinatura sem custo em moeda fica com consumo medido (weekly_delta, five_hour_delta) e custo estimado ou None. Ausência de delta ou de fonte fica `ausente`, nunca 0.0. A função nova `usage_by_execution(project_id)` agrega por execução e TicketCost ganha os campos measured_usd, estimated_usd e missing_sources. total_cost_usd continua existindo para compatibilidade, mas não é exibido como medido.
3) Limite obrigatório (core/portfolio/budget_manager.py): hoje get_budget cria um limite padrão de US$25 em silêncio (DEFAULT_BUDGETS_USD.get(..., 25.0)), o que contraria a decisão do owner. Isso passa a ser `require_budget(project_id)`, que devolve status BLOCKED com o motivo 'limite_ausente' quando o projeto não tem limite explícito (budgets.json ou perfil). O teto global vigente é lido do mesmo store. Um método `check_execution_allowed(project_id)` devolve allow ou deny com motivo e é chamado no ponto de admissão de execução (run_ticket/scheduler do portfólio). Ele bloqueia quando falta limite, quando o limite do projeto foi atingido ou quando o teto global foi atingido. record_spend recebe origem: só valores medidos e estimados rotulados entram no contador, e o estimado é guardado num campo separado (estimated_spent_usd).
4) Alertas: reaproveitar _send_telegram_alert e o mecanismo de owner_actions/DarkHub, com dedupe por (project, tipo, status) usando last_alert_at e a última chave enviada. Tipos de alerta: limite_proximo (o limiar de 80% que já existe), limite_excedido, limite_ausente e medicao_ausente. Alerta igual ao anterior não é reenviado.
5) Retenção: os registros ficam no store durável já usado (state_root/reports e portfolio/budgets.json), que é o destino autorizado do darkfac. Projeto sem destino durável autorizado no perfil não persiste registros e aparece com o motivo 'destino_nao_autorizado'.
6) DarkHub: a rota nova GET /api/portfolio/costs devolve BudgetComparison por projeto e a rota GET /api/portfolio/projects/{project_id}/costs devolve as execuções do projeto. As duas filtram por projeto e não expõem evidência bruta nem credencial. O frontend de portfólio ganha um painel com colunas separadas para medido, estimado, limite, % e motivo de bloqueio, e o selo 'ausente' no lugar de 0.
7) Documentação: atualizar docs/manual (roteamento.md e dicionario_dados.md), docs/FASE_ATUAL.md e o learning pack. Os testes novos substituem ou consolidam os testes de budget_manager e ticket_cost que já existem.

## Arquivos a tocar
- core/portfolio/models.py
- core/portfolio/budget_manager.py
- core/portfolio/scheduler.py
- core/usage/ticket_cost.py
- core/usage/ledger.py
- hub/backend/api.py
- hub/backend/service.py
- hub/backend/service_progress.py
- hub/frontend (painel de portfólio existente)
- run_ticket.py (ponto de admissão, se a checagem não couber no scheduler)
- docs/manual/roteamento.md
- docs/manual/dicionario_dados.md
- docs/FASE_ATUAL.md
- tests/test_portfolio_budget*.py
- tests/test_ticket_cost*.py

## Riscos
- Tirar o padrão silencioso de US$25 de get_budget pode bloquear projetos ativos (atrium, jarvis) que hoje dependem dele. A migração precisa listar os projetos sem limite explícito e criar um item OA-NNN para o owner definir cada limite, sem inventar valores.
- PortfolioBudgetManager é usado por router_optimizer, qualified_routes e o hub. Mudar a assinatura de get_budget ou record_spend pode quebrar esses chamadores; manter um wrapper compatível.
- Somar estimado com medido sem querer (total_cost_usd já mistura api com implicit). Os testes precisam garantir campos separados e que nada seja somado sem rótulo.
- Tratar ausência como zero: hoje o código usa `or 0.0` em vários pontos. Todo caminho de agregação precisa preservar None ou ausente.
- Dedupe de alerta mal feito gera spam no Telegram ou engole alerta novo.
- Isolamento por projeto no hub: a rota de detalhe não pode vazar execuções de outro projeto.
- Bloqueio fail-closed na admissão pode travar a própria frota do darkfac se o limite do darkfac não estiver definido; confirmar que o darkfac tem limite explícito antes do merge.
