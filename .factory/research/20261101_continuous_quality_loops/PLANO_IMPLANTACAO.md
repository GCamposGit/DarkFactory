# Implantação proposta: qualidade, valor e experimentação contínuos

Data: 01/11/2026. Estado: implantação autorizada pelo owner para encaminhamento à esteira em andamento. Complementa `INSIGHTS.md`; a fonte operacional é a fila persistente existente, projetada no roadmap da fábrica.

## Diretriz de execução e compromisso de escopo

Usar plenamente a capacidade elegível da fábrica para avançar o mais rápido possível dentro do orçamento vigente. O despacho segue disponibilidade de rota, recursos, dependências e portões; não há datas-alvo, estimativas de duração, espera por calendário ou reserva percentual nova de capacidade neste plano.

As entregas incluem implementação, validação, revisão, documentação, integração e deploy com evidência. As melhorias descobertas pelas rotinas entram na mesma fila, com seu próprio escopo e critérios. Não duplicar tickets ou serviços existentes.

A implementação registra consumo real desde a primeira rodada e opera dentro dos tetos vigentes, sem pressupor contratação ou ampliação de orçamento. A folga das assinaturas e os hosts aptos alimentam o despacho existente; piso de cota, permissões, revisão cruzada e qualidade não são flexibilizados. Trabalho independente pode avançar em paralelo quando os recursos e a coordenação existente permitirem.

## Sequência executável e critérios de saída

| Entrega | Escopo concreto | Critério para avançar |
|---|---|---|
| 1. Ciclo recorrente comum | Conferir operação e reaproveitar coordenador/feeder, cadastro, fila e roteador; persistir execução devida, cobertura, custo e achados; deduplicar/inutilizar cache vencido; conectar documentação, segurança e eficiência. | Na DarkFac, achado real gera/atualiza demanda; repetição não duplica; falha retoma; evidência válida é reaproveitada; correção percorre a esteira e é rechecada. |
| 2. Grill e experimentação | Reutilizar USR-446; registrar origem e incerteza de decisões; protocolo de experimento e comparação offline real de modelos/prompts com mesmas tarefas, custo e avaliação independente. | Decisão material não vira escolha por silêncio; comparação real produz resultado e decisão rastreáveis; inconclusivo não promove variante; simulação não altera métricas de inferência real. |
| 3. Cobertura, resultado e expansão | Conectar refatoração, interface, missão/valor e novas features; mostrar cobertura, achados, decisões, custo e efeito no Hub/resumo existentes; aplicar à fábrica e a outro projeto real, expandindo por prontidão. | Mesmo motor funciona em dois projetos; melhoria/sugestão tem objetivo e evidência; resultado pós-entrega é verificado; cobertura parcial é explícita; controles e orçamento permanecem. |

As três entregas de código seguem o fluxo normal quando autorizadas. Cada uma termina em revisão cruzada, portão oficial, verificações de PR, merge, deploy, smoke e evidência de resultado. O fatiamento interno em PRs pequenos ocorre no planejamento técnico; não se transforma cada domínio em um programa ou épico independente.

As entregas 1 e 2 são independentes entre si; a entrega 2 depende da correção existente USR-446. A entrega 3 depende das entregas 1 e 2. Verificação operacional e confirmação de reúso são atividades da primeira fatia de cada entrega, não um ticket de espera ou uma nova rodada de autorização para decisões já fornecidas. Desktop desabilitado continua excluído, e o portão completo não concorre consigo mesmo no mesmo host.

## Liberação progressiva

1. **Instalar com ativação por projeto/domínio.** Uma flag ou política existente controla o acionamento; estado e evidências ficam preservados ao desativar.
2. **Validar na DarkFac.** Exercitar geração de achados e verificar custo, duplicação e utilidade antes de liberar remediação dessa origem. Avançar assim que houver evidência suficiente, sem período de espera obrigatório.
3. **Liberar correções delimitadas.** Após evidência satisfatória, admitir demandas elegíveis na mesma esteira e nas fronteiras autorizadas. Decisões materiais e governança protegida continuam exigindo suas autorizações normais.
4. **Aplicar em um segundo projeto operacional.** Confirmar isolamento, comandos e resultados reais; não exigir onboarding, segredo ou telemetria inexistente para cumprir artificialmente o prazo.
5. **Ampliar aos projetos elegíveis.** Cadastro continua único. Cada projeto declara ativo, pausado ou não aplicável, e cobertura comprovada. Ausência de um recurso não impede os demais domínios de funcionar.

Orçamento e capacidade são herdados das políticas vigentes e aplicados pelo roteador/controle de custos existente. Limites técnicos configuráveis de candidatos e tentativas são definidos e justificados pelo planejador. Correções e melhorias pequenas dentro de objetivos aprovados usam o aceite automático normal; mudança de objetivo, regra de negócio, segredo, governança protegida ou entrega grande segue o grill/aceite vigente. Esta autorização não substitui assinatura de governança nem concede acesso novo.

Rollback operacional: desativar apenas a nova origem de rotinas por projeto/domínio, interromper novas admissões, preservar tickets/evidências e manter o motor regular de demandas. Mudanças entregues usam os mecanismos existentes de reversão. A demonstração desse comportamento integra a validação da entrega 1.

## O que a V1 entrega em cada tema

- **Segurança:** avisos aplicáveis e verificações da stack efetivamente homologadas, integradas a achados e remediação. Não declarar cobertura completa de segurança com um scanner.
- **Eficiência:** custo, tempo e retries já disponíveis; investigar maiores desvios. Instrumentação profunda de um produto sem telemetria é trabalho específico desse produto.
- **Refatoração:** selecionar pontos de maior impacto a partir de mudanças e defeitos; encaminhar intervenções pequenas verificáveis. Não reescrever o núcleo inteiro.
- **Documentação:** cobrir jornadas e mudanças relevantes com evidência, mantendo revisão periódica. Não prometer leitura e correção integral diária de todos os documentos.
- **Interface:** avaliar jornadas e acessibilidade onde houver superfície web suportada e ambiente de teste. Melhorias cosméticas precisam de hipótese de benefício.
- **Missão/valor/features:** conectar sugestão, objetivo vigente, evidência e forma de medir; submeter decisões de negócio ao owner. Não inferir missão de projeto que não esteja definida.
- **Fábrica:** reutilizar retrospectiva e verificações operacionais para realimentar a priorização; observar recorrência e custo das falhas.

## A/B online: ativação por evidência

A V1 inclui protocolo e comparação offline real, úteis imediatamente para modelos, prompts e execução. A/B online requer um produto, hipótese, instrumentação e amostra viáveis. Não é possível garantir seu prazo estatístico apenas pelo prazo do software.

O primeiro caso online usa produto instrumentado, mecanismo de flags/estatística homologado e hipótese compatível com a missão e autorização existentes. Amostra, efeito relevante e regra de parada determinam a validade; não há data de conclusão assumida. Se faltar tráfego ou pré-requisito, registrar a causa e usar comparação offline, protótipo ou teste de tarefa apropriado, sem bloquear as demais entregas nem apresentar simulação como resultado real.

## Observação após implantação

Comparar com a linha de base desde as primeiras execuções: custo por rodada e correção aceita; falhas/recorrências; achados aceitos e descartados; cobertura e dados ausentes; atraso da fila; trabalho humano; melhorias ou hipóteses invalidadas com evidência. Acompanhar prejuízo às demandas regulares e qualidade das decisões de experimento. Observação é contínua e não cria uma fase temporal que impeça trabalho pronto de avançar.

Antes/depois operacional sinaliza utilidade, mas sozinho não estabelece efeito causal. Resultados de pouco volume devem ser relatados como limitados. Não ampliar apenas por ter passado tempo: custo descontrolado, duplicação ou evidência insuficiente mantêm o domínio restrito e acionam ajuste.

## Marcos por resultado

- Ciclo real de documentação/segurança/eficiência na DarkFac, com custo e correção rastreados.
- Grill aprimorado, experimento offline real e decisão rastreável.
- Aplicação comprovada em segundo projeto e expansão por evidência, com efeito das melhorias visível.

O owner participa apenas das decisões e aceites que a política atual reserva a ele. A autorização de cadastro e desenvolvimento foi dada nesta conversa, inclusive a diretriz de capacidade máxima dentro do orçamento; não reapresentar essas mesmas decisões como perguntas pendentes.

Registrar as três entregas na fila/roadmap existentes, reaproveitando USR-446, com dependências reais, contratos autocontidos e todos os tickets em horizonte `now`. A esteira planeja as fatias pequenas, executa, revisa e entrega usando a capacidade disponível. O registro de IDs e a prova de leitura persistente acompanham este plano.

## Cobertura integral no roadmap

O owner confirmou que 100% deste plano deve constar no roadmap e autorizou seu encaminhamento enquanto estiver ausente. A composicao `CQ-20261101` agrupa USR-449, USR-450 e USR-451, reaproveita USR-446 e relaciona USR-452/USR-453 como suporte de validacao/publicacao. O epico e uma projecao de acompanhamento, sem outra execucao ou fila concorrente.

`COBERTURA_PLANO.json` liga os 20 grupos de requisitos aos tickets, aos criterios de aceite e a evidencias observaveis. Cobertura de cadastro nao significa implementacao concluida. Lacuna descoberta continua em ticket/fatia formal, sem apagar requisito, inventar sucesso ou marcar a composicao concluida antes das entregas verificadas.

A cobertura inclui seguranca de software e agentes, desempenho/recursos e custos, refatoracao, qualidade dos testes, confiabilidade e recuperacao, documentacao, interface, missao/valor, novas features, grill e experimentacao de modelos/produto, alem da infraestrutura comum e da aprendizagem. Pre-requisitos de A/B online permanecem rastreados e nao podem ser ocultados com simulacao ou significancia sem amostra.
