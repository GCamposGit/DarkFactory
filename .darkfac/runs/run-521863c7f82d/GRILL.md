# GRILL

## Premissas
- Esta rodada permanece exclusivamente de leitura, sem arquivos, tickets, chamadas a modelos ou operações externas.
- O escopo inicial continua sendo vulnerabilidades de dependências em darkfac, conforme as premissas fornecidas; não inclui novos scanners ou produtos.
- Reutilizar CloudCoordinator, FeederCycle, BacklogFeeder, ProjectRegistry, roteador e control store; reconciliar a base entregue antes de introduzir outro ciclo, pois QualityCycle não foi identificado neste checkout.
- O modelo recebe evidências após o veredito determinístico e produz explicação, prioridade sugerida e proposta rastreável; não altera aplicabilidade, severidade da fonte nem transforma incerteza em ausência de vulnerabilidade.
- Falha ou indisponibilidade do modelo preserva o resultado determinístico e registra a assistência como indisponível, respeitando as rotas, cotas e tetos existentes.
- A validação reutiliza o portão vigente e revisão de família diferente da implementadora, vinculados ao candidato; aprovação textual do modelo não comprova correção.
- O achado somente fecha após reteste determinístico no ambiente afetado com dependências resolvidas e evidências atuais; merge ou teste unitário isolado não bastam.
- Mudanças de candidato, advisory ou ambiente invalidam evidências pertinentes e provocam reavaliação, preservando histórico e deduplicação por projeto e vulnerabilidade canônica.
- Exceções, aceitação de risco e mudanças em governança protegida seguem os mecanismos existentes, sem autorização implícita nesta demanda.

## Decisoes do owner
- (nenhuma pergunta bloqueante)

## Aguardando decisao do owner
- [intent] Nesta entrega, as propostas de correção devem apenas ser apresentadas ou também executadas? (id=q1)
- [business] A prioridade sugerida pelo modelo pode mudar a ordem de execução dos tickets? (id=q2)
