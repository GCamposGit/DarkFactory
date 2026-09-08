# Política obrigatória de planejamento e handoff

Versão: 1.0 — 08/09/2026. Origem: instrução explícita do owner nesta sessão. Aplicável a toda funcionalidade, correção, refatoração, script, skill e configuração desenvolvida pela Dark Factory, inclusive alterações na própria fábrica.

## Regra central

**Um modelo de alta inteligência especifica e aprova o plano concreto de implementação. Um modelo econômico implementa e executa a validação planejada. Nenhuma implementação começa sem um handoff pronto e vigente.**

A regra é permanente, não uma recomendação para tarefas complexas. Para tarefas simples, o plano pode ser curto, mas deve conter os mesmos elementos de prontidão. Um template genérico, um título de issue ou uma descrição de épico não substituem um plano específico revisado por modelo de alta inteligência.

O papel de alta inteligência pode ser ocupado pelo Astra ou outro modelo qualificado no catálogo de capacidades. O implementador econômico é escolhido por capacidade demonstrada, custo, disponibilidade e cota; não fixar nomes comerciais como condição permanente. A ausência de um planejador qualificado suspende novos planejamentos, sem autorizar o implementador a assumir esse papel. Tickets já aprovados e vigentes continuam executáveis.

## Sequência obrigatória

1. **Especificar — alta inteligência:** interpretar intenção, inspecionar os pontos necessários da base e resolver arquitetura, interfaces, comportamento, erros, validação e limites.
2. **Fatiar — alta inteligência:** produzir unidades pequenas com dependências explícitas, normalmente até quatro arquivos principais por ticket. Relatórios e espelhos gerados precisam ser declarados; não são autorização para ampliar escopo.
3. **Validar prontidão — controle determinístico + revisão de alta inteligência:** conferir campos, dependências, hashes e autorização de escopo. A qualidade semântica do plano exige julgamento do planejador; um formulário preenchido não prova completude.
4. **Implementar e testar — econômico:** seguir o contrato, executar scripts/verificadores, corrigir defeitos dentro dos critérios e devolver evidências. Executar comandos determinísticos não exige uma chamada de IA por comando.
5. **Revisar — independente:** revisão técnica conforme risco, com retorno ao implementador para defeitos de execução. Escolhas arquiteturais, alteração de critérios ou lacunas retornam ao planejador de alta inteligência.
6. **Entregar — supervisor:** integrar somente evidências válidas, aplicar a política GitHub/produção e registrar resultado real. O owner decide apenas aquilo que exige intenção, acesso ou julgamento humano.

## Contrato de prontidão

Todo handoff deve declarar:

- `ticket_id`, `parent_id`, objetivo, origem, versão do plano e documento canônico;
- identidade do planejador, capacidade/nível, registro de aprovação do plano e referência à sessão/run que o produziu;
- baseline de referência e hashes dos arquivos relevantes; dependências e artefatos que devem existir antes da execução;
- inputs, outputs, interfaces, exemplos, invariantes e casos de erro;
- arquivos principais permitidos, arquivos apenas de leitura, non-goals e ordem de implementação;
- decisões já tomadas e valores configuráveis; nenhuma dúvida arquitetural aberta no ticket liberado;
- critérios de aceitação numerados e verificadores externos ao texto do implementador;
- comandos de validação e condições de existência dos testes novos;
- orçamento/tentativas, ambiente necessário e permissões para efeitos externos;
- rollback/limpeza e evidências mínimas de handoff remoto;
- condições específicas de escalonamento e próximo destinatário.

Fluxo lógico: `needs_spec → planning_high → ready_for_handoff → implementing_economy → validating → independent_review → delivered`. Estados auxiliares: `waiting_dependency`, `waiting_access`, `needs_replan`, `failed_validation`. São estados propostos para o futuro controle HF; não renomeiam silenciosamente os estados atuais de DF-01.

### Gate do despacho

O futuro verificador admite despacho somente se: plano aprovado por identidade de planejador qualificada; versão e integridade conferidas; dependências satisfeitas; baseline relevante compatível; escopo e autorização válidos; ambiente/cota disponíveis. A classificação de modelo e a aprovação vêm do registro confiável do supervisor. Campos escritos pelo implementador como `planner_tier=high` ou `approved=true` não são prova de autoridade. Hash detecta alteração, não autentica o autor.

Mudança de SHA por ticket predecessor é esperada. Conferir arquivos/contratos relevantes contra o estado aprovado e rebase validado; não invalidar todos os planos por uma alteração não relacionada. Mudança nos contratos, critérios, permissões ou arquivos relevantes leva a `needs_replan` e exige revisão de alta inteligência.

O gate não está implementado nesta revisão documental. **Até HF-04/HF-06/HF-07, o agente coordenador deve cumprir este procedimento explicitamente usando o workflow PIV atual.** Esses pacotes deverão materializar respectivamente o contrato/validador, as instruções/espelhos e o despacho dos papéis. Não declarar fiscalização automática já existente.

## Escalonamento sem transferir arquitetura ao implementador

O agente econômico pode escolher nomes internos, dividir funções privadas, corrigir erros de implementação e elaborar testes adicionais coerentes com os critérios. Não pode trocar runtime, mudar contrato público, enfraquecer um gate, alterar escopo ou ativar uma dependência/serviço não previsto.

Após duas tentativas focais sem progresso sobre a mesma causa, ou imediatamente diante de lacuna arquitetural, entregar ao planejador: sintoma, evidência curta, hipótese, opções e recomendação. O planejador corrige o plano e devolve nova versão para execução econômica. O escalonamento não troca silenciosamente o papel de implementação por um modelo caro; qualquer exceção a essa separação exige instrução explícita posterior do owner.

A aprovação do plano pelo modelo qualificado é diferente do aceite de produto pelo owner. Não criar uma aprovação humana adicional por ticket. Preservar a política de produção de clientes pagantes.

## Handoffs preparados

- [HF-01 — baseline e reconciliação](handoffs/HF-01.md): seis tickets, com revisão final do pacote por alta inteligência.
- [HF-02 — experimento e decisão de runtime](handoffs/HF-02.md): oito tickets; implementação/medição econômicas, decisão arquitetural final de alta inteligência.

Os handoffs estão especificados para execução sequencial, condicionada às dependências e ao preflight de cada sessão. Eles não autorizam iniciar o desenvolvimento nesta sessão de planejamento. O registro estruturado está em `.factory/planning/hf01-hf02-handoffs.json`; seu status descreve o plano, não a conclusão da funcionalidade.
