---
name: autonomous-piv-loop
description: Executa o ciclo contínuo Prime-Plan-Implement-Validate (PIV) de forma autônoma e com isolamento de contexto fresco por tarefa. Cada subtarefa é implementada e validada antes de avançar para a próxima. Use ao implementar tickets, funcionalidades ou correções de bugs.
---

# Autonomous PIV Loop: O Motor de Execução

O ciclo PIV decompõe a implementação em passos atômicos estritos, garantindo que o agente nunca gere um bloco maciço de código sem validação intermediária.

## Princípio Fundamental: Fresh Context Isolation
- Sessões longas degradam a atenção do modelo e geram alucinações cumulativas.
- Cada etapa (Planejar, Codificar Tarefa 1, Codificar Tarefa 2, Validar, Auditar) roda com **contexto limpo** ou via subagentes especializados (`invoke_subagent`).

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

1. **Crie uma branch ou worktree dedicada**:
   `git checkout -b feature/<task-slug>`
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
   Gere um sumário em `.factory/reports/<task-slug>-report.md` documentando os arquivos alterados e os comandos executados.

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

