---
name: meta-skills-evolver
description: Analisa o histórico de execuções da fábrica autônoma, audita drift de regras, gasto de tokens e falhas recorrentes para sintetizar novas skills, calibrar a matriz de roteamento de modelos e atualizar o repositório. Use periodicamente para auto-aperfeiçoamento do ecossistema de agentes.
---

# Meta-Skills Evolver: Auto-Evolução do Ecossistema

Uma fábrica de software autônoma eficiente não é estática: ela aprende com cada falha de compilação, gargalo de validação e desvio de escopo.

## Responsabilidades do Evolver

1. **Detecção de Drift de Regras (Rules Drift)**:
   - Identifica se convenções adotadas no dia a dia entraram em conflito com o `AGENTS.md` ou `FACTORY_RULES.md`.
2. **Sintetizador de Novas Skills**:
   - Quando um procedimento manual ou fluxo de comandos é repetido mais de 3 vezes por agentes, esta skill sintetiza um novo pacote em `.agents/skills/<nova-skill>/SKILL.md`.
3. **Auditoria de Custo & Eficiência de Modelos**:
   - Analisa métricas de sucesso por modelo: se tarefas médias estão falhando com modelos menores, o roteador recalibra automaticamente para escalar para modelos de maior capacidade (`claude-3.7-sonnet` ou `deepseek-r1`).
4. **Higienização do Contexto (Ablation)**:
   - Remove regras obsoletas e instruções redundantes que apenas aumentam a janela de contexto sem gerar impacto real.

## Procedimento de Execução

1. **Auditar falhas recentes**:
   Examine `.factory/state.json` buscando tarefas que passaram pelo estado `NEEDS_FIX`.
2. **Avaliar padrões de falha**:
   - Falha de tipo -> Adicionar regra de checagem estrita no `AGENTS.md` ou pré-passo de tipagem no PIV loop.
   - Falha de regressão em E2E -> Adicionar novo cenário na escada do `validation-harness`.
3. **Gerar Relatório de Evolução**:
   Gere `.factory/evolution_report.md` com propostas de melhoria para revisão do desenvolvedor.

---

## 🧠 Continuous Self-Improvement & Failure RCA Integration

1. **Sinergia com `00-continuous-self-improvement`**:
   - Enquanto a skill `00` opera em tempo real no nível da sessão ativa (checkpoint no 2º prompt), o `08-meta-skills-evolver` opera em nível macro/periódico.
   - O evolver consome `.factory/learning/learning_ledger.json`, consolidando múltiplos registros de RCA e preferências do usuário em atualizações estruturais definitivas nas skills.
2. **Destilação de Regras e Prevenção de Inchaço (Bloat)**:
   - Se uma preferência foi reforçada múltiplas vezes, o evolver a promove para regra explícita na skill correspondente.
   - Regras que nunca foram violadas ou que se tornaram óbvias são simplificadas para manter o contexto ágil e eficiente.

