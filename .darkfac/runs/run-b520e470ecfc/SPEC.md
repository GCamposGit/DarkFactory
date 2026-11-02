# SPEC

## Objetivo
Criar um ciclo diário e durável de qualidade para o darkfac que execute verificações documentais, de segurança e eficiência, registre estados e custos sem falsos verdes e encaminhe achados reais à fila canônica.

## Fora de escopo
- Semear ou inventar falhas para obter aceite.
- Provocar indisponibilidade deliberada em produção.
- Definir novos limiares de negócio ou ampliar o ciclo para outros projetos.
- Cancelar automaticamente tickets já admitidos quando um domínio for desativado.
- Alterar arquivos protegidos sem autorização assinada ou modificar o experimento Canaletto.

## Desenho
Adicionar quality_cycle.py como ciclo irmão do FeederCycle, acionado pelo CloudCoordinator e coordenado pelo control store. Cada janela diária consolidada executa adaptadores dos checks existentes para documentação, segurança e eficiência no SHA observado do projeto registrado. O store persiste janela, candidato, cobertura, indisponibilidade, adiamento, custo e evidências. Somente achados concretos e sanitizados viram demandas idempotentes; BacklogFeeder continua sendo o único caminho até a linha. Uma rechecagem vinculada ao achado e ao candidato corrigido é a única forma de resolvê-lo. A qualificação operacional e o aceite automático ficam em marcos posteriores.

## Arquivos a tocar
- core/line/quality_cycle.py
- core/workflow/control_contracts.py
- core/workflow/control_store.py
- core/orchestrator/adapters/control_postgres.py
- core/orchestrator/cloud_coordinator.py
- core/demands/service.py
- core/line/backlog_feeder.py
- .factory/config/quality_cycle.json
- tests/test_quality_cycle_store.py
- tests/test_quality_cycle.py

## Riscos
- Os checks podem terminar verdes e não produzir um achado real; nesse caso o aceite operacional deve permanecer pendente.
- Concorrência entre coordenadores ou retomadas pode duplicar janelas, achados ou tickets se a idempotência não for transacional.
- Migrações SQLite e PostgreSQL podem divergir e comprometer replay ou recuperação.
- Saídas de segurança podem conter material sensível e precisam ser sanitizadas antes da persistência e da criação do ticket.
- Custo indisponível pode ser confundido com zero se o contrato permitir valores implícitos.
- Mudança do SHA após uma verificação invalida a evidência e exige nova execução vinculada ao candidato correto.
