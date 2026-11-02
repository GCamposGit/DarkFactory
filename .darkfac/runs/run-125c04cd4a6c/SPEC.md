# SPEC

## Objetivo
Adicionar ao ciclo existente um domínio recorrente de eficiência que reconcilie evidências observáveis, consolide métricas diárias, detecte desvios por política reproduzível e admita exatamente um ticket por problema qualificado.

## Fora de escopo
- Criar painel ou fluxo visual novo no DarkHub.
- Converter custo ausente em zero ou misturar USD, percentual de cota e custo implícito de assinatura.
- Definir limiares de negócio não presentes em política versionada.
- Ampliar permissões, objetivos ou acessos ao cadastrar outros projetos.
- Alterar o agendamento principal ou criar um segundo scheduler fora do CloudCoordinator.

## Desenho
Introduzir um serviço headless de eficiência acionado pelo mesmo loop do CloudCoordinator. O serviço normaliza ModelUsageLedger, ticket_cost, telemetria e jobs/runs do control store em amostras com identidade e proveniência; deduplica eventos espelhados; consolida períodos UTC por projeto e ticket; persiste cobertura, ausências e fingerprints das fontes; recompõe períodos atrasados ou afetados por evidência tardia; calcula baseline somente sobre coortes comparáveis e política versionada; e produz desvios reproduzíveis. Uma camada de admissão usa identidade determinística do problema para criar ou atualizar um único ticket, respeitando estados existentes, desativação por projeto e rechecagem pós-remediação com evidência nova. Cache e resultados derivados são invalidados por mudanças nas fontes, política, baseline, estado do ticket ou remediação.

## Arquivos a tocar
- core/usage/ticket_cost.py
- core/telemetry/models.py
- core/telemetry/store.py
- core/efficiency/
- core/projects/models.py
- core/line/efficiency_cycle.py
- core/orchestrator/cloud_coordinator.py
- hub/backend/service_usage.py
- hub/backend/api.py
- docs/manual/eficiencia.md
- docs/FASE_ATUAL.md

## Riscos
- Identificadores históricos incompletos podem impedir reconciliação segura; o resultado deve registrar cobertura limitada em vez de inferir vínculos.
- O ledger de eventos recentes é limitado, podendo deixar períodos antigos sem amostras suficientes para baseline.
- Eventos tardios e relógios divergentes podem alterar períodos já consolidados se o fingerprint e a janela UTC forem calculados incorretamente.
- Deduplicação excessiva pode ocultar tentativas reais; deduplicação insuficiente pode duplicar custo e retries.
- Política ou identidade de problema instável pode gerar churn de tickets ou comparações não equivalentes.
- Indisponibilidade ou divergência entre versão implantada, configuração e control store pode produzir diagnóstico falso de ticket ausente, especialmente em USR-449.
