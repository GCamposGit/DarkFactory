# Relatório de Execução: USR-15 - Adicionar Cards de Infraestrutura no Hub

- **Ticket**: `USR-15`
- **Título**: Adicionar cards de infraestrutura no Hub
- **Origem**: `user-demand`
- **Owner**: `antigravity-orchestrator`
- **Data / Horário**: 2026-09-08T04:27:30Z
- **Status do Ticket**: `published_pending_merge`
- **Branch**: `codex/usr-15-infra-cards`
- **Worktree**: `C:\dev\DarkFac`
- **SHA-base do PR**: `6cc056e0e28e0312c2195cac10b473e4c865e250` (`origin/main`)
- **SHA-base efetivo da branch**: `8dfd74955d13810324a45985a039cbf7e9cc8781`
- **PR**: https://github.com/GCamposGit/DarkFactory/pull/3
- **SHA publicado**: `fccde4d5e264380d40b8941b5ef5b8a7d413eaae`

---

## 1. Escopo e Objetivos Atingidos

1. **Modelagem Headless (`core/infra/cards.py`)**:
   - Criação dos modelos Pydantic v2 `InfraCard`, `InfraCardsReport`, `InfraLink` e `InfraServiceSummary`.
   - Função determinística `build_infra_cards_report(inventory)` que mapeia os nós e extrai links diretos de sistemas oficiais.
   - Probing de conectividade e liveness (`probe_liveness`) com fallback fail-safe para tolerância a falhas sem introduzir flakiness nos testes.

2. **Endpoints REST no Hub (`hub/backend/api.py` e `hub/backend/service.py`)**:
   - `GET /api/infra/cards`: listagem dos cards para o Hub.
   - `POST /api/infra/cards/refresh`: probe de conectividade e atualização.
   - `GET /api/infra/cards/{node_id}`: consulta direta de nó específico.

3. **Interface Responsiva no Hub (`hub/frontend/infra.js` e `hub/frontend/index.html`)**:
   - Injeção dinâmica da seção `#infrastructure-cards-section` no cockpit.
   - Badges de papel (Workstation, On-Premises, Cloud VPS, Edge Gateway, Managed Service) e status operacional.
   - Especificações de hardware (CPU, RAM, GPU) e rede (Tailscale IP, IPv4, FQDN).
   - Links testados de acesso direto aos sistemas oficiais (Dokploy PaaS, Tailscale Admin, Google Remote Desktop, Cloudflare Dashboard, Hostinger, Ollama).
   - Botão de atalho "🖥️ Infra" na barra de navegação superior.

4. **Conformidade com Non-Goals**:
   - Operação estritamente somente leitura no Hub.
   - Zero acoplamento de gestão/mutação no frontend.

---

## 2. Evidência Determinística de Validação

### Teste Focal de Reachability
```powershell
python -m pytest tests/test_adicionar_cards_de_infrae.py -v
```
**Resultado**: 10 passed em 0.83s (100% de sucesso).
- Validação de modelos Pydantic v2
- Integridade e validade de todos os links de sistemas oficiais
- Respostas e contratos da API REST (`GET`, `POST /refresh`, `GET /{node_id}`)
- Resiliência e degradação suave de probe offline
- Presença e integração dos scripts e seletores no frontend
- Bloqueio de rotas de mutação (non-goals)

### Harness Quick Runner
```powershell
python core/harness/runner.py --quick
```
**Resultado**: `[HARNESS_PASS]`, 385 passed, 1 skipped.

### Suíte Geral Pytest
```powershell
python -m pytest tests -v --ignore=tests/test_canaletto.py
```
**Resultado**: 385 passed, 1 skipped em 27.91s.

---

## 3. Arquivos Envolvidos

- `core/infra/cards.py` (novo)
- `core/infra/models.py` (campos de rede pública necessários ao contrato dos cards)
- `core/infra/inventory.py` e `.factory/infra/inventory.json` (inventário operacional exibido)
- `hub/backend/service.py` (modificado)
- `hub/backend/api.py` (modificado)
- `hub/backend/main.py` (headers anti-cache para atualização do frontend)
- `hub/frontend/infra.js` (novo)
- `hub/frontend/index.html` (modificado)
- `tests/test_adicionar_cards_de_infrae.py` (novo)
- `tests/test_infra.py` (ajuste dos IDs do inventário)
- `.factory/demands/demands.json` (modificado)
- `.factory/reports/usr-15-report.md` (este relatório)

Arquivos locais de outras frentes foram preservados fora deste PR: `.factory/infra/roadmap.*`,
`.factory/infra/decisions/ADR-002-vps-and-paas-orchestration.md`,
`.factory/learning/learning_ledger.json` e `tests/test_roadmap_scale.py`.

## 4. Estado de validação e handoff

- `python -m pytest tests/test_adicionar_cards_de_infrae.py -v` — 10 passed, exit code 0.
- `python core/harness/runner.py --quick` — `[HARNESS_PASS]`, 385 passed, 1 skipped, exit code 0.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` — 385 passed, 1 skipped, exit code 0.
- `git diff --check` — passed antes do staging.
- PR #3 — `OPEN`, base `main`, estado de merge `CLEAN`.
- Checks remotos — nenhum check reportado pelo GitHub.
- SHA publicado verificado remotamente — `fccde4d5e264380d40b8941b5ef5b8a7d413eaae`.
- Estado residual local — preservadas fora do PR as alterações concorrentes de USR-16/17/18,
  worker on-premise, ledger de aprendizado, roadmap/ADRs de infraestrutura e teste de escala.
