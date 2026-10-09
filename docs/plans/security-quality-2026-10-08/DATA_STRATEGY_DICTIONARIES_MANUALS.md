# Estratégia de dados, dicionários e manuais — extensão do roadmap

Estado: **planejamento; implantação não autorizada**. Este complemento registra o pedido adicional do Owner e integra SQ-31 a SQ-38 ao roadmap principal. Segurança, isolamento entre clientes e proteção de evidências continuam obrigatórios. Nenhuma ferramenta, regra ou rotina é ativada aqui.

## 1. Resultado verificável

Durante cada rodada de desenvolvimento, cada módulo deve ter estratégia de dados validada e cada função deve ter dicionário e manual próprios. No aceite do módulo/projeto, reconciliar todos os símbolos do inventário, inclusive funções privadas e internas. Código gerado e vendor constam no manifesto com proveniência e referência ao manual de origem; qualquer exceção à documentação detalhada exige justificativa e validação explícita.

O manual será legível por humanos e consultável por agentes, ligado à versão examinada do código. Cada PR, incluindo seus pushes subsequentes, atualiza os documentos afetados. Uma consulta deve validar versão, integridade e autorização antes de apresentar conteúdo como atual. A documentação completa do projeto existe desde o desenvolvimento e acompanha sua operação e manutenção.

## 2. Aproveitar o existente

A baseline tem contratos Pydantic, schemas de armazenamento, referências de arquitetura, runbooks e catálogo de componentes. Na análise estática dos 304 arquivos Python de `core/` e `hub/`, 1.736 das 3.235 funções têm docstring. Esta contagem não avalia correção/completude, nem encontra toda a documentação externa.

A busca no escopo auditado não encontrou um registro semântico comum, um dicionário relacionado a cada função ou verificação sistemática da correspondência entre código/manual/índice a cada consulta. São capacidades a desenvolver. Não substituir contratos válidos por novos formatos concorrentes; ligar suas definições ao dicionário. Reusar catálogo, inventário, knowledge e a esteira de revisão/validação.

## 3. Modelo comum de dados do portfólio

| Camada | Conteúdo e responsabilidade |
|---|---|
| Vocabulário comum | Identificadores de conceitos, definição, namespace, nome canônico, aliases, idioma, dono, versão e estado. Conceitos compartilhados são referências versionadas. |
| Estratégia do módulo | Finalidade e minimização; origem, produtores/consumidores, fluxo e transformações; persistência, acesso, residência, retenção, exclusão, recuperação; qualidade e contratos aplicáveis. |
| Dicionário técnico | Nome lógico/físico, tipo/schema, unidade, precisão, moeda, fuso, obrigatoriedade, nullability, limites, valores permitidos, identificação/chaves, classificação e exemplos sintéticos. |
| Vinculação de símbolos | Parâmetro, retorno, campo, evento, variável semântica ou efeito ligado a termo/contrato e versão; produtores, consumidores, transformações e relações verificadas. |
| Manifesto de entrega | Digests do código, contratos, dicionário, manuais e índice; versão de regras/ferramentas, identidade de validação, ACL e evidência. |

As definições de negócio têm uma fonte autoritativa no registro semântico; assinaturas/tipos vêm dos contratos de código; persistência vem de schemas e migrações. A integração detecta conflito entre essas fontes. Nenhuma descrição inventada por IA substitui validação de comportamento.

Identificadores permanecem estáveis em refatorações. Nomes legíveis e caminhos podem mudar com aliases e histórico. Por exemplo, `portfolio.execution.run_id` e `project.billing.invoice_id` representam conceitos diferentes mesmo que alguma implementação histórica chame ambos de `id`. O parâmetro deve ser explícito no contexto e apontar para seu termo. Identificadores de clientes com o mesmo formato não são intercambiáveis sem a validação do domínio e do tenant.

Python adota `snake_case`; outra linguagem pode ter convenção diferente, com mapeamento registrado. Unidades devem estar claras no contrato e no nome quando necessário, como `timeout_seconds`. Transformação de centavos para valor decimal exige moeda e regra de conversão. Timestamp distingue instante, data civil e fuso. Uma mudança de significado pode ser incompatível sem alterar o tipo.

Todas as variáveis locais aparecem no inventário quando o adapter consegue extraí-las. Variáveis com significado de domínio têm vinculação semântica; auxiliares estruturais seguem convenção documentada e escopo local. Relações dinâmicas/inferidas ficam marcadas como desconhecidas ou pendentes, com tratamento antes do aceite se forem necessárias ao contrato. A interoperabilidade é assegurada por contratos e referências, preservando encapsulamento.

Metadados compartilhados devem passar por classificação e ACL. O registro comum não expõe dados, esquemas confidenciais ou nomes de clientes a outros projetos. Cada projeto pode estender seu namespace; promover conceito ao núcleo comum exige comparação de significado, consumidores e compatibilidade.

Base de desenho: conceitos/labels de [W3C SKOS](https://www.w3.org/TR/skos-reference/), identidade/versionamento de catálogo em [DCAT 3](https://www.w3.org/TR/vocab-dcat-3/), ligação entre significado e representação em [ODCS](https://bitol-io.github.io/open-data-contract-standard/latest/schema/). Este desenho adapta as referências; não exige um banco RDF ou uma plataforma comercial.

## 4. Contratos e estratégia por módulo

Antes de desenvolver, identificar finalidade, termos, entradas/saídas e consumidores. Registrar quem pode produzir/ler cada dado, quais operações são permitidas, classificação e regras de minimização. Definir lifecycle e obrigações legais no Grill do projeto; retenção e SLOs materiais não são inventados pelo agente.

Durante o desenvolvimento, schemas e dicionários evoluem com código e testes. Interfaces Python reutilizam Pydantic/type hints; HTTP usa OpenAPI na versão suportada pelo stack; eventos/arquivos/datasets usam schema próprio verificável. [OpenAPI 3.2](https://spec.openapis.org/oas/v3.2.0.html) foi consultado como referência publicada, sem exigir migração imediata do FastAPI. Avaliar ODCS para contratos de datasets e transformações, com versão fixada na homologação.

Um módulo sem banco de dados declara ausência de persistência, mas documenta entradas/saídas, logs, caches e efeitos aplicáveis. Uma função sem parâmetros ou retorno útil ainda possui finalidade, invariantes, exceções e efeitos. Usar N/A fundamentado, sem formular estratégias artificiais.

Cada contrato deve ter casos positivos e negativos: tipo inválido, unidade errada, campo extra, valor ausente/nulo, identidade de outro tenant, duplicação ou dado fora dos limites. Compatibilidade é testada nos dois lados da integração. Mudança incompatível recebe migração, janela de compatibilidade, depreciação e rollback no ticket; não entra escondida numa refatoração.

O [Data Contract CLI](https://cli.datacontract.com/) é candidato para lint, comparação de mudanças e testes de qualidade. Homologar extras mínimos, versão/licenças, Windows/Linux e execução isolada; a documentação do projeto registra limitações em testes Windows. Contratos externos e regras SQL são entrada não confiável. Nenhum teste usa endpoints ou credenciais de exemplos públicos, produção ou clientes.

## 5. Manual próprio de cada função

Cada função terá uma entrada estável no manual, com links de ida/volta ao código, dicionário, módulo, consumidores e testes. A extensão acompanha a responsabilidade: uma função simples pode ter referência breve; um entrypoint exige exemplos e regras completas. A cobertura inclui métodos e funções internas, com navegação hierárquica.

| Campo obrigatório | Conteúdo útil |
|---|---|
| Identidade/finalidade | ID, nome qualificado, módulo, versão, objetivo e situação de uso. |
| Uso e dados | Assinatura, parâmetros/defaults, termos do dicionário, valores/restrições e retorno. |
| Comportamento | Pré/pós-condições, invariantes, erros, idempotência, estado, concorrência e efeitos aplicáveis. |
| Segurança/operação | Dados sensíveis, permissões/capabilities, I/O/rede, limites e custos relevantes. |
| Exemplos | Caso sintético de sucesso e de erro relevante, verificados no ambiente permitido. |
| Relações | Contratos produtor/consumidor, testes, componentes, decisões e depreciação/migração. |

Campos não aplicáveis exigem motivo. Texto vazio, assinatura sem finalidade, paráfrase do nome ou comentário gerado sem conferência não aprova. Extrair estrutura com AST/Griffe em modo estático e sem fallback de import/inspection; homologar plugins/extensões como código executável. Exemplos executáveis rodam no sandbox, sem I/O privilegiado.

Reaproveitar [Griffe](https://mkdocstrings.github.io/griffe/reference/docstrings/) para parâmetros/retornos/erros e [mkdocstrings](https://mkdocstrings.github.io/) para referência e ligações entre páginas. Definições estruturais são geradas; contexto e significado são escritos e revisados junto ao código.

## 6. Manual completo de módulo e projeto

O módulo documenta responsabilidade, integração, fluxos de dados, operações suportadas, seus contratos, dependências, erros, segurança e recuperação. O projeto agrega mapa de módulos, arquitetura, configuração, tarefas do usuário/operador, manutenção, runbooks, dados, incidentes, limites conhecidos e mudanças.

Aplicar [Diátaxis](https://www.diataxis.fr/start-here/): tutoriais para aprender, guias para executar tarefas, referência para consultar e explicações para compreender decisões. Referências por função compõem o manual técnico; tarefas e fluxos compõem o manual de uso. Um índice de funções sozinho não atende ao manual do projeto.

Os agentes recebem um índice estruturado com IDs e referências verificadas; humanos recebem navegação e busca. Ambos usam a mesma versão autoritativa. Guias que exigirem configuração humana seguem AGENTS.md, com telas atuais, todos os campos/selectors e confirmação do resultado. Guias e exemplos não contêm segredos nem detalhes ofensivos privados.

Adotar docs como código, conforme a prática descrita no [TechDocs](https://backstage.io/docs/features/techdocs/how-to-guides/). Markdown com engine homologada é suficiente para começar; o Hub existente pode consumir os artefatos. Não criar uma segunda plataforma de catálogo apenas para renderizar manuais.

## 7. Atualização em cada rodada, PR e push

1. Planejamento: selecionar termos e contratos, declarar impacto documental e incluir aceite de dicionário/manual no ticket.
2. Desenvolvimento: atualizar código, contrato, dicionário e manuais na mesma rodada; regenerar a referência dos símbolos afetados.
3. Impacto: comparar assinatura, corpo, schemas, configuração e dependências; percorrer consumidores e guias relacionados. Mudança de comportamento com assinatura igual também exige avaliação.
4. Validação: reconciliar inventário e documentos; checar schemas, significado, links/anchors, exemplos, compatibilidade e classificação. A revisão independente confere se o manual explica o comportamento real.
5. PR/push: repetir para o candidato atual. Documento afetado deve conter o conteúdo correspondente; se a refatoração preservou o texto, registrar revalidação e republicação do vínculo no manifesto. Alterar apenas data não atende.
6. Portão/entrega: incorporar os checks ao portão oficial pelo percurso autorizado de governança. Publicar artefatos de código e manual associados, validando também o candidato resultante de integração.
7. Marco/final: exigir cobertura de todo o manifesto, dicionário consistente entre módulos/projetos autorizados e manual completo; nenhuma dívida documental necessária ao uso/auditoria fica silenciosa.

Refatoração deve preservar IDs dos conceitos/símbolos e corrigir links, nomes físicos, aliases e consumidores. Otimização atualiza limites e exemplos quando mudarem. Mudança real de contrato é tratada com migração e testes próprios.

## 8. Atualidade em cada consulta

Resolver projeto, autorização e versão-alvo antes da consulta. Desenvolvimento pode apontar para candidato específico; operação aponta para artefato implantado; histórico aponta para a versão solicitada. “Mais recente” é um alias explícito e atualizado apenas após publicação validada.

O manifesto é produzido após commit em armazenamento de evidências separado, ligado aos digests do código/documentação e recipe de geração; depois é vinculado ao SHA/artefato. Isso evita exigir que um arquivo contenha o hash de uma árvore que inclui o próprio arquivo. Índices de busca/RAG herdam versão, digests e ACL.

Leitura valida que código, contrato, manual e índice correspondem. Divergência/ausência retorna bloqueio explícito e aciona recuperação rastreável a partir de fonte validada. Atualizar uma consulta não autoriza inventar conteúdo ou reescrever código automaticamente. Publicação usa staging e troca coordenada do alias; falha preserva a versão anterior completa ou mantém bloqueio de compatibilidade.

Cada resposta de agente cita função/módulo, versão e localização do manual. Cache inclui projeto/versão/ACL e é invalidado em atualização/revogação. Controle vale no Hub, CLI, retrieval e montagem de contexto dos agentes; a evidência deve comprovar cada consumidor real.

## 9. Aceite e cenários mínimos

| Cenário de validação futura | Resultado esperado |
|---|---|
| Função nova sem manual/dicionário | Rodada/PR bloqueado, com símbolo e lacuna identificados. |
| Significado/unidade divergente com mesmo tipo | Contrato incompatível detectado; exige adaptação ou migração validada. |
| Mudança de comportamento sem mudar assinatura | Impacto inclui exemplos e consumidores; manual antigo não passa. |
| Rename em refatoração | IDs e referências preservados; aliases e nomes físicos atualizados. |
| Mudança afetando dois módulos | Ambos os contratos/manuais e cenários são revalidados. |
| Manual/índice de candidato anterior | Consulta bloqueada como atual; recuperação não ignora ACL. |
| Consulta de versão histórica | Manual daquela versão, identificado como histórico. |
| Metadados de outro cliente | Leitura negada; índice/cache não cruza autorização. |
| Fixture enganosa ou docstring vazia | Não aprova só por presença; exemplo e finalidade conferidos. |

Piloto: dois módulos da fábrica e um produto isolado, cobrindo termos compartilhados, um conceito homônimo, uma integração, uma refatoração e uma mudança incompatível controlada. Medir cobertura, divergências, custo e atraso de geração/consulta. Os oito itens SQ-31..38 especificam arquivos principais, dependências e validação futura; os testes citados ainda não existem.
