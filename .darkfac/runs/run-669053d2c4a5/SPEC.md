# SPEC

## Objetivo
Inventariar offline e de forma determinística as resoluções Node registradas em package-lock v2/v3, pnpm-lock v6/v9 e yarn.lock Classic v1/Modern v8, preservando grafo, contextos e limitações localizadas.

## Fora de escopo
- Instalar dependências, executar scripts ou consultar registries.
- Consultar advisories, calcular vulnerabilidades ou integrar remediação automática.
- Gerar SBOM ou inventariar imagens e IaC.
- Suportar versões de lockfile diferentes das explicitadas.
- Afirmar que pacotes foram instalados, são aplicáveis à plataforma ou tiveram sua integridade verificada.
- Criar scheduler, QualityCycle, API HTTP ou UI paralelos.

## Desenho
Criar um domínio headless em core/projects: modelos Pydantic representam resoluções por locator, origem e versão; relações tipadas preservam importer/workspace, dependência direta ou transitiva, escopo dev/opcional/peer e contexto de peers; integridades declaradas mantêm formato, algoritmo e valor, sempre marcadas como não verificadas. Um dispatcher seleciona adapters somente pelo nome e versão suportados. Manifests package.json classificam dependências diretas por importer; ausência ou inconsistência gera limitação sem descartar resoluções válidas. A implementação segue os formatos oficiais de [npm package-lock](https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json/), [pnpm 6](https://github.com/pnpm/spec/blob/master/lockfile/6.0.md), [pnpm 9](https://github.com/pnpm/spec/blob/master/lockfile/9.0.md) e [Yarn Classic](https://classic.yarnpkg.com/en/docs/yarn-lock). A baseline e o candidato USR-449 não expõem QualityCycle ou port de inventário; portanto, inventory_node_dependencies será a única fronteira pública, sem duplicar orquestração.

## Arquivos a tocar
- core/projects/node_inventory.py
- core/projects/node_lockfiles/common.py
- core/projects/node_lockfiles/npm.py
- core/projects/node_lockfiles/pnpm.py
- core/projects/node_lockfiles/yarn_classic.py
- core/projects/node_lockfiles/yarn_modern.py
- tests/test_node_inventory_contracts.py
- tests/test_node_lockfile_npm.py
- tests/test_node_lockfile_pnpm.py
- tests/test_node_lockfile_yarn_classic.py
- tests/test_node_lockfile_yarn_modern.py
- tests/test_node_inventory_e2e.py
- docs/manual/dependencias_node.md
- docs/FASE_ATUAL.md
- docs/learning_packs/<DEMAND_ID>.md

## Riscos
- O contrato predecessor pode mudar antes da implementação; a integração deve ser reconciliada novamente sem criar um segundo ciclo.
- Locators pnpm e Yarn podem combinar aliases, protocolos e contextos de peers aninhados; normalização excessiva perderia identidades distintas.
- O mesmo pacote pode aparecer em vários importers, escopos e resoluções; classificação no nó, em vez da relação, produziria resultados incorretos.
- Yarn Classic não registra toda a informação de peers; a cobertura precisa permanecer parcial quando manifests não bastarem.
- Manifests e lockfiles podem estar ausentes, malformados ou inconsistentes; falhas locais não podem invalidar componentes independentes.
- Checksums e SRI são declarações do lockfile, não prova de verificação do conteúdo.
