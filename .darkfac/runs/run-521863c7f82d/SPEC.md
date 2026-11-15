# SPEC

## Objetivo
Entregar triagem assistida para vulnerabilidades de dependências após o veredito determinístico, com explicação, prioridade recomendada, proposta rastreável, validação independente e fechamento somente por reteste atual.

## Fora de escopo
- Adicionar ou substituir scanners de vulnerabilidades.
- Permitir que o modelo altere aplicabilidade, severidade da fonte ou ordem efetiva dos tickets.
- Executar automaticamente propostas de correção; a execução pertence a ticket separado.
- Aprovar correções apenas por parecer textual do modelo, merge ou teste unitário isolado.
- Alterar mecanismos existentes de exceção, aceitação de risco ou governança protegida.

## Desenho
Estender o fluxo existente de CloudCoordinator, FeederCycle e BacklogFeeder com um estágio pós-veredito. O resultado determinístico permanece imutável e recebe um registro auxiliar estruturado contendo explicação, prioridade sugerida, proposta, proveniência e estado de disponibilidade. Chamadas passam pelo roteador e respeitam cotas e tetos. O control store mantém histórico e vínculos entre projeto, vulnerabilidade canônica, advisory, candidato, ambiente e evidências. Candidatos são avaliados pelo portão vigente e por revisor de família diferente. O fechamento exige reteste determinístico no ambiente afetado; mudanças relevantes invalidam evidências e reabrem a avaliação sem perder o histórico.

## Arquivos a tocar
- core/line/
- core/orchestrator/
- core/control/
- core/projects/
- core/models/
- tests/

## Riscos
- Acoplamento incorreto a um QualityCycle inexistente neste checkout em vez de reutilizar os ciclos disponíveis.
- Saída do modelo ser confundida com fato determinístico ou autorização de execução.
- Prioridade recomendada alterar indevidamente a ordenação efetiva do backlog.
- Evidências obsoletas permanecerem válidas após mudança de candidato, advisory ou ambiente.
- Deduplicação excessiva combinar vulnerabilidades ou ambientes distintos.
- Indisponibilidade, cota ou erro do modelo interromper o fluxo determinístico.
- Validação independente usar a mesma família do implementador ou não ficar vinculada ao candidato exato.
