---
name: autonomous-piv-loop
description: Executa tickets, funcionalidades e correções pelo ciclo Prime-Plan-Implement-Validate (PIV), incluindo paralelização segura em branches e worktrees isoladas quando duas ou mais frentes escrevem no mesmo repositório.
---

# Autonomous PIV Loop: O Motor de Execução

O ciclo PIV decompõe a implementação em passos atômicos estritos, garantindo que o agente nunca gere um bloco maciço de código sem validação intermediária.

## Princípio Fundamental: isolamento de contexto e estado
- Sessões longas degradam a atenção do modelo e geram alucinações cumulativas.
- Cada etapa (Planejar, Codificar Tarefa 1, Codificar Tarefa 2, Validar, Auditar) roda com **contexto limpo** ou via subagentes especializados (`invoke_subagent`).
- Isolamento de contexto não substitui isolamento Git: cada frente que escreve recebe branch, worktree, owner e lease exclusivos.
- Nunca faça fan-out a partir de um checkout sujo sem antes criar um backup recuperável e um checkpoint de integração limpo. Copiar uma baseline suja para várias worktrees duplica deltas e torna autoria e integração ambíguas.

Quando houver duas ou mais frentes concorrentes, ou quando resultados de worktrees precisarem ser integrados, leia e siga integralmente [references/worktree-parallelism.md](references/worktree-parallelism.md). O protocolo é fail-closed: se não for possível provar baseline, ownership, ambiente de teste ou conclusão, não despache nem integre.

## O Loop em 5 Etapas

```text
[Prime] Contexto Mínimo Necessário
   │
   ▼
[Plan] Decomposição em Micro-tarefas com comandos de validação
   │
   ▼
[Implement] Tarefa N (Local com qwen-fast/qwen-deep ou Nuvem com Claude 3.7/DeepSeek)
   │
   ▼
[Validate Step] python core/harness/runner.py --quick
   │ (Se falhar: corrige imediatamente. Não acumula erros)
   ▼
[Loop para Tarefa N+1 até concluir todas]
   │
   ▼
[Validate Full] python core/harness/runner.py
   │
   ▼
[Adversarial Review] Nível 1 (gpt-review local) -> Nível 2 (Nuvem Cruzada)
```

## Instruções de Execução por Tarefa

1. **Prepare a unidade de trabalho**:
   - Em uma frente única, use uma branch dedicada.
   - Em paralelismo, use o protocolo de worktrees referenciado acima; não reutilize o checkout raiz como executor.
   - Registre o SHA-base e confirme `python`, `pytest` e os comandos focais antes da escrita.
2. **Execute tarefa a tarefa**:
   - Abra apenas os arquivos explicitamente listados no ticket.
   - Escreva o código seguindo os padrões do `AGENTS.md`.
   - **Execute o comando de validação rápida imediatamente**:
     `python core/harness/runner.py --quick`
   - Se falhar, corrija agora. Proibido avançar com testes rápidos em vermelho.
3. **Validação Final da Suíte**:
   - Execute a suíte completa com marcadores determinísticos:
     `python core/harness/runner.py | python core/harness/markers.py`
4. **Relatório de Implementação**:
   Gere um sumário em `.factory/reports/<task-slug>-report.md` documentando SHA/branch/worktree, arquivos alterados, comandos executados, resultados e estado residual.

## Contrato de conclusão

Uma frente só está pronta para integração quando entrega um commit seletivo e alcançável contendo apenas seu delta, acompanhado de: ticket e owner; SHA-base e SHA final; lista de arquivos; testes focais e obrigatórios com exit code; relatório; e `git status` residual explicado. Turno concluído sem esse contrato, título/ID ausente, teste indisponível ou commit misturado é falha de handoff, não sucesso.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Gatilho de Auto-Avaliação no 2º Prompt da Sessão**:
   - Se a implementação for desencadeada por um follow-up ou se estiver no 2º prompt da sessão, execute obrigatoriamente a verificação de intenção via `core/learning/cli.py record-turn`.
   - Avalie a causa da necessidade de intervenção humana anterior e incorpore as preferências no plano antes de codificar.
2. **Root Cause Analysis em Toda Quebra de Validação (`--quick` ou `Full`)**:
   - Nunca faça tentativas aleatórias (*trial and error*) ao encontrar um teste falhando.
   - Aplique o diagnóstico 5-Whys: Identifique a causa raiz exata (ex.: tipo incompatível, mock desatualizado, path no Windows com barras invertidas).
   - Registre o RCA no ledger (`python core/learning/cli.py rca`) e aplique o patch de forma que o erro não possa se repetir.
3. **Execução One-Shot com Cobertura Preventiva**:
   - A entrega deve ser completa na primeira passada: código tipado, testes unitários para a nova funcionalidade, documentação atualizada e zero dependências soltas.

