# Qualidade, valor e experimentação contínuos na DarkFac

Pesquisa em 01/11/2026, solicitada pelo owner. Base local inspecionada: `b585a191addce4be54a4abd0f051d8e587949e02`. A pesquisa começou fora da esteira; depois o owner autorizou o cadastro da implantação e sua execução pela esteira existente. O contrato vigente está em `PLANO_IMPLANTACAO.md`: avançar com capacidade máxima elegível dentro do orçamento, por dependências e evidências, sem prazos estimados. Esta pesquisa não é homologação de ferramentas nem comprovação de execução em produção.

## 1. Conclusão

Estender os mecanismos existentes com um ciclo comum de melhoria: **observar → qualificar evidência → priorizar → corrigir ou experimentar → entregar pela esteira → medir efeito → atualizar conhecimento**. Segurança, eficiência, refatoração, documentação, interface, valor do produto e evolução da fábrica são domínios desse ciclo. Não precisam de sete agendadores, sete filas ou sete agentes permanentes.

O objetivo é aumentar resultado útil por custo total e reduzir risco e intervenção humana. Ocupação de hardware, consumo de assinatura, quantidade de tickets, linhas modificadas e volume de documentos não são critérios de sucesso. Capacidade disponível pode acelerar trabalho útil e experimentos relevantes; não justifica produzir mudanças sem benefício plausível.

A preferência por mecanismos simples com evidência externa é coerente com [Building effective agents](https://www.anthropic.com/research/building-effective-agents). O artigo tem aviso sobre evolução das ferramentas desde 2024; usamos os princípios, não uma arquitetura de fornecedor como pacote a instalar. O estudo [Agentless](https://arxiv.org/abs/2407.01489) oferece uma comparação histórica em reparo de software; não prova desempenho atual na DarkFac. A aplicação local é uma inferência a validar.

## 2. Método e limites

- Consultar primeiro pesquisas e planos existentes, depois atualizar fontes externas pertinentes.
- Priorizar documentação de autores/projetos, pesquisas originais e padrões; não usar rankings de popularidade como prova de qualidade.
- Distinguir código encontrado, chamador encontrado e operação real comprovada. Nesta sessão foram inspecionados arquivos e chamadores, sem sondar produção, executar scanners, comparar modelos pagos ou rodar o portão completo.
- Papers Agentless, Efficient Agents e o survey de autocorreção foram consultados em metadados e resumos; não houve replicação experimental ou revisão integral de todos os experimentos.
- Nenhuma frequência universal foi encontrada nas fontes. Cadências propostas neste documento são escolhas de desenho, sujeitas a custo, risco, mudanças e decisões do owner.
- Nenhum pacote foi instalado, código de terceiro copiado, serviço novo contratado ou teste A/B ativado.

## 3. Base existente e reúso

| Capacidade | Evidência local | O que reaproveitar / limite da evidência |
|---|---|---|
| Cadastro e comandos por projeto | `core/projects/models.py:130` | `ProjectDescriptor` já contém repositório, ambiente, comandos, deploy, smoke e aceite comercial. Usar esse cadastro e referências a missão/objetivos existentes; não criar outro catálogo de projetos. Não há no modelo inspecionado um contrato tipado de objetivos mensuráveis e experimentos. |
| Acionamento e admissão | `core/orchestrator/cloud_coordinator.py:92,399`; `core/line/feeder_cycle.py`; `core/line/backlog_feeder.py` | Coordenador chama o feeder; ele respeita intervalos, slots, elegibilidade e submissão idempotente. Feeder consome demandas existentes; não faz a descoberta semântica de melhorias. |
| Entrega única | `run_ticket.py`; `core/line/owner_intake.py`; `docs/manual/motor.md` | Achados aprovados devem gerar/atualizar demandas pelo caminho existente; manter portão, revisão cruzada, merge, deploy e evidência. |
| Segurança e documentação já pesquisadas | `docs/plans/security-quality-2026-10-08/RESEARCH.md`, `roadmap.json`; `.factory/reports/security-quality-usr-191-reconciliation.md` | Reutilizar seleção de ferramentas e requisitos. O roadmap histórico contém 38 entradas SQ; não ativá-las em bloco nem tratá-las como implantadas. O ledger externo citado nesse RESEARCH não estava presente no caminho indicado neste checkout; este dossiê mantém proveniência própria. |
| Canário, sentinela e dogfood | `core/line/canary.py`, `sentinel.py`, `dogfood.py` | Já há funções de verificação e consumo autônomo do backlog. Dogfood exige elegibilidade e dependências; não é mecanismo genérico para inventar features. Não foi conferido o estado vivo dos serviços. |
| Documentação | `core/harness/docs_sync.py`; `docs/manual/docs_sync.md` | Checagem relaciona código alterado e páginas declaradas. Isso não atesta correção semântica, cobertura completa ou executabilidade dos procedimentos. |
| Qualidade e economia de validação | `core/harness/runner.py`, `affected.py`; `scripts/typecheck.py`, `complexity_ratchet.py`; `.github/workflows/ci.yml` | Reusar cache, testes afetados, verificação de tipos e ratchet. Não rodar várias suítes completas competindo por host. Ausência de scanners nos arquivos inspecionados não demonstra ausência em todo serviço/projeto. |
| Retrospectiva | `core/line/bindings.py:578`; `core/line/retrospective.py` | Handler chama extração determinística de lições, sem LLM. Há escrita em `.darkfac/LESSONS.md` e no tracker. `stage_grill.py:190` lê lições locais. Não presumir que toda lição muda decisões futuras ou melhora resultados. |
| Promoção de aprendizados | `core/learning/promotion.py`, `service.py` | Já existem avaliação, promoção e rollback. Chamadas encontradas no serviço/contexto não provam acionamento periódico em produção. Conectar ao consumidor certo antes de criar outro motor de aprendizagem. |
| Custo e capacidade | `core/line/routing.py:1016`; `core/usage/ticket_cost.py`; `core/portfolio/scheduler.py`, `budget_manager.py` | Roteador de produção deve continuar único. `PortfolioScheduler` e o roteador auxiliar de benchmarks existem, mas as buscas não comprovaram sua adoção pela admissão de produção. Reusar contratos úteis sem introduzir rota concorrente. |
| Grill | `core/line/stage_grill.py`; `core/demands/grill.py`; `docs/manual/grill.md` | Categorias do owner e registro de premissas já existem. Falta representar uma incerteza resolvível por experimento e ligá-la a um resultado observado. Ver achado G1 abaixo. |
| Comparação de modelos | `core/benchmarks/racing.py:221,452`; `core/benchmarks/cli.py:270,299` | Motor separa inferência real, validator-only e simulação no ledger. Cascata é útil para execução econômica. O torneio padrão usa fixtures com validadores, não comparação real de modelos. Não substituir esse ledger ou declarar que uma cascata mede efeito causal. |

## 4. Práticas que orientam a feature

**Qualidade precisa fechar o ciclo.** O efeito da mudança deve aparecer no comportamento do sistema e do usuário. [DORA](https://dora.dev/guides/dora-metrics/) acompanha entrega por cinco métricas: tempo até produção, frequência de deploy, recuperação de deploy falho, taxa de falha e retrabalho de deploy. Usar por aplicação e ao longo do tempo; não criar competição entre projetos incomparáveis. Acrescentar custo por entrega aceita, risco, tempo humano e métrica do objetivo de cada projeto.

**Confiabilidade direciona prioridade.** A [política de error budget do Google SRE](https://sre.google/workbook/error-budget-policy/) ilustra como falhas de confiabilidade redirecionam esforço para estabilização. A DarkFac deve adotar a lógica com limites próprios, sem copiar números do exemplo. [Eliminating Toil](https://sre.google/workbook/eliminating-toil/) recomenda medir esforço economizado e comparar benefício ao custo de criar e manter a automação.

**A IA interpreta; ferramentas e observações verificam.** Combinar testes executáveis, análise estática, métricas, revisão e avaliação de tarefas. O guia de [evals de agentes](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) diferencia avaliadores por código, modelo e pessoas. O [survey de autocorreção](https://arxiv.org/abs/2406.01297) sustenta cautela com autoavaliação sem feedback confiável; sua conclusão é limitada aos estudos analisados, não uma impossibilidade universal de autocorreção.

**Segurança é contínua e inclui o agente.** Usar [SSDF](https://csrc.nist.gov/projects/ssdf/publications) e [OWASP para agentes](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html) para cobrir dependências, segredos, privilégios, injeção de instruções, memória e abuso de ferramentas. Na página NIST consultada, SSDF 1.1 é final e 1.2 é draft. Mudanças em prompts, ferramentas ou memória também podem exigir reavaliação. Material encontrado por um loop é dado de baixa confiança, sem autoridade para ampliar permissões ou reescrever governança.

**Refatoração deve melhorar capacidade de mudança.** Priorizar módulos com mudanças frequentes, falhas, duplicação ou gargalos medidos. O argumento de [Martin Fowler](https://martinfowler.com/articles/is-quality-worth-cost.html) liga qualidade interna à facilidade de evoluir software; não justifica reescrever módulos estáveis para atender uma pontuação estética.

**Documentação é uma tarefa de uso.** Usar [Diátaxis](https://diataxis.fr/) para distinguir tutorial, procedimento, referência e explicação, conforme necessidade real. Checar jornadas como instalar, operar, recuperar e integrar, além de contratos e links. [DORA sobre documentação](https://dora.dev/capabilities/documentation-quality/) dá suporte à atenção à clareza, encontrabilidade e confiabilidade; não fornece uma fórmula automática de completude.

**Interface deve ser avaliada por tarefas e acesso.** Combinar sucesso em jornadas, erros, tempo, responsividade e acessibilidade. [W3C WAI](https://www.w3.org/WAI/test-evaluate/) ressalta que ferramentas sozinhas não determinam conformidade. [Web Vitals](https://web.dev/articles/vitals) cobre carga, interação e estabilidade; métricas de laboratório não equivalem automaticamente à experiência de campo. Evitar mudanças cosméticas recorrentes sem benefício demonstrável.

**Features precisam de hipótese e vínculo ao objetivo.** [DORA sobre feedback](https://dora.dev/capabilities/customer-feedback/) recomenda verificar se o produto resolve problemas e é usado. Para DarkFac, a sugestão deve ligar missão vigente → necessidade observada → hipótese → métrica → menor intervenção. Uma hipótese pode resultar em simplificação ou retirada de funcionalidade; não precisa sempre aumentar o produto.

## 5. Cadência proposta, adaptada por projeto

| Domínio | Disparo preferido | Verificação periódica proposta | Evidência de melhoria |
|---|---|---|---|
| Segurança | Dependência, exposição, permissão, prompt/ferramenta ou advisory novo | Atualização diária de avisos; revisão mais profunda por risco | Vulnerabilidade aplicável corrigida; teste de abuso continua bloqueado |
| Confiabilidade e dados | Falha, deploy, erro de integridade, restauração | Reusar canário, backup e sentinela; adaptar ao serviço | Jornada real passa, restauração comprovada, menos incidentes |
| Eficiência | Regressão de latência, uso de memória/CPU, tokens, custo ou retries | Consolidado diário de telemetria; investigação quando relevante | Custo total/tempo menor preservando qualidade e privacidade |
| Refatoração | Área alterada e com retrabalho, erro ou dificuldade recorrente | Seleção semanal de pontos de maior retorno | Mudança mais simples, menos falhas ou tempo de manutenção menor |
| Documentação | Mudança de comportamento ou procedimento que falhou | Revisão semanal das lacunas e áreas ainda não cobertas | Procedimento funciona; usuário encontra e executa a tarefa |
| Interface/acessibilidade | Tela/jornada alterada, erro ou feedback | Revisão semanal das jornadas relevantes, quando aplicável | Mais sucesso na tarefa, menos erro/abandono, barreira removida |
| Missão, valor e novas features | Feedback e resultado de entrega/experimento | Revisão semanal de evidência e prioridades | Objetivo vigente atendido ou hipótese rejeitada com aprendizado |
| Processo da fábrica | Falha de gate, retrabalho, intervenção ou rota ruim | Retrospectiva por execução e consolidação semanal | Mesma falha recorre menos; menor custo por entrega aceita |

Diário/semanal são pontos de partida discutíveis, não autorização de ativação. Projeto parado não precisa de releitura semântica completa diária. Mesmo sem commit novo, um advisory, mudança de API externa, erro operacional ou vencimento de evidência pode justificar trabalho. Estado arquivado/pausado e obrigações remanescentes precisam de política explícita.

## 6. Economia de tokens, capacidade e fila

1. Consultar artefatos e telemetria existentes antes de novas chamadas. Guardar identificador, versão, trecho e resultado; contexto integral apenas quando necessário.
2. Triagem determinística primeiro. Hash/diff e versão dos insumos invalidam o cache: código, dependências, regras, prompts, objetivos e fontes externas relevantes. Cache não pode ocultar advisory novo ou resultado vencido.
3. Dar ao LLM o conjunto afetado e a evidência útil. Fazer leituras amplas de forma espaçada ou por risco, com cobertura acumulada visível.
4. Identificar achados por projeto, causa e alvo. Atualizar ticket aberto ou suprimir com motivo/validade; nunca gerar a mesma demanda em cada rodada.
5. Usar a fila, leases, slots e roteador existentes. Persistir execução devida e retomada; uma queda não deve gerar avalanche de rodadas atrasadas. [CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/) documenta por que acionamento periódico exige tarefas idempotentes; isso não é recomendação de adotar Kubernetes.
6. Priorizar por risco obrigatório e retorno esperado, considerando confiança, custo de investigar/implementar, manutenção e oportunidade de adiar outra entrega. Não atribuir números falsamente precisos a benefício incerto.
7. Limitar trabalho em curso, candidatos, tentativas, tempo e custo por rodada. Usar capacidade ociosa em backlog elegível e dúvidas importantes; interromper quando o benefício marginal não justificar o custo. Piso de cota e tetos existentes permanecem.
8. Medir custo do ciclo completo, inclusive comparações perdedoras, testes, revisão, deploy, falhas e follow-up. O paper [Efficient Agents](https://arxiv.org/abs/2508.02694) usa custo por sucesso; os números de GAIA não são promessa de economia local.

O loop só conta como bem-sucedido quando há resultado verificável. Seus estados precisam distinguir sucesso sem achado, execução parcial, falha, bloqueio por recurso e não aplicável. Toda rodada deve registrar o que efetivamente avaliou e o que ficou fora. Repetição sem ganho ou excesso de falsos positivos reduz a frequência e gera ajuste delimitado.

## 7. Grill orientado a objetivos e incerteza

Manter perguntas curtas, no máximo três por rodada, com opção recomendada e impacto. O contrato do projeto/feature deve registrar público, problema, objetivo, sucesso observável, restrições e quem aceita. Perguntar apenas a informação material ausente; aproveitar respostas já confirmadas na mesma demanda.

Classificar cada dúvida:

- **Preferência, objetivo, restrição ou autorização do owner:** pedir decisão explícita quando necessária. Experimento não decide valores ou concede autorização.
- **Fato pesquisável:** consultar código, dados, documentos ou fonte primária, registrando validade e procedência.
- **Escolha técnica reversível com evidência suficiente:** resolver e registrar a justificativa.
- **Incerteza empírica relevante:** desenhar o menor experimento capaz de mudar a decisão.
- **Sem evidência viável no orçamento/prazo:** registrar incerteza residual; adotar opção reversível de menor risco dentro da autorização ou manter o bloqueio material.

Adicionar à decisão uma referência de origem (`owner`, evidência, decisão técnica ou experimento) e validade. Se um experimento puder resolver a dúvida, o grill estabelece objetivo e limites; não exige que o owner adivinhe qual variante funcionará melhor. O planejamento pode continuar nas partes independentes e parar apenas onde a decisão é realmente necessária.

### Achado G1 registrado: USR-446

Inspeção estática de `stage_grill.py:964-980` mostra adoção de recomendação após 12h para categorias materiais e finalização por prazo vencido. `stage_grill.py:760-765` envia questões além das três primeiras para `technical`, mesmo se pertencerem ao owner. `core/demands/grill.py:338-343,549` também possui resolução automática de perguntas não respondidas. Isso diverge da exigência atual de decisão explícita para ambiguidade material. É achado de código/documentação, não incidente comprovado em produção. O ajuste deve preservar políticas explícitas de canário e perguntar em rodadas quando houver mais decisões materiais.

Registrado em 01/11/2026 como **USR-446**, status `planned`, via `run_ticket.py --create --queue-only`. A primeira leitura no sandbox não alcançou o PostgreSQL e apresentou fallback em memória; nenhuma escrita foi feita nesse backend. O registro ocorreu após liberação de rede pelo ambiente, com `mock_mode=False` verificado e releitura da linha após a criação. O ticket é correção de defeito identificado na pesquisa, separado da feature ainda em discussão. Não houve implementação.

## 8. Experimentação sem criar uma plataforma própria

| Dúvida | Método inicial econômico | O que não concluir |
|---|---|---|
| Qual modelo/prompt/rota oferece melhor custo-benefício? | Comparação offline pareada em tarefas representativas; mesmo estado e critérios, execuções isoladas, repetições quando necessárias | Cascata com escalonamento não mede vencedor causal; validação de fixture não mede inferência real |
| Qual UI facilita uma tarefa? | Protótipo e teste de tarefa; A/B online se houver volume e instrumentação suficientes | Avaliação visual de um modelo ou preferência do owner não prova comportamento do público |
| Qual feature vale desenvolver? | Feedback, dados, protótipo e piloto delimitado; depois A/B quando viável | Não é necessário desenvolver duas features completas antes de aprender |
| Otimização/refatoração melhora o sistema? | Benchmark repetido, perfil e validação de equivalência, seguido de rollout controlado | Comparar períodos com tráfego diferente não isola o efeito |
| Projeto com um usuário ou pouco tráfego | Avaliação de tarefas, piloto e evidência qualitativa, declarando o limite | Não apresentar significância estatística sem amostra adequada |

O protocolo mínimo de experimento deve guardar: pergunta; hipótese; controle/variantes e versões; população ou conjunto de tarefas; unidade de comparação/randomização; métrica principal; limites de segurança, qualidade, latência e custo; amostra/duração e regra de parada; orçamento; instrumentação; condições de adoção/rollback; resultado, incerteza e validade.

Usar métricas de produto coerentes com a missão. [Microsoft ExP, pré-experimento](https://www.microsoft.com/en-us/research/articles/patterns-of-trustworthy-experimentation-pre-experiment-stage/) fundamenta hipótese, métrica e desenho da amostra. [Microsoft ExP, acompanhamento](https://www.microsoft.com/en-us/research/articles/patterns-of-trustworthy-experimentation-during-experiment-stage/) fundamenta métricas de proteção e cautela com consultas repetidas. [Diagnóstico de SRM](https://www.microsoft.com/en-us/research/publication/diagnosing-sample-ratio-mismatch-in-online-controlled-experiments-a-taxonomy-and-rules-of-thumb-for-practitioners/) trata desvios na alocação que podem invalidar conclusões.

Na implementação: pré-definir método estatístico adequado, potência/efeito mínimo relevante e tratamento de múltiplas comparações; não parar arbitrariamente no primeiro resultado favorável. Inconclusivo não equivale a empate nem ausência de efeito. Avaliar dados defeituosos antes de promover variante. Teste A/A é útil para conferir instrumentação, não precisa virar ritual em toda mudança. Rollout canário mede segurança da liberação; sozinho não demonstra benefício causal.

Para modelos, usar avaliador independente quando houver julgamento subjetivo e conferi-lo contra exemplos com resultado conhecido. Custo total por tarefa aceita e qualidade mínima prevalecem sobre preço por token ou Elo isolado. Separar amostra de seleção e confirmação; comparar em tarefas pareadas para evitar confusão pela dificuldade dos tickets e condições de cota/host.

## 9. Componentes externos candidatos

| Necessidade | Reúso sugerido | Estado nesta pesquisa |
|---|---|---|
| Dependências | Scanners já avaliados no plano SQ; considerar [OSV-Scanner](https://google.github.io/osv-scanner/) para ecossistemas cobertos | Documentação/repositório consultados; Apache-2.0 declarado. Selecionar um caminho coerente com a stack, sem empilhar scanners equivalentes. |
| Jornadas e acessibilidade | Harness web existente, ou [Playwright com axe](https://playwright.dev/docs/accessibility-testing) onde faltar capacidade | Playwright declara Apache-2.0; integração axe possui licença própria. Não houve instalação, execução nem homologação Windows/Linux nesta sessão. |
| Flags e análise A/B | Avaliar adapter/SDK/estatística de [GrowthBook](https://docs.growthbook.io/) ou flags existentes | [Repositório](https://github.com/growthbook/growthbook) declara núcleo MIT e diretórios comerciais. Não copiar/instalar conjunto sem verificar artefato, suíte, licença e custo operacional. Não é seleção final. |
| Alternativa para gestão de flags | [Unleash](https://docs.getunleash.io/) | Documentação consultada. Flag não substitui análise estatística. Comparação detalhada e homologação de licença/suíte não feitas. |
| Documentação | Markdown e contratos existentes; componentes do plano SQ quando necessários | Não implantar Backstage ou outro portal apenas para viabilizar a revisão. |

A infraestrutura enxuta da fábrica favorece integrar capacidades headless existentes. Uma plataforma externa completa só se justifica quando economizar mais do que seu custo de operar e manter. Repositórios consultados não estão homologados; estrelas, página de licença e presença de CI não substituem execução e integração real.

## 10. Escopo pequeno autorizado para encaminhamento

Três partes de uma mesma feature, detalhadas nos tickets autocontidos da implantação:

1. **Conectar sinais ao motor existente:** política por projeto, execução devida persistida, cache/invalidação, achados com evidência e deduplicação, custo e priorização na fila atual. Reutilizar fontes já disponíveis em todos os domínios; não exigir sete agentes novos.
2. **Aprimorar grill e experimentos:** decisões explícitas, incertezas rastreadas, protocolo mínimo e comparação offline real de modelos; adapter de A/B online apenas quando houver um projeto com necessidade e amostra viáveis.
3. **Fechar o efeito:** verificar resultado depois da entrega, atualizar aprendizado, reverter quando necessário e mostrar status/custo/resultado no Hub e no resumo existente.

Critérios de demonstração: mesmo achado não duplica ticket; projeto sem mudança reaproveita evidência válida; advisory novo invalida o necessário; falha de máquina retoma sem duplicar; decisão material permanece pendente; experimento pode terminar inconclusivo; melhoria entregue altera métrica ou comportamento; outro projeto usa o mesmo mecanismo com perfil próprio; tentativa inválida não promove rota nem aprendizado.

Fronteiras para implementação: usar orçamento, roteador e prioridade vigentes, sem criar reserva percentual ou elevar gastos. Correções e melhorias pequenas dentro de objetivos aprovados seguem o aceite automático normal; mudança de objetivo, regra de negócio, segredo, governança ou entrega grande segue o grill/aceite vigente. Experimentos com exposição real precisam dos pré-requisitos e da autorização aplicável ao projeto. O owner autorizou a execução desta feature nesta conversa; não repetir essa autorização como pergunta pendente.

Diretrizes explícitas do owner nesta demanda: aplicar a todos os projetos e à fábrica; abranger segurança, eficiência, refatoração, documentação, interface, valor e novas features; melhorar o grill; resolver dúvidas empíricas por experimentação quando compensar; pesquisar e reaproveitar antes de desenhar; evitar roadmap extenso e desperdício de tokens. A instrução final autoriza abrir os tickets no roadmap de implantação para a esteira assumir, usando plenamente a capacidade disponível dentro do orçamento e sem prazos no plano. Rotinas só são ativadas após desenvolvimento e validação reais.

## 11. Explicação curta para o owner

A fábrica passa a cuidar do software como uma equipe que observa o uso e a operação, escolhe poucas melhorias justificadas e confere o efeito delas. O calendário garante que nenhum tema seja esquecido; eventos evitam esperar pela próxima rodada quando algo mudou. Quando falta conhecimento, a fábrica pesquisa ou experimenta. O grill continua reservando para você objetivo, limites e decisões de negócio.

Uma inspeção cria evidência, uma sugestão cria uma hipótese e uma entrega cria uma mudança. Só a verificação do efeito fecha o ciclo de melhoria.
