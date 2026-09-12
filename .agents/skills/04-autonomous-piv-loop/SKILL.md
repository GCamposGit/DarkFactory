---
name: autonomous-piv-loop
description: Executa tickets, funcionalidades e correções pelo ciclo Prime-Plan-Implement-Validate (PIV), com protocolo fail-closed para branches e worktrees concorrentes.
---

# 04 - Autonomous PIV Loop: Ciclo de Execução e Entrega

O ciclo PIV divide a entrega em mudanças pequenas, isoladas e estritamente verificáveis: preparar contexto, planejar, implementar uma unidade no executor econômico, validar via testes determinísticos e submeter à revisão independente antes da entrega final.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Contrato de Handoff Aprovado**: `WorkflowHandoff` em estado `WorkflowState.READY_FOR_HANDOFF`.
- **Fencing de Isolamento Git**: Branch dedicada e worktree exclusiva com lease de ownership (`references/worktree-parallelism.md`).
- **Baseline SHA**: Hash do commit base limpo e verificado.
- **Governança**: `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`.

### Ações e Procedimento Executável
1. **Transição para Implementação (`IMPLEMENTING_ECONOMY`)**:
   - Valide a transição legal de estado: `READY_FOR_HANDOFF -> IMPLEMENTING_ECONOMY`.
   - Realize o preflight de terminal com `-NoProfile -NonInteractive -ExecutionPolicy Bypass`.
   - Confirme ausência de estado sujo na árvore de trabalho (`startingState: working-tree` é expressamente proibido).
2. **Implementação Estrita nos Caminhos Permitidos**:
   - Modifique exclusivamente os caminhos declarados em `allowed_paths` do handoff.
   - Respeite `read_only_paths`. Se for necessário alterar contratos públicos ou oráculos, devolva ao planejador qualificado (`NEEDS_REPLAN`).
3. **Validação Determinística Local (`VALIDATING`)**:
   - Transicione para `WorkflowState.VALIDATING`.
   - Execute testes focais da unidade e a suíte rápida:
     ```powershell
     python core/harness/runner.py --quick
     ```
   - Gere registros de `EnvironmentEvidence` associando cada teste executado à sua versão de configuração, build digest e resultado (`PASSED`/`FAILED`).
4. **Encaminhamento para Revisão Independente (`INDEPENDENT_REVIEW`)**:
   - Submeta o candidato com sua suíte verde e evidências completas para revisão adversarial independente (`06-adversarial-review`).

### Outputs Estruturados
- **Código e Commits Seletivos**: Commits isolados com mensagens rastreáveis vinculadas ao ticket.
- **Evidências de Ambiente**: Instâncias de `EnvironmentEvidence` geradas durante a validação.
- **Relatório de Execução da Unidade**: `.factory/reports/<task-slug>-report.md` contendo ticket, branch, SHA base/final, arquivos alterados, contagens de testes, códigos de saída e heartbeat final.
- **Handoff Atualizado**: Objeto pronto para avaliação do portão de revisão.

### Portões, Política e Validação
- **Conformidade com ReadinessGate**: O avanço entre estágios obedece rigorosamente às transições legais (`ALLOWED_TRANSITIONS`).
- **Protocolo Fail-Closed de Worktrees**: Ausência de heartbeat, colisão de arquivos ou branch suja bloqueiam o ciclo imediatamente.
- **Entrega Remota Obrigatória**: Conforme `references/remote-delivery.md`, um ticket de desenvolvimento só é concluído após PR aberta, checks de CI verdes, merge aprovado e confirmação do SHA no `main` remoto.
- **Proibição de Atalhos**: Nenhum artefato é marcado como entregue sem aprovação do `ReadinessGate`.

---

## 2. Continuous Self-Improvement & RCA

- **RCA Antes de Qualquer Retry**: Falhas de validação, timeout, erro de compilação ou rejeição em portão exigem RCA estruturado antes de nova tentativa.
- **Teto de Tentativas**: Duas correções focais sem progresso exigem replanejamento com alta inteligência (`WorkflowState.NEEDS_REPLAN`).

---

## 3. Step V-5 — Hub Sync (obrigatório após HARNESS_PASS)

Após receber `[HARNESS_PASS]` do runner, execute o seguinte passo antes de marcar o ticket como entregue:

```powershell
# Notifica o DarkHub para atualizar benchmarks e fronteiras de Pareto com os novos dados.
# - Se o hub estiver rodando: retorna 200 e confirma atualização.
# - Se o hub não estiver rodando: falha silenciosa, nunca bloqueia a entrega.
Invoke-RestMethod -Method POST `
    -Uri "${env:DARKHUB_URL:-https://darkhub.ggcampos.com}/api/benchmarks/refresh" `
    -ContentType "application/json" `
    -ErrorAction SilentlyContinue
```

**Regras:**
- ✅ Retorno 200 → logar `[HUB] Benchmark refresh OK` e prosseguir.
- ⚠️ Qualquer erro (connection refused, timeout) → logar `[HUB] Hub offline, skipping refresh` e prosseguir normalmente. **Não bloquear entrega.**
- 🔧 Para sobrescrever a URL padrão, definir `DARKHUB_URL` no ambiente (ex.: `https://darkhub.ggcampos.com` ou `http://localhost:8000`).


> **Nota:** O runner.py já executa este refresh automaticamente via `_notify_hub_on_pass()`. Este step existe para harnesses que chamam o runner indiretamente ou orquestram testes sem usar `runner.py` diretamente (ex.: Grok, Claude Code, scripts CI).

