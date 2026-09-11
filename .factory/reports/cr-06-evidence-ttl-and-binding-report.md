# Relatório de Conclusão — CR-06: Calcular Validade e Conferir o Sujeito da Evidência

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-06 (HF-04 P1, depende diretamente de CR-05)  
**Achado endereçado:** F02 (confiança cega em rótulo de atualidade e ausência de vinculação estrita de evidências)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `core/workflow/readiness.py`, `core/workflow/verification.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Cálculo Determinístico de TTL em UTC**:
   - Aplicação da regra temporal: `0 <= now - observed_at <= max_age` em UTC com relógio injetado (`context.now`).
   - `max_age` provém da política (`GatePolicy.get_max_age(requirement)`), suportando parametrização focal por requisito e fallback seguro para `default_max_age_seconds: int = 86400` (24 h).
   - Rejeição de timestamp no futuro (`now - observed_at < 0`) com motivo detalhado e estruturado.
   - Rejeição de timestamp expirado (`now - observed_at > max_age`) com relatório de idade observada vs limite configurado.
   - Aceite no limite exato (`age == max_age`) e rejeição determinística exatamente um instante além do limite (`age == max_age + 1`).
   - O rótulo `freshness=current` autodeclarado pelo candidato é explicitamente ignorado para autorização de avanço, não podendo mascarar uma evidência expirada.
   - Valores ausentes não recebem defaults que os façam atuais.

2. **Vinculação Estrita Multidimensional**:
   - **Sujeito (Ticket)**: `receipt.subject` deve conferir estritamente com `handoff.ticket_id`. Divergência bloqueia o gate.
   - **Plano**: `receipt.plan_digest` deve conferir com `context.plan_digest`.
   - **Candidate / Build Digest**: `receipt.candidate_digest` (com normalização bidirecional de alias `build_digest`) deve conferir com `context.candidate_digest`. Em entrega (`DELIVERED`), prova operacional requer digest do candidato obrigatoriamente vinculado.
   - **Configuração**: `receipt.config_version` deve conferir com `context.config_version`. Alteração de versão de configuração invalida receipt anterior mesmo dentro do TTL.
   - **Ambiente**: `receipt.environment_ref` deve conferir tanto com `context.expected_environment_ref` quanto com `handoff.environment.environment_ref`.
   - **Rota**: `receipt.route` (quando informado) deve conferir com `context.expected_route`.
   - **Papel do Produtor**: `receipt.producer.role` deve pertencer a `context.policy.enabled_roles`.
   - **Capacidades Autorizadas**: Qualquer capacidade declarada em `receipt.capabilities` deve estar contida nas `approval.enabled_capabilities` do plano aprovado.
   - **Hash do Artefato**: `receipt.artifact_hash` não pode ser vazio e é sanitizado contra vazamento de segredos.

3. **Revisão Independente e Provas em Entrega**:
   - O loop de entrega (`DELIVERED`) valida que receipts de revisão independente estejam estritamente vinculados ao sujeito (`receipt.subject == handoff.ticket_id`) e dentro do TTL da política (`rev_age <= rev_max_age`).

4. **Isolamento de Negativas em Testes**:
   - Cada dimensão negativa é testada contra um controle positivo plenamente válido e aprovado, garantindo que não existam falsos verdes provocados por falhas compartilhadas de autorização.
   - Todas as mensagens de erro contêm IDs estruturados (`req_id`, `ticket_id`, etc.) e censuram segredos sintéticos.

---

## 2. Arquivos Modificados

| Arquivo | Mudança |
|---|---|
| `core/workflow/verification.py` | Adicionados `build_digest`, `route` e `capabilities` em `EvidenceReceipt` com normalização de dados e checagem de segredos; adicionado `default_max_age_seconds: int = 86400` em `GatePolicy` com método `get_max_age`. |
| `core/workflow/readiness.py` | Implementada checagem determinística de TTL em UTC (`0 <= now - observed_at <= max_age`) e vinculação estrita de sujeito, rota, build digest, ambiente, versão de configuração e capacidades autorizadas; aplicada regra temporal e de sujeito no receipt de revisão de entrega. |
| `tests/test_workflow_contracts.py` | Alinhada fixture de simulação (`make_simulation_context`) com `subject="HF-04-01"` e `route=expected_route`. |
| `tests/test_workflow_verification.py` | Adicionados 14 testes cobrindo todas as dimensões de TTL, limites exatos, data futura, `freshness=current`, sujeitos divergentes, rotas, digests, ambientes, configurações, papéis, capacidades e sanitização de segredos. |

---

## 3. Validação Executada

```powershell
python -m pytest tests/test_workflow_verification.py tests/test_workflow_contracts.py -v
```

Resultado:
```
48 passed in 0.15s (100% PASS)
```

Testes de regressão da revisão crítica (`.factory/reviews/hf-critical-20260909/test_gate_regressions.py`):
```
10 passed, 2 failed (os 2 failures referem-se estritamente ao ticket pendente CR-08)
```
