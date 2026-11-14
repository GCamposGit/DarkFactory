# SPEC

## Objetivo
Estender, após reconciliar a base canônica entregue, o inventário de vulnerabilidades para providers e módulos Terraform resolvidos e dependências Helm v2 travadas, preservando origem, versão, hashes e limitações de cobertura.

## Fora de escopo
- Misconfiguration, configuração insegura e policy-as-code.
- Execução de Terraform, Helm, scripts ou download de artefatos.
- Geração de SBOM, inventário Node ou inspeção de imagens.
- Uso de constraints, tags, branches, appVersion ou referências mutáveis como versão resolvida.
- Verificação material dos artefatos ou dos hashes declarados.
- Criação de outro domínio, persistência, fonte de advisories ou mecanismo de findings.

## Desenho
Reconciliar primeiro o SHA e os contratos reais do marco Python em core/security, reutilizando ComponentOccurrence, CoverageLimitation, identidade canônica e analyzer. Adapters headless e puros leem arquivos locais: Terraform correlaciona required_providers HCL2 com .terraform.lock.hcl, preserva a versão selecionada e hashes, e admite módulos somente com versão exata de registry ou ref Git completa e imutável; Helm correlaciona Chart.yaml apiVersion v2 com Chart.lock, preservando repository, versão resolvida e digest do lock. Cada entrada mantém caminhos e digests dos manifestos. Dados incompletos, inconsistentes ou sem mapeamento canônico/advisory produzem limitações, nunca finding negativo. Mudanças nos arquivos invalidam a análise pelo mecanismo de digest já existente.

## Arquivos a tocar
- core/security/models.py
- core/security/analyzer.py
- core/security/terraform_inventory.py
- core/security/helm_inventory.py
- core/security/quality_cycle.py
- requirements.txt
- tests/test_security_contracts.py
- tests/test_terraform_inventory.py
- tests/test_helm_inventory.py
- tests/test_quality_cycle.py
- docs/manual/quality_cycle.md
- docs/FASE_ATUAL.md

## Riscos
- O checkout atual não contém core/security nem a implementação do marco Python; a execução deve depender do receipt e SHA entregues pelo predecessor, sem recriar contratos paralelos.
- HCL2 válido pode conter expressões não resolvíveis estaticamente; esses casos precisam permanecer como limitações.
- Lockfiles ausentes, obsoletos ou inconsistentes podem aparentar resolução inexistente.
- Hashes Terraform e digest Helm são evidência declarada, não prova de download ou integridade verificada.
- Fontes Terraform e repositórios Helm podem não possuir correspondência confiável nas fontes de advisories; cobertura desconhecida não equivale a ausência de vulnerabilidade.
- Adicionar um parser HCL2 exige dependência compatível com Python 3.12 e atualização do contrato de instalação.
