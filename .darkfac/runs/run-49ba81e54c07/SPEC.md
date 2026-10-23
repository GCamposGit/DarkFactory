# SPEC

## Objetivo
Fechar de vez o defeito de health do portfolio: garantir que GET /api/portfolio/projects/{id} nunca devolva erro de atributo em health_details e que qualquer falha real no calculo de saude seja refletida (warning + diagnostico), nunca mascarada como healthy. O fix do acesso a RoadmapHealth.status ja esta no checkout (commit d8896c4 / USR-345, hub/backend/service.py:3519-3537 usa apenas campos existentes stale/blocked_items/conflicts/warnings/sources_unavailable); falta cobertura no nivel HTTP e fechar o caminho irmao de mascaramento (falha de get_snapshot engolida em log debug, sem diagnostico e sem rebaixar o status).

## Fora de escopo
- Redesenhar o painel de portfolio do DarkHub ou o frontend
- Alterar o contrato do modelo RoadmapHealth (core/roadmap/models.py) ou adicionar o campo status
- Mudar a semantica dos demais endpoints de portfolio (overview, efficiency, archetypes)
- Criar nova funcionalidade de saude alem dos criterios de aceite

## Desenho
1) Cobertura HTTP: tests/test_portfolio_api.py so exercita _build_portfolio_project_summary diretamente (helper _summary_with_roadmap_health, linhas 167-189). Adicionar teste que faz GET /api/portfolio/projects/{id} via TestClient para todos os projetos registrados (incluindo pilot-hello, presente em .factory/projects.json) e afirma que nem project.health_details nem roadmap_health carregam chave 'error', e que health_status pertence ao conjunto valido. 2) Anti-mascaramento restante: em _build_portfolio_project_summary o primeiro bloco try/except (hub/backend/service.py:3511-3517) captura a falha de self.roadmap.get_snapshot e apenas loga em debug; os contadores ficam zerados e o projeto pode terminar healthy mesmo com o calculo quebrado. Registrar o diagnostico em health_details (chave dedicada, ex. roadmap_snapshot_error) e forcar overall_health='warning' na agregacao (hub/backend/service.py:3550-3557), seguindo o mesmo padrao ja usado para o erro de get_health. Nada muda no caminho feliz: health_details continua com stale/blocked_items/conflicts_count/warnings/sources_unavailable.

## Arquivos a tocar
- hub/backend/service.py
- tests/test_portfolio_api.py

## Riscos
- test_get_portfolio_overview assume healthy_projects + warning_projects == total_projects; rebaixar projetos com snapshot quebrado para warning pode mudar a contagem em ambientes onde os paths de projeto nao existem — verificar que a suite inteira continua verde no portao oficial
- Teste HTTP sobre o registry real depende de projetos cujo path nao existe no Linux/CI (caminhos C:\dev\...); a asercao deve focar em ausencia de erro de atributo e status valido, nao em healthy
- A versao em producao (darkhub.ggcampos.com) so deixa de responder o erro apos o redeploy pos-merge (scripts/dokploy_redeploy.py); a validacao do ticket e local pelo portao oficial
