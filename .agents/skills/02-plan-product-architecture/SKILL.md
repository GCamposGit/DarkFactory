---
name: plan-product-architecture
description: Produz PRD, decisões de arquitetura e handoffs pequenos a partir de demanda e baseline verificadas. Use ao planejar projetos, módulos ou correções que exigem contratos claros; separa decisões do owner, desenho técnico e implementação econômica.
---

# 02 - Planejamento e Especificação para Execução

O produto do planejamento é um contrato normativo verificável que um implementador econômico consegue executar e um verificador independente consegue auditar. No DarkFac, o planejamento opera sob `HANDOFF_POLICY.md` e `HYBRID_AUTONOMY_REQUIREMENTS.md`, gerando o contrato `WorkflowHandoff`.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Origem da Demanda**: `HandoffOrigin` (`user-demand`, `code-review`, `agent-feature`).
- **Baseline Verificada**: SHA commitado em branch limpa e inventário de caminhos auditados.
- **Contexto e Decisões Históricas**: Resoluções anteriores registradas no Knowledge Ledger ou em tickets predecessores.
- **Governança**: `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`.

### Ações e Procedimento Executável
1. **Intenção e Processamento de Grill (`GrillRecord`)**:
   - Elimine ambiguidades materiais com o menor esforço do usuário: apresente de 1 a 3 perguntas por rodada com alternativas, recomendação e consequência direta.
   - Decisões técnicas dedutíveis devem ser resolvidas pelo próprio planejador.
   - Registre decisões materiais respondidas com sua fonte (`decision_source`), alternativas consideradas e justificativas. Decisões reutilizadas devem referenciar o ID de origem (`reused_decision_ref`).
   - Assuma pressupostos reversíveis explicitamente sem travar o avanço do trabalho.
2. **Definição de Arquitetura e Superfície**:
   - Mantenha o domínio headless, separando regras de negócio de frameworks visuais ou de transporte.
   - Especifique caminhos permitidos (`allowed_paths`) e somente leitura (`read_only_paths`).
   - Declare ferramentas, portas e serviços via `EnvironmentManifest`, garantindo ausência total de credenciais ou tokens em URLs/DSNs (utilize `SecretReference`).
3. **Mapeamento de Requisitos e Dependências**:
   - Especifique cada `EvidenceRequirement` associado ao respectivo estágio de prontidão (`ReadinessState`).
   - Se houver ação humana indispensável, formule um `ManualDependency` estrito com passos numerados consecutivos, comando de probe final (`final_probe`), critério de retomada e estágios bloqueados (`blocked_stages`).
4. **Fatiamento em Tickets**:
   - Cada ticket de execução deve cobrir de 1 a 4 arquivos principais, declarando objetivo, critérios de aceitação e `validate_commands` exatos.

### Outputs Estruturados
- **Contrato de Handoff Normativo**: Instância validada de `core.workflow.contracts.WorkflowHandoff` com:
  - `grill`: `GrillRecord` com `ready_for_spec=True`, sem perguntas pendentes e com critérios de exemplo.
  - `environment`: `EnvironmentManifest` tipado.
  - `required_evidence`: Lista de `EvidenceRequirement`.
  - `manual_dependencies`: Lista de `ManualDependency` (se aplicável).
  - `state`: `WorkflowState.READY_FOR_HANDOFF`.
  - `planner_tier`: `PlannerTier.HIGH`.

### Portões, Política e Validação
- **Avaliação pelo ReadinessGate**: O handoff gerado é submetido a `ReadinessGate.evaluate(handoff, context=context, target_state=WorkflowState.READY_FOR_HANDOFF)`.
- **Exigência de PlanApproval**: O `VerificationContext` deve conter aprovação resolvida (`PlanApproval`) emitida por supervisor qualificado, contendo:
  - `plan_digest` correspondente ao digest exato do plano.
  - `planner_tier` obrigatoriamente `PlannerTier.HIGH` (planejadores `ECONOMY` são rejeitados pelo gate).
  - `approved_by.role` habilitado na política (`GatePolicy.is_role_enabled`).
- **Fail-Closed**: Perguntas pendentes no Grill, decisões materiais sem origem ou ausência de contexto de verificação bloqueiam imediatamente o avanço do workflow.

---

## 2. Modelos e Papéis

- **Planejamento de Alta Inteligência**: Modelos qualificados com raciocínio profundo (`gemini-3.8-flash`, `claude-3.7-sonnet`, `deepseek-r1`, `gpt-6-astra`), com esforço `high` ou `max`.
- **Proibição de Degradação Silenciosa**: O planejador não pode rebaixar requisitos arquiteturais para acomodar limitações de implementadores econômicos.
