# SPEC

## Objetivo
Entregar primeiro uma base Python real e extensível de inventário e análise de vulnerabilidades; depois ampliar o mesmo contrato para Node, imagens, SBOM e IaC.

## Fora de escopo
- Remediação automática, alteração de dependências ou abertura automática de PRs.
- Análise de configurações inseguras de IaC; somente componentes e versões referenciados serão avaliados.
- Conceder novos acessos a repositórios, registries, ambientes ou fontes privadas.
- Tratar indisponibilidade, versão não resolvida ou cobertura incompleta como ausência de vulnerabilidade.
- Detecção genérica de malware, exploração ativa ou comprovação de segurança absoluta.
- Nova interface visual ou novo mecanismo de fila, roteamento ou revisão.

## Desenho
Criar um domínio headless em core/security com ArtifactRef, ComponentOccurrence, AdvisorySnapshot, CoverageLimitation e Finding. Cada execução vincula projeto, SHA/artefato, ambiente, plataforma, ferramenta, versões, hashes de entrada/saída, validade e proveniência. A identidade persistente do achado será project_id + canonical_vulnerability_id; ocorrências por componente e artefato permanecem separadas. A base Python inventaria versões realmente instaladas com pip inspect, sem inferir resolução a partir de ranges. Fontes OSV e CISA KEV entram por adapters com snapshot, TTL, ETag/digest e estado indisponível explícito. O control store fornece persistência transacional, lease, fencing, checkpoints, cache e deduplicação. QualityCycle roda no loop já existente do CloudCoordinator, consulta apenas projetos registrados e acessíveis e retoma checkpoints. Achados acionáveis geram demanda idempotente no ledger; BacklogFeeder, roteador e ReviewStage existentes cuidam de execução e revisão cruzada, sem remediação direta pelo ciclo.

## Arquivos a tocar
- core/security/models.py
- core/security/canonical.py
- core/security/store.py
- core/security/python_inventory.py
- core/security/advisory_sources.py
- core/security/analyzer.py
- core/security/demand_bridge.py
- core/security/quality_cycle.py
- core/workflow/control_store.py
- core/orchestrator/adapters/control_postgres.py
- core/orchestrator/cloud_coordinator.py
- tests/test_security_contracts.py
- tests/test_security_control_store.py
- tests/test_python_inventory.py
- tests/test_advisory_sources.py
- tests/test_security_analysis.py
- tests/test_quality_cycle.py
- docs/manual/quality_cycle.md
- docs/FASE_ATUAL.md

## Riscos
- Não existe implementação entregue do marco Python; tratar os documentos SQ como código existente criaria uma extensão sem base funcional.
- requirements.txt do próprio DarkFac contém ranges e não prova as versões instaladas; inventários sem resolução exata precisam registrar limitação.
- Aliases CVE, GHSA e OSV podem mudar ou divergir, exigindo normalização versionada sem fundir vulnerabilidades distintas.
- Mudanças e indisponibilidade das fontes podem produzir cache obsoleto; validade vencida deve resultar em estado desconhecido, não limpo.
- Concorrência entre coordenadores pode duplicar demandas ou sobrescrever evidência sem unique keys, leases e fencing transacionais.
- Cadastro de projeto não garante que seu caminho ou ambiente esteja disponível no host executor.
- Testes com fontes reais podem ser instáveis; a entrega precisa separar testes determinísticos de replay da demonstração real registrada.
