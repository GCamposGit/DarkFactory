# Relatório de Conclusão: USR-93

**Ticket**: USR-93  
**Título**: Aceite V1-V4 com evidencia automatica e visivel: probe de aceite, status do canario no DarkHub e HF-27 sai de validating  
**Status**: `completed`  
**Data**: 2026-10-05  

---

## 1. Contexto e Problema

O marco `HF-27` (Autonomous Production Line) permaneceu em estado `validating` no manifesto do roadmap (`.factory/roadmap/darkfac.json`) por falta de um mecanismo de verificação determinístico e automatizado dos quatro critérios centrais de aceite definidos na Seção 8 do `docs/PRODUCTION_LINE_PLAN_2026-09-22.md`:
- **V1**: Entrega ponta a ponta sem intervenção humana além do intake/grill;
- **V2**: Canário diário contínuo com streak verde >= 7 dias;
- **V3**: Capacidade de failover, degradação graciosa e respeitos às cotas;
- **V4**: Adoção greenfield e decomposição de marcos via `core.adoption`.

Além disso, o estado do canário (streak e último relatório) residia apenas no volume da VPS/relatórios de disco locais, e o DarkHub não possuía rota ou superfície de interface para expor a saúde da linha e os critérios de aceite para o proprietário.

---

## 2. Solução Implementada

1. **Probe Autônomo de Aceite HF-27 (`core/line/acceptance.py`)**:
   - Desenvolvido o módulo `core.line.acceptance` com verificações programáticas para os critérios V1 a V4:
     - `verify_v1(store)`: inspeciona o control store em busca de runs entregues com evidência de merge/PR comprovada;
     - `verify_v2(reports_dir, min_streak)`: calcula o `green_streak` a partir dos relatórios diários do canário;
     - `verify_v3()`: simula despacho com restrição de escrita, failover de rota e expiração de espera;
     - `verify_v4()`: valida os contratos de `core.adoption` (`ProjectKind.GREENFIELD`, `plan_adoption`, `apply_adoption`, `verify_adoption`).
   - Implementada a função `reconcile_roadmap_manifest`, que atualiza o item `HF-27` em `.factory/roadmap/darkfac.json` com `delivery_status: "completed"` e anexa referências de evidência estruturadas (`report:HF-27:line-acceptance`).
   - CLI integrada para operadores e automações: `python -m core.line.acceptance verify` e `python -m core.line.acceptance status`.

2. **Backend do DarkHub (`hub/backend/models.py`, `service.py`, `api.py`)**:
   - Criado o contrato Pydantic `LineStatusResponse` em `hub/backend/models.py` contendo `canary_streak`, `last_canary_report`, `active_runs_count`, `active_runs`, `nodes` e `acceptance`.
   - Adicionado o método `get_line_status` em `HubService` integrando dados do canário, control store, infraestrutura e relatórios de aceite.
   - Exposta a rota HTTP read-only `GET /api/line/status` em `hub/backend/api.py`.

3. **Superfície DarkHub e Governança de Cobertura (`hub/frontend/`, `hub/coverage.json`)**:
   - Mapeada a rota `GET /api/line/status` para a superfície `"line-status-card"` em `hub/coverage.json`.
   - Inserida a seção `#line-status-card` em `hub/frontend/index.html` com badges em tempo real para streak do canário, status dos critérios V1-V4 e atalho para a esteira ao vivo.
   - Implementado o consumidor assíncrono `loadLineStatus()` em `hub/frontend/tasks.js`.
   - Validação com `python scripts/hub_coverage.py`: 100% de cobertura de superfícies mantida.

4. **Suíte de Testes Automatizada (`tests/test_line_acceptance.py`)**:
   - 9 testes cobrindo todo o ciclo:
     - `test_verify_v1_without_runs`: comportamento com store vazio;
     - `test_verify_v1_with_succeeded_run`: validação de evidência de PR/merge em runs concluídos;
     - `test_verify_v2_canary_streak`: cálculo de streaks e verificação de limiar;
     - `test_verify_v3_failover_and_cooldown`: restrição de capabilities e failover;
     - `test_verify_v4_adoption`: contratos da gateway de adoção;
     - `test_run_acceptance_verification_and_save`: geração de relatórios JSON e Markdown;
     - `test_reconcile_roadmap_manifest`: atualização idempotente de `HF-27` no roadmap;
     - `test_hub_get_line_status`: integridade da resposta no serviço do Hub;
     - `test_api_get_line_status`: endpoint REST via TestClient FastAPI.

---

## 3. Evidências de Teste e Validação

- `tests/test_line_acceptance.py`: 9/9 testes passando.
- `scripts/hub_coverage.py`: 141 surfaced, 0 pending, 18 waived (100% de cobertura).
- `python -m core.line.acceptance verify`: relatórios gerados em `.factory/reports/line-acceptance/`.
