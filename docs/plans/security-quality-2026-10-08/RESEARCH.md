# Pesquisa — segurança, red team e qualidade

Consulta em 08/10/2026. Este dossiê distingue fontes normativas/técnicas, ferramentas candidatas e opiniões de especialistas. Nenhum pacote foi instalado e nenhum scanner foi executado contra sistemas durante a pesquisa.

## 1. Método e hierarquia de fontes

Decisões de segurança partem de NIST, OWASP, especificações e documentação dos próprios projetos. Blogs de autores reconhecidos e fornecedores explicam experiências e ajudam a priorizar; não fornecem certificação nem substituem controles. Repositórios foram consultados por páginas oficiais e API GitHub: licença detectada, arquivamento, atividade e estrutura. Estrelas são um sinal de adoção, não prova de segurança. Datas de push não provam que uma release seja segura.

Não copiamos código. A escolha final de cada componente exige licença do **artefato exato**, dependências transitivas, origem/digest de release, suíte existente, prova local de integração, risco de telemetry/egress, custos e compatibilidade Windows/Linux. As versões ainda não foram homologadas. Ausência de leitura detalhada de uma suíte ou de execução dos testes do fornecedor permanece explicitamente pendente.

## 2. Fontes técnicas e aplicação no plano

| ID | Fonte primária | Uso e limites |
|---|---|---|
| R01 | [NIST SP 800-218, SSDF 1.1 final](https://csrc.nist.gov/pubs/sp/800/218/final) | Estruturar desenvolvimento seguro e resposta às causas das vulnerabilidades. Não é selo automático de segurança. |
| R02 | [SSDF 1.2, Initial Public Draft](https://csrc.nist.gov/pubs/sp/800/218/r1/ipd) | Acompanhar evolução; não tratar draft como obrigação final. |
| R03 | [NIST SP 800-218A](https://www.nist.gov/publications/secure-software-development-practices-generative-ai-and-dual-use-foundation-models-ssdf) | Considerações para desenvolvimento de modelos/sistemas de IA; aplicar somente itens pertinentes à fábrica usuária de modelos. |
| R04 | [OWASP ASVS](https://owasp.github.io/www-project-application-security-verification-standard/) | Requisitos verificáveis de aplicações; referência v5.0.0 consultada. Selecionar controles/nível pelo projeto, com justificativas N/A. |
| R05 | [OWASP WSTG stable](https://wstg.owasp.org/stable/2-Introduction/) | Guia de testes e threat modeling; fixar cenários/versionamento na implementação. |
| R06 | [OWASP AI Agent Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html) | Permissões no executor, validação, dados/memória e testes adversariais. Exemplos não são código de produção a copiar. |
| R07 | [OWASP Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/) | Taxonomia para riscos de autonomia e casos de abuso. |
| R08 | [OWASP Agentic Skills Top 10](https://owasp.org/projects/agentic-skills-top-10) | Avaliar skills como parte da superfície executável, incluindo cadeia de dependências. Referência recente; não assumir maturidade de ferramenta. |
| R09 | [OWASP MCP Top 10](https://owasp.org/projects/mcp-top-10) | Mapear riscos de ferramentas/protocolos, identidade e contexto. |
| R10 | [OWASP Threat Modeling](https://cheatsheetseries.owasp.org/cheatsheets/Threat_Modeling_Cheat_Sheet.html) | Modelo de ameaças cedo e durante evolução do sistema. |
| R11 | [SLSA 1.2, status Approved](https://slsa.dev/spec/v1.2/) | Provenance e garantias progressivas de source/build. Meta futura deve declarar track/nível e evidência; SBOM não equivale a SLSA. |
| R12 | [MITRE ATLAS](https://atlas.mitre.org/) | Indexar técnicas/casos de IA; página consultada, sem extração detalhada de todas as técnicas neste turno. |
| R13 | [CISA KEV](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) | Priorizar vulnerabilidades já exploradas contra ativos inventariados. Busca primária consultada; abertura direta retornou erro no navegador de pesquisa. Ingestão/feed precisa de probe na implementação. |
| R14 | [PyRIT — paper dos autores, arXiv:2410.02828](https://arxiv.org/abs/2410.02828) | Referência de pesquisa para identificação de riscos em GenAI. Não prova segurança do aplicativo ou do harness. |
| R15 | [WASP — arXiv:2504.18575](https://arxiv.org/abs/2504.18575) | Exemplo de avaliação de injection em ambiente isolado. Apenas metadados/resumo consultados; estudo integral e corpus não homologados. |

As escolhas arquiteturais no roadmap são propostas da DarkFac fundamentadas por essas fontes, e não transcrição de uma única norma.

## 3. Reúso de ferramentas e componentes

| Candidato e fonte | Licença/atividade observadas em 08/10 | Aplicação proposta | Decisão e limite |
|---|---|---|---|
| [Ruff](https://github.com/astral-sh/ruff) | MIT; não arquivado; push 08/10/2026; ~49,9 mil estrelas | Lint, formatter e métricas simples sobre código novo/tocado | Preferido para piloto; não resolve tipos nem vulnerabilidades de fluxo sozinho. |
| [mypy](https://github.com/python/mypy) | Licença precisa ser conferida no artefato fixado; suíte `test-data`/`mypy/test` | Checker Python como candidato inicial | Comparar integração Pydantic e custo com Pyright; não declarar homologado pela consulta. |
| [Pyright](https://github.com/microsoft/pyright), [licença](https://github.com/microsoft/pyright/blob/main/LICENSE.txt) | API devolveu `NOASSERTION`; não arquivado, push 08/10/2026 | Alternativa de type checking | Não promover por um campo de licença inconclusivo; conferir licença/avisos do artefato antes da seleção. Escolher um checker principal. |
| [Bandit](https://github.com/PyCQA/bandit) | Apache-2.0; não arquivado; push 06/10/2026; ~8,3 mil estrelas | SAST inicial de Python | Preferido para piloto simples. Nem todo alerta é exploração e nem toda vulnerabilidade é detectável por AST. |
| [Semgrep](https://github.com/semgrep/semgrep) | LGPL-2.1; não arquivado; push 08/10/2026; ~16,9 mil estrelas | Avaliação de regras próprias e suporte a outros stacks | Não copiar/incorporar seu código no núcleo sob a política de reúso permissivo. Considerar ferramenta externa isolada somente após avaliar licenciamento, regras e telemetria. CE possui limites de análise; funcionalidades comerciais não são pressupostas. |
| [pip-audit](https://github.com/pypa/pip-audit) | Apache-2.0; não arquivado; push 01/10/2026; ~1,4 mil estrelas | Vulnerabilidades conhecidas de dependências Python | Preferido para adapter; resolver pacotes somente em ambiente isolado e sem credenciais operacionais. Ausência de advisory não prova ausência de pacote malicioso. |
| [Gitleaks](https://github.com/gitleaks/gitleaks) | MIT; não arquivado; push 30/09/2026; ~29,8 mil estrelas | Segredos staged/artefatos e revisão do histórico | Complementar `core.git.secret_scan`. Não substituir rotação; resultados sempre redigidos. |
| [Trivy](https://github.com/aquasecurity/trivy) | Apache-2.0; não arquivado; push 08/10/2026; ~38,3 mil estrelas | Imagens, IaC, vulnerabilidades e SBOM | Preferido no laboratório/release; fixar scanner, DB e digest de imagem. Evitar duplicação de scans sem benefício. |
| [ZAP](https://github.com/zaproxy/zaproxy), [Automation Framework](https://www.zaproxy.org/docs/desktop/addons/automation-framework/) | Apache-2.0; não arquivado; push 08/10/2026; ~15,9 mil estrelas | DAST autenticado em réplica autorizada | Preferido. Não confundir scan passivo com teste ativo; incluir roles, sessão e contexto. |
| [PyRIT](https://github.com/microsoft/PyRIT) | MIT; não arquivado; push 08/10/2026; ~4,6 mil estrelas; `tests/` | Orquestrar avaliação de segurança de features GenAI | Piloto após isolamento. Datasets/dependências podem ter licenças próprias; escopo não cobre toda segurança web. |
| [garak](https://github.com/NVIDIA/garak) | Apache-2.0; não arquivado; push 08/10/2026; ~9,5 mil estrelas; `tests/` | Probes complementares de modelos/diálogo | Alternativa complementar ao PyRIT, não dois frameworks obrigatórios no MVP. |
| [Nuclei](https://github.com/projectdiscovery/nuclei) | MIT; não arquivado; push 08/10/2026; ~31,8 mil estrelas | Cenários adicionais específicos | Não usar catálogo inteiro automaticamente. Templates são código/dados não confiáveis: curadoria, digest, revisão, scope e teste antes de liberar. |
| [Griffe](https://github.com/mkdocstrings/griffe) | ISC; não arquivado; push 08/10/2026; 698 estrelas | Inventário estático de assinaturas e comparação de APIs | Preferido como candidato; desabilitar import/inspeção dinâmica de candidato não confiável. Complementar relações desconhecidas, não inventá-las. |
| [Import Linter](https://github.com/seddonym/import-linter) | BSD-2-Clause; não arquivado; push 16/09/2026; 1.208 estrelas | Contratos de camadas/imports Python | Preferido para piloto; existência de contrato precisa ser exercitada com violação intencional em fixture. |

A estrutura de testes/CI foi identificada nas páginas/árvores dos candidatos; execução, cobertura e suficiência dos testes não foram auditadas aqui. A documentação de cada ferramenta, seus mecanismos de update e as licenças transitivas entram no aceite do adapter. Não instalar dependências flutuantes no runtime compartilhado para acelerar o piloto.

## 4. Mercado e especialistas

| Fonte original | Insight usado | Decisão da fábrica |
|---|---|---|
| [GitHub — arquitetura de Agentic Workflows](https://github.blog/ai-and-ml/generative-ai/under-the-hood-security-architecture-of-github-agentic-workflows/) | Exemplos de separação de execução, credenciais e rede em agentes | Avaliar broker e isolamento por tarefa. Não migrar a esteira inteira para outra plataforma. |
| [GitHub — princípios de segurança de agentes](https://github.blog/ai-and-ml/github-copilot/how-githubs-agentic-security-principles-make-our-ai-agents-as-secure-as-possible/) | Injection também pode chegar por conteúdo de desenvolvimento | Corpus cobrindo issues, código/docs, skills e resultados de ferramenta. |
| [GitHub Security Lab — taskflows](https://github.blog/security/how-to-scan-for-vulnerabilities-with-github-security-labs-open-source-ai-powered-framework/) | Pesquisa assistida por IA exige distinguir bug e vulnerabilidade explorável | Usar modelos para hipótese/triagem, e prova de impacto e verificação independente para bloqueio. |
| [Simon Willison — lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) | Combinação de dados privados, conteúdo não confiável e saída externa merece atenção | Separar acesso a dados e egress por capabilities. Opinião do autor como explicação de risco, não como norma. |
| [Martin Fowler — Refactoring](https://www.martinfowler.com/books/refactoring.html) | Transformações pequenas com comportamento preservado | Testes de caracterização e comparação de contratos antes/depois. |
| [Fowler — refatoração preparatória](https://martinfowler.com/articles/preparatory-refactoring-example.html) | Melhorar estrutura pode simplificar a alteração seguinte | Avaliar antes da feature, sem impor reescrita por preferência estética. |
| [Fowler — refatoração oportunista](https://martinfowler.com/bliki/OpportunisticRefactoring.html) | Melhorias frequentes junto ao trabalho e com testes verdes | Etapa obrigatória de avaliação por ticket e consolidação por módulo. |
| [Sonar — quality profiles/gates](https://www.sonarsource.com/blog/clean_coding-quality_profile_quality_gate_guidance/) | Gates focados em novo código permitem adoção gradual | Reusar ideia de não degradar o código tocado; não importar cobertura/thresholds universais. |
| [CodeScene — technical debt](https://codescene.com/manage-and-reduce-technical-debt) | Hotspots direcionam esforço de dívida técnica | Medir churn, responsabilidade, incidentes e complexidade antes de refatorar. Ferramenta paga é benchmark de abordagem, não dependência inicial. |

SonarQube/CodeScene oferecem visibilidade agregada; a primeira implantação proposta usa adapters headless e o Hub existente. Compra/SaaS só seria analisada após piloto, necessidade medida, compatibilidade de dados e autorização de gasto. Alegações de eficácia dos fornecedores não foram reproduzidas.

## 5. Decisões de planejamento

- Reusar módulos da fábrica e integrar política aos consumidores reais; apenas criar a orquestração/evidência ausente.
- Começar com Ruff + um checker + Bandit + pip-audit/Gitleaks; adicionar Trivy por artefato. Não tornar todas as ferramentas obrigatórias em todo ticket.
- Red team: ZAP para aplicações; PyRIT ou garak para superfície GenAI, com fixtures próprias de autorização, tenants e capacidades.
- Inventário: extração estática + metadados verificáveis + documentação de invariantes; catálogo e infraestrutura ligados por IDs.
- Atualizações entram como propostas testadas e versionadas. Fonte web/skill jamais muda permissões diretamente.
- Nenhum candidato está homologado por este documento; a seleção de release/digest e a execução de provas fazem parte dos tickets de implantação.

Catálogo estruturado e provenance: `.factory/research/security-quality-2026-10-08/ledger.json`. O ledger registra URLs e metadados de consulta; hashes de páginas completas/releases não foram calculados nesta pesquisa e não são apresentados como se existissem.

## 6. Extensão — dados e documentação contínua

A pesquisa foi ampliada após o pedido explícito de dicionários e manuais: **34 fontes registradas e 17 repositórios candidatos no total**. As três decisões iniciais do Grill estão confirmadas; formatos/versões serão homologados na implementação e requisitos materiais específicos no Grill de cada projeto/campanha.

| ID | Fonte original | Aplicação e limites |
|---|---|---|
| R16 | [Bitol ODCS — schema](https://bitol-io.github.io/open-data-contract-standard/latest/schema/), [definições autoritativas](https://bitol-io.github.io/open-data-contract-standard/latest/authoritative-definitions/) | Identidade estável de elementos, representação lógica/física e vínculo a definições. Release v3.2.0 observada na API em 08/09/2026; fixar versão na homologação. Não criar YAML concorrente para cada função quando contratos tipados já existem. |
| R17 | [W3C SKOS Reference](https://www.w3.org/TR/skos-reference/) | Conceitos identificados, termos preferidos, aliases e esquemas de vocabulário. A política de namespaces do portfólio é desenho da fábrica. Não requer armazenar tudo em RDF. |
| R18 | [W3C DCAT 3](https://www.w3.org/TR/vocab-dcat-3/) | Catalogação e relações de versão/histórico; apoiar referências duráveis entre projetos autorizados. Não concede permissão para compartilhar dados. |
| R19 | [OpenAPI 3.2.0](https://spec.openapis.org/oas/v3.2.0.html) | Interface HTTP compreensível por humanos e ferramentas. Referência publicada; conferir suporte real de FastAPI/consumidores antes de escolher versão. |
| R20 | [Griffe — docstrings](https://mkdocstrings.github.io/griffe/reference/docstrings/) | Estrutura de parâmetros, retornos e erros. Extrair estaticamente e validar conteúdo; presença de docstring não prova finalidade correta. |
| R21 | [Data Contract CLI](https://cli.datacontract.com/) | Lint, mudanças incompatíveis, qualidade e exportação de contratos. O projeto declara limitação em testes Windows; validar adapter/ambiente e extras mínimos. Exemplos online não serão executados. |
| R22 | [MkDocs](https://www.mkdocs.org/) | Engine candidata para manuais Markdown. Fixar engine/plugins/handlers; build de documentação também executa código e exige isolamento. |
| R23 | [mkdocstrings](https://mkdocstrings.github.io/) | Referência automática e ligações entre páginas/sites. A configuração deve preservar modo estático e ACL das referências de outros projetos. |
| E05 | [Daniele Procida — Diátaxis](https://www.diataxis.fr/start-here/) | Organizar tutoriais, tarefas, referência e explicações por necessidade do leitor. Funções têm referência própria; módulos/projetos também precisam de fluxos e operação. |
| M06 | [Backstage TechDocs — docs like code](https://backstage.io/docs/features/techdocs/how-to-guides/) | Gerir documentação junto ao código e catálogo. Adotar prática e reaproveitar o Hub; a plataforma Backstage inteira não é selecionada para instalação. |

| Repositório adicional | Snapshot público em 08/10/2026 | Uso proposto e evidência |
|---|---|---|
| [bitol-io/open-data-contract-standard](https://github.com/bitol-io/open-data-contract-standard) | Apache-2.0; não arquivado; push 11/09/2026; 1.170 estrelas | Schema/spec, exemplos, `.github/` e release v3.2.0 observados. Suficiência dos checks não auditada; um schema JSON auxiliar não substitui o texto do padrão. |
| [datacontract/datacontract-cli](https://github.com/datacontract/datacontract-cli) | MIT; não arquivado; push 08/10/2026; 1.081 estrelas | `tests/`, `.github/`, Python e exemplos observados. Adapter candidato para dados sintéticos em laboratório; licença dos extras/dependências e suporte Windows ainda exigem homologação. |
| [mkdocstrings/mkdocstrings](https://github.com/mkdocstrings/mkdocstrings) | ISC; não arquivado; push 03/10/2026; 2.098 estrelas | `tests/`, `.github/` e documentação observados. Complementa Griffe; engine/handlers têm versões e licenças próprias a conferir. |

Decisão proposta: registro semântico pequeno e versionado integrado ao catálogo; referências aos contratos existentes; ODCS onde houver datasets; referência estática com Griffe/mkdocstrings e manuais de uso em Markdown. Cada PR valida impacto semântico/documental e cada consulta resolve a versão e ACL. O desenho de publicação coordenada, manifesto externo e bloqueio de índice obsoleto é uma inferência arquitetural da fábrica a partir dessas práticas, não uma garantia fornecida pelos frameworks.

Detalhamento normativo de planejamento: [dados, dicionários e manuais](DATA_STRATEGY_DICTIONARIES_MANUALS.md). Nenhuma suíte dos fornecedores foi executada; releases, licenças transitivas e comportamento dos adapters continuam sem homologação.
