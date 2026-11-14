# GRILL

## Premissas
- A entrega é uma extensão pontual de inventário Node; imagens, SBOM e IaC permanecem fora desta fatia.
- Suportar explicitamente package-lock.json v2 e v3, pnpm-lock.yaml 6.0 e 9.0, Yarn Classic v1 e Yarn Modern com __metadata.version 8; versões desconhecidas produzirão limitação estruturada.
- Usar versões efetivamente resolvidas, preservando identidade do pacote, origem, workspace/importer, relações entre dependências e contextos de peers, sem colapsar resoluções distintas.
- Ler também os manifests package.json da raiz e dos workspaces quando necessários para identificar dependências diretas e distinguir dev, opcionais e peers; informação ausente produzirá limitação, sem classificação inventada.
- Preservar a integridade declarada quando disponível, distinguindo algoritmos e formatos; sua presença não comprova que o conteúdo do pacote foi verificado.
- Entradas locais ou git sem versão verificável, peers não resolvidos e referências inconsistentes produzirão limitações localizadas; os componentes verificáveis continuarão no resultado, com cobertura parcial explícita.
- O inventário representa resoluções registradas, incluindo dev e opcionais; não comprova instalação ou aplicabilidade em uma plataforma ou ambiente específico.
- Os adapters serão de leitura, sem instalar dependências, executar scripts dos projetos ou consultar registries para completar versões ausentes.
- README, AGENTS.md e código relevante foram consultados. A busca em core encontrou detecção de gerenciadores e cache por lockfile, mas não adapters de inventário de vulnerabilidades nem QualityCycle; o planejamento deverá reconciliar a base e os contratos da etapa predecessora antes de definir a integração.
- Esta rodada não altera arquivos, cria tickets ou executa integrações; as referências técnicas fornecidas foram consultadas apenas para leitura.

## Decisoes do owner
- (nenhuma pergunta bloqueante)
