---
name: continuous-self-improvement
description: Reavalia preferências, incidentes e falhas observadas em correções, rejeições de portão ou marcos de transição; transforma evidência em melhorias delimitadas de skills e verifica sua eficácia sem flexibilizar regras ou confundir testes de forma com sucesso do produto.
---

# 00 - Melhoria Contínua Orientada a Evidências

Esta skill orienta a evolução contínua da fábrica de software com base em evidências verificáveis de execução, incidentes, regressões e preferências expressas pelo usuário. O ciclo é estritamente orientado a eventos e marcos operacionais, sem depender de contagem de turnos ou esperas artificiais por prompts humanos.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Relatórios de Portão e Falha**: `ReadinessReport` emitido por `ReadinessGate.evaluate()`, contendo `reasons`, `missing_evidence` ou `blocking_dependency_ids`.
- **Logs de Execução**: Registros de erro em `.factory/test_logs/<ticket>/` ou saídas do `validation-harness`.
- **Eventos de Disparo (`trigger_events`)**:
  - Rejeição ou bloqueio em portões determinísticos (`FAILED_VALIDATION`, `NEEDS_REPLAN`, `BLOCKED_POLICY`).
  - Feedback corretivo explícito fornecido pelo usuário.
  - Conclusão de marco arquitetural relevante ou entrega de ticket.
- **Contexto Existente**: Histórico consolidado em `.factory/learning/learning_ledger.json`.

### Ações e Procedimento Executável
1. **Isolamento de Causa Raiz (RCA)**:
   - Identifique resultado esperado, observado e fonte confiável. Separe bug reproduzido, lacuna de especificação, restrição de ambiente e preferência do owner.
   - Execute RCA causal determinístico: diagnostique o mecanismo real com suporte factual via `python -m core.learning.cli rca`. Não invente causas para cumprir formato.
2. **Desenho da Menor Mudança Delimitada**:
   - Aplique a menor intervenção na skill ou módulo responsável. Consolide instruções contraditórias sem adicionar regras concorrentes.
   - Preserve contexto, contratos de etapa e fronteiras de autorização.
3. **Validação em Caso Independente**:
   - Valide sintaxe, referências e scripts afetados.
   - Quando o risco justificar, avalie a skill com pedido realista e artefatos mínimos sem expor a resposta esperada.

### Outputs Estruturados
- **Registro no Knowledge/Learning Ledger**: Entrada tipada em `.factory/learning/learning_ledger.json` contendo `domain`, `mechanism`, `root_cause`, `evidence_ref` e `reversal_path`.
- **Patch de Instrução ou Skill**: Atualização canônica em `.agents/skills/` com histórico de versão e proveniência auditável.
- **Relatório de Eficácia**: Evidência observável demonstrando que a correção resolveu o mecanismo sem regressão de contratos.

### Portões, Política e Validação
- **Inviolabilidade do ReadinessGate**: Nenhuma melhoria de skill pode enfraquecer, contornar ou flexibilizar os portões normativos de `core.workflow.readiness` ou `core.workflow.verification`.
- **Arquivos Protegidos**: Proibida alteração autônoma de `MISSION.md`, `FACTORY_RULES.md` e `FACTORY_GOVERNANCE.md`.
- **Critérios de Eficácia Real**:
  - Testes sintéticos de presença textual não comprovam eficácia de raciocínio.
  - Regressões devem ser atestadas por entradas/saídas observáveis pelo oráculo independente.
  - Se uma garantia não puder ser provada deterministicamente, reduza a declaração ao fato observado.

---

## 2. Persistência e Espelhamento

- No DarkFac, `.agents/skills/` é a fonte canônica.
- Toda modificação deve ser sincronizada deterministicamente para `.claude/skills/` via `python scripts/sync_skills.py`.
- O CLI oficial de aprendizagem pode ser inspecionado com `python -m core.learning.cli --help`.
