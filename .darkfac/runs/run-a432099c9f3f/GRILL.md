# GRILL

## Premissas
- A rodada permaneceu somente de leitura, sem criação de arquivos, tickets ou execução de integrações operacionais.
- README.md, AGENTS.md, docs/FASE_ATUAL.md, requirements.txt e os módulos FeederCycle e BacklogFeeder foram inspecionados; a implementação seguirá Python, contratos Pydantic v2 e serviços headless em core/.
- A busca em core/, docs/ e tests/ não localizou implementação de consumo ou geração de SBOM nem QualityCycle; o planejamento deverá reconciliar os contratos da base citada sem pressupor que já existem neste checkout.
- Consumo e geração abrangerão CycloneDX JSON 1.6 e 1.7 e SPDX JSON conforme a especificação 2.3.1, sem ampliar para XML ou outros formatos.
- Na especificação SPDX 2.3.1, o campo de versão continua SPDX-2.3; não será exigido o literal SPDX-2.3.1. Referência consultada: https://spdx.github.io/spdx-spec/v2.3.1/document-creation-information/.
- Schemas e regras semânticas serão fixados por versão, com validação de identificadores, criação, ferramenta e versão, subject, purl/CPE quando presentes, relações e hashes; nenhuma informação ausente será inventada.
- Versões não suportadas e documentos estruturalmente inválidos serão recusados com diagnóstico explícito, sem conversão silenciosa.
- O SBOM original será preservado byte a byte por SHA-256; normalização e geração produzirão evidências separadas, vinculadas ao original e ao artefato.
- A geração abrangerá os projetos e imagens já definidos nas premissas, com ferramenta fixada, proveniência e validação da saída em cada formato solicitado; limitações do gerador serão explícitas.
- Cadastro de projeto não amplia permissões; referências externas no SBOM não autorizam acesso automático a URLs ou execução de código.
- Esta demanda não inclui criar um novo produto, ampliar a análise de IaC para configurações inseguras ou implementar um novo motor de vulnerabilidades.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [business] Para confirmar cobertura de um SBOM recebido, qual evidência deve comprovar seu vínculo ao artefato? (id=q1)
- [business] Se o SBOM for estruturalmente válido, mas tiver componentes sem versão ou evidência suficiente, os componentes verificáveis podem ser aproveitados? (id=q2)
