# GRILL

## Premissas
- A demanda é uma correção pontual no backend Python/FastAPI, sem redesenho do painel ou nova funcionalidade.
- RoadmapHealth não possui status; o cálculo deve usar seus campos existentes, preservando o contrato Pydantic.
- Falhas no cálculo devem preservar o diagnóstico em health_details e resultar em health_status=warning, conforme a agregação já existente, nunca healthy.
- O checkout já contém tratamento compatível e dois testes de regressão em tests/test_portfolio_api.py; o planejamento deve verificar a cobertura HTTP e a versão implantada antes de propor alterações adicionais.
- Esta etapa é somente leitura: nenhum arquivo foi alterado e o portão oficial não foi executado. A implementação deverá validar com python core/harness/runner.py --quick.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
