---
name: continuous-self-improvement
description: Reavalia preferências e falhas observadas a cada segundo prompt e em correções, rejeições ou marcos relevantes; transforma evidência em melhorias delimitadas de skills e verifica sua eficácia sem confundir testes de forma com sucesso do produto.
---

# Melhoria contínua orientada a evidências

Reutilize contexto e preferências explícitas desde o início. Reavalie a cada segundo prompt e quando houver feedback corretivo, incidente, revisão ou marco. Essa cadência agenda uma avaliação; não exige interromper trabalho independente, fazer perguntas artificiais ou alterar arquivos em todo turno.

## Diagnóstico e alteração

1. Identifique resultado esperado, observado e fonte. Separe bug reproduzido, lacuna da especificação, limitação de ambiente e preferência do owner. Pedido de continuação não é, por si só, falha do agente.
2. Registre o mecanismo e a causa sustentados pelos fatos. Use perguntas causais adicionais quando úteis; não invente cinco causas para cumprir formato. Uma hipótese não vira conclusão nem preferência global.
3. Escolha a menor mudança na skill responsável. Consolide a instrução contraditória; não acrescente outra regra dizendo o oposto no fim do documento. Preserve contexto, escopo, autorização e alternativas válidas.
4. Registre origem, domínio, evidência, mudança, limites e caminho de reversão. Preferências explícitas mantêm essa origem; propostas inferidas permanecem candidatas. Extrapole apenas quando o mesmo mecanismo foi encontrado em outro módulo.
5. Verifique sintaxe/referências/scripts e, quando o risco justificar, aplique a skill a um caso independente. O avaliador recebe pedido realista e artefatos mínimos, sem o diagnóstico ou a resposta esperada. Revisores/contas externos precisam de autorização e disponibilidade reais.

## O que conta como eficácia

- Um teste de estrutura demonstra estrutura válida; uma busca de palavras demonstra presença de texto. Nenhum dos dois prova que um agente tomará a decisão certa.
- Uma suíte sintética com 4/4 não significa 100% de eficácia de aprendizado em tarefas reais. Registrar amostra, modo, casos, limites e evidência independente.
- Regressões de comportamento devem ser verificadas por entradas/saídas observáveis. Não enfraquecer o oráculo para deixar uma correção verde.
- Melhorar uma instrução não corrige automaticamente os módulos existentes. Manter cada defeito no estado real até aplicar e validar sua correção.
- Se uma garantia não puder ser provada, reduzir a afirmação ao que foi observado e criar o próximo passo verificável.

Em planejamento/revisão de gates, evidências ou runtimes, consultar [padrões de contratos verificáveis](../02-plan-product-architecture/references/contract-review-patterns.md). Aplicar somente os padrões pertinentes ao caso.

## Persistência e permissões

No DarkFac, `.agents/skills/` é canônico; sincronize `.claude/skills/` pelo procedimento do repositório. Atualização das cópias pessoais do Codex é separada e só ocorre no escopo autorizado. Verifique hash anterior e preserve customizações fora dos arquivos alterados.

Use o ledger de aprendizado existente quando puder escrever com ownership seguro. Se outra frente o estiver modificando, grave um registro separado com origem para reconciliação; não sobrescreva o ledger compartilhado nem marque promoção por simulação. O CLI disponível pode ser consultado com `python -m core.learning.cli --help` antes de usar flags.

Uma skill não amplia permissões do sistema. Prepare o patch, valide e use o fluxo permitido para aplicar alterações protegidas. Uma recusa real permanece explícita; não criar cópia alternativa para contornar uma proibição.
