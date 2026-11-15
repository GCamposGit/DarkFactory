# SPEC

## Objetivo
Ampliar o portfólio do DarkHub com visão operacional por projeto, incluindo bloqueios, cobertura, custos, retestes e evidências, com autorização explícita e isolamento aplicado no servidor.

## Fora de escopo
- Criar outro app, fila, roteador, scanner ou ciclo de qualidade concorrente.
- Conceder acesso a evidências pelo simples cadastro no ProjectRegistry.
- Elevar limites financeiros, alterar tetos globais ou introduzir segredos.
- Recriar core/security ou antecipar funcionalidades ainda não entregues pelo USR-449.
- Aplicar retenção destrutiva a evidências ou histórico operacional.

## Desenho
Criar um registro operacional separado do ProjectRegistry, contendo perfil, destino durável, prazo de reteste, limite financeiro e grants explícitos. Um repositório append-only armazena evidências sempre com project_id e exige o mesmo escopo em lista, detalhe, download e agregados. Uma projeção headless combina esse repositório com control store, usage ledger e orçamento, preservando valores medidos e estimados separadamente e classificando cobertura como evaluated, unknown, omitted ou expired. O DarkHub expõe endpoints autenticados e escopados, consumidos pelo painel de portfólio. Alertas usam a infraestrutura existente de DarkHub e Telegram, com fingerprint persistente da condição. O baseline c4dfbb724b0e9410cf14a27ae3ad224c547a8e6f contém USR-449 em implementing, mas ainda não contém QualityCycle nem core/security; cada implementação deve reconciliar o estado após rebase e adaptar capacidades que tenham chegado, sem duplicá-las.

## Arquivos a tocar
- core/projects/operational.py
- core/projects/evidence.py
- core/projects/operations_view.py
- core/projects/operations_alerts.py
- core/portfolio/budget_manager.py
- core/line/admission.py
- hub/backend/models.py
- hub/backend/service_operations.py
- hub/backend/api.py
- hub/frontend/portfolio.js
- hub/frontend/index.html
- .factory/project_operations.json
- docs/manual/paineis_operacionais.md

## Riscos
- USR-449 pode aterrissar durante a execução e mudar a fonte canônica de cobertura e evidências; o rebase deve preferir adaptação ao contrato existente.
- O control store do DarkHub pode estar configurado somente para leitura ou apontar para versão diferente; indisponibilidade deve aparecer como unknown, nunca como ausência de achados.
- Referências de jobs existentes podem conter dados de projetos diferentes; filtrá-las depois da consulta permitiria vazamento por ID, contagem ou diagnóstico.
- Eventos antigos de custo podem não ter project_id ou separar medição de estimativa; devem permanecer não atribuídos, sem imputação silenciosa.
- Relógio, timezone ou alteração do prazo de perfil podem classificar retestes incorretamente; a avaliação precisa aceitar clock UTC injetável.
- A deduplicação atual de notificações é parcialmente em memória e a retenção padrão não é permanente; a chave da condição operacional precisa de persistência própria.
