# SPEC

## Objetivo
Entregar o primeiro marco vertical do domínio de segurança para dependências Python do darkfac: inventário resolvido e rastreável, advisory versionado, aplicabilidade determinística, reavaliação contínua e um único ticket por vulnerabilidade aplicável.

## Fora de escopo
- Ecossistemas além de Python, imagens de contêiner e IaC neste primeiro marco.
- Usar modelo para decidir identidade, versão, intervalo afetado ou aplicabilidade.
- Remediação automática, merge ou concessão de acesso a outros projetos.
- Nova interface visual e campanhas de SAST, segredos ou red team.

## Desenho
Usar ProjectRegistry para localizar o checkout e seus ambientes; produzir inventário somente com versões exatas comprovadas por lockfile ou ambiente identificado, registrando lacunas como cobertura parcial. Um adaptador de fonte oficial normaliza IDs e aliases sem perder origem, conteúdo, revisão, digest, instante da consulta e validade. SQLite e PostgreSQL do control store persistem revisões, avaliações, cache, findings e a chave única projeto+vulnerabilidade canônica. O motor de aplicabilidade compara deterministicamente pacote normalizado, versão PEP 440, intervalos e condições ambientais antes de qualquer modelo. A consolidação é ligada ao ciclo canônico disponível: no baseline atual, que não contém QualityCycle, reutiliza a supervisão do CloudCoordinator sem criar scheduler paralelo; FeederCycle e BacklogFeeder admitem no fluxo normal o ticket persistido, cujo desenvolvimento usa o roteador existente. O fingerprint da evidência inclui inventário, ambiente, revisão do advisory e política, permitindo invalidação sem commit.

## Arquivos a tocar
- core/security/models.py
- core/security/dependency_inventory.py
- core/security/advisory_sources.py
- core/security/applicability.py
- core/security/cycle.py
- core/security/ticketing.py
- core/workflow/control_store.py
- core/orchestrator/adapters/control_postgres.py
- core/orchestrator/cloud_coordinator.py
- core/line/backlog_feeder.py
- requirements.txt
- docs/manual/seguranca_dependencias.md
- docs/FASE_ATUAL.md

## Riscos
- O SHA inspecionado não contém QualityCycle nem core/security, apesar da demanda afirmar entrega anterior; a implementação deve reconciliar a base e garantir um único ciclo ativo.
- requirements.txt e variantes contêm intervalos, não versões implantadas; ambientes não identificados devem permanecer partial ou waiting_resource.
- Aliases, revisões retroativas e condições ambientais dos advisories podem causar deduplicação ou aplicabilidade incorretas se a proveniência for perdida.
- SQLite e PostgreSQL precisam manter a mesma unicidade e atomicidade sob concorrência e retomada.
- Fonte externa pode impor indisponibilidade, rate limit ou mudança de contrato; evidência anterior não pode ser apresentada como atual.
- A divergência de USR-449 pode ser store indisponível, configuração divergente ou ticket ausente e exige estados distintos.
