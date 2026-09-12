---
name: adversarial-review
description: Revisa código, contratos e evidências de forma adversarial, com contraexemplos reproduzíveis e revisão independente quando autorizada. Use em auditorias críticas e antes de aprovar integração conforme a política do repositório.
---

# 06 - Revisão Adversarial Orientada ao Contrato

Esta skill opera a auditoria crítica e independente de contratos, código, segurança e evidências antes de autorizar a entrega ou o merge no branch principal.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Candidato Sob Análise**: Digest de build/candidato (`candidate_digest`), diff e manifesto de arquivos.
- **Contrato de Handoff**: Instância de `WorkflowHandoff` em estado `WorkflowState.INDEPENDENT_REVIEW`.
- **Evidências de Validação**: Registros de `EnvironmentEvidence` produzidos pelo harness.
- **Identidade do Implementador**: `SanitizedIdentity` do agente que produziu o código.

### Ações e Procedimento Executável
1. **Reconstrução Crítica do Fluxo de Confiança**:
   - Reconstrua a cadeia: `requisito -> entrada -> decisão -> efeito -> evidência`.
   - Inspecione a API pública em busca de vulnerabilidades clássicas: listas vazias que fingem passar, status autodeclarados, bypass de portões, desserialização insegura e vazamento de credenciais (conforme `safe_export.py`).
2. **Auditoria de Replay e Idempotência**:
   - Verifique se replays com a mesma chave rejeitam efeitos colaterais duplicados e se conflitos de chave falham de forma segura.
3. **Produção de Contraexemplos Mínimos**:
   - Para qualquer falha detectada, elabore um caso de teste mínimo, isolado e reproduzível que demonstre o defeito antes de solicitar correções.
4. **Emissão de Veredito**:
   - `changes_required`: Há achado impeditivo concreto com reprodução e ticket de reparo.
   - `no_actionable_findings_in_reviewed_scope`: Nenhum defeito acionável no escopo examinado.
   - `incomplete`: Dados ou contexto insuficientes para emitir veredito.

### Outputs Estruturados
- **Recibo de Revisão Independente (`EvidenceReceipt`)**:
  - `receipt_id`: Identificador único do recibo.
  - `producer`: `SanitizedIdentity` com `role` pertencente a `{"reviewer", "supervisor", "independent_reviewer"}` e `subject` **obrigatoriamente distinto** do implementador.
  - `subject`: `ticket_id` do handoff.
  - `requirement`: `review` ou `independent_review`.
  - `result`: `EvidenceResult.PASSED`.
  - `mode`: `ValidationMode.TARGET_ENVIRONMENT` (para release) ou `DOCUMENTARY`.
  - `candidate_digest`: Vinculado estritamente ao digest do código examinado.
  - `observed_at`: Timestamp com fuso explícito UTC.
- **Relatório de Auditoria**: Documento com achados, limites de escopo e contraexemplos.

### Portões, Política e Validação
- **Portão de Entrega no ReadinessGate**: O método `ReadinessGate.evaluate(..., target_state=WorkflowState.DELIVERED)` falha de forma fechada (*fail-closed*) se não encontrar um `EvidenceReceipt` válido de revisão independente no `VerificationContext`.
- **Independência Estrita de Papéis**: O revisor NÃO PODE ser o mesmo sujeito que implementou o ticket (`receipt.producer.subject != context.expected_identity.subject`).
- **Validade e TTL**: O recibo expira conforme o `max_age_seconds` definido na `GatePolicy` (padrão 86.400 s) e deve ter sido observado antes de `now` em UTC.
- **Proibição de Autoaprovação**: Flags de auto-revisão no diff do candidato são sumariamente ignoradas pelo gate.

---

## 2. Modelos e Economia de Revisão

- **Nível 1 (Local, Custo $0)**: Execução de pré-revisão com modelo local via Ollama (`gpt-review:latest` ou `qwen-code-deep`) para detecção estática e estrutural.
- **Nível 2 (Fronteira Independente)**: Para auditorias críticas e entregas em produção, despache para modelo de fronteira de família diferente do implementador (`claude-3.7-sonnet`, `deepseek-r1` ou `grok-4.6`).
