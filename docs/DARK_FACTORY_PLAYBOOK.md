# Playbook de Operação da Dark Factory (Nível 3)

Este manual descreve o procedimento operacional para rodar a Dark Factory no dia a dia, desde a criação de um épico até o auto-merge em produção.

---

## 1. Como Cadastrar uma Nova Demanda (Issue/Épico)

1. **Defina a Intenção no PRD**:
   Utilize a skill `plan-product-architecture` para estruturar a demanda.
   Certifique-se de preencher a lista de **Non-Goals** no `MISSION.md`.
2. **Cadastre a Tarefa no Estado Autônomo**:
   Adicione a tarefa ao `.factory/state.json` com status `TRIAGED`:
   ```json
   {
     "id": "TASK-001",
     "title": "Implementar endpoint headless de cálculo",
     "status": "TRIAGED",
     "priority": 1
   }
   ```

---

## 2. A Sequência de Execução Autônoma

O orquestrador (`core/orchestrator/state.py next`) despacha a tarefa respeitando a ordem inegociável de prioridade:
1. **Consertar PRs pendentes** (`NEEDS_FIX`).
2. **Validar PRs aguardando teste** (`VALIDATING`).
3. **Implementar tarefas planejadas** (`PLANNED`).
4. **Triar novas tarefas** (`TRIAGED`).

---

## 3. O Loop por Tarefa (PIV)

Para cada tarefa:
1. **Branch Limpa**: `git checkout -b feature/<task-id>`
2. **Implementação**: O modelo selecionado pelo `core/router/model_router.py` escreve o código e o teste associado.
3. **Validação Rápida**: `python core/harness/runner.py --quick`
4. **Validação Completa**: `python core/harness/runner.py | python core/harness/markers.py`
5. **Auditoria Local Nível 1**: `python core/router/model_router.py call-local --model gpt-review:latest --prompt "[DIFF]"`
6. **Auditoria Cruzada Nível 2**: Modelo independente avalia conformidade de segurança.
7. **Portão de Proteção**: `python core/orchestrator/guard.py HEAD`
8. **Auto-Merge**: Se todos os passos retornarem código 0 (`PASS`), o PR é mesclado automaticamente na branch principal (`main`).

---

## 4. O Que Fazer em Caso de Falhas

- Se o `core/harness/runner.py` falhar: A tarefa entra em `NEEDS_FIX`. O orquestrador aciona um subagente com contexto limpo contendo apenas o erro e o diff para correção imediata.
- Se o `core/orchestrator/guard.py` falhar: O agente tentou modificar governança. A execução é congelada e o desenvolvedor é notificado.
