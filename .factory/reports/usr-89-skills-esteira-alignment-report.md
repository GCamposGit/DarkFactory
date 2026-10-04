# Relatório de Conclusão: USR-89 (Skills x Esteira) e USR-96 (Ações do Owner)

## 1. Resumo Executivo
Em conformidade com o Plano de Estabilidade ([`docs/STABILITY_PLAN_2026-09-30.md`](file:///c:/dev/DarkFac/docs/STABILITY_PLAN_2026-09-30.md)), a auditoria e alinhamento completo do catálogo de skills com a linha de produção HF-27 foi concluída com sucesso no ticket **USR-89**. Além disso, as ações exclusivas do Owner catalogadas no ticket **USR-96** foram verificadas e integradas.

---

## 2. Implementações Realizadas (USR-89)

### A. Seção Canônica "Binding com a Esteira (HF-27)"
Cada skill pertencente ao caminho crítico recebeu uma seção formal delimitando seu papel, módulos correspondentes e a fonte única de verdade:
- **`00-continuous-self-improvement`**: Vinculada ao pacote `core.learning` (`core/learning/`) e à etapa de retrospectiva autônoma (`retrospective` na esteira contínua, USR-91).
- **`02-plan-product-architecture`**: Vinculada a `core.line.stage_planning` (`core/line/stage_planning.py`) e `planning.md`. Remoção das referências a arquivos legados inexistentes (`HANDOFF_POLICY.md`, `HYBRID_AUTONOMY_REQUIREMENTS.md`).
- **`03-model-router`**: Vinculada a `core.line.routing` (`core/line/routing.py`) e apontando explicitamente `.factory/config/line_routing.json` como fonte única de verdade da matriz de despacho, respeitando `core.line.agent_cli.HARNESS_CAPABILITIES` (Antigravity somente leitura; Claude, Codex e Grok com escrita).
- **`04-autonomous-piv-loop`**: Vinculada a `core.line.stage_build` (3 iterações padrão), `stage_review` e `stage_integration`. Remoção das referências a arquivos legados inexistentes (`references/remote-delivery.md`, `references/worktree-parallelism.md`), formalizando o uso de `core.line.workspace` e `core.git.autonomy`.
- **`05-validation-harness`**: Vinculada a `core/harness/runner.py` e validação interna de `stage_build`. Reafirmação do portão determinístico único `runner.py --quick`.
- **`06-adversarial-review`**: Vinculada a `core.line.stage_review` (`core/line/stage_review.py`) e reforço da regra de família independente (`other_family_than_development`).
- **`08-meta-skills-evolver`**: Vinculada a `core.evolution` (`core/evolution/engine.py`), substituindo a menção a `.factory/state.json` pelo banco SQLite e catálogo estruturado `.factory/demands/demands.json`.
- **`19-run-ticket`**: Vinculada ao launcher interativo/headless `run_ticket.py`, unificando as regras com a esteira HF-27. Remoção da menção à flag inexistente `--allow-critical-quota` (mantendo apenas `--force`) e ajuste do teto de iterações para 3.

### B. Classificação de Skills Auxiliares
As skills complementares (fora do caminho crítico de entrega de tickets de código) foram anotadas com `"Nota de Operação: Auxiliar (Fora do Caminho Crítico)"` especificando o consumidor real:
- **`09-local-audio-transcription`**: Bots do Telegram e atas de reunião.
- **`10-topic-deep-research`**: Knowledge Ledger (`.factory/research/`).
- **`11-repo-code-scout`**: Knowledge Ledger e mineração de código aberto (`.factory/research/`).
- **`12-daily-model-benchmark`**: Cron diário e painel `/api/benchmarks` do DarkHub.
- **`13-session-learning-pack`**: Sessões interativas e flashcards Feynman ao operador.
- **`14-speculative-model-racing`**: Torneios empíricos e calibração de modelos.
- **`15-anti-slop-content-engine`**: Motor de pureza textual e geração de conteúdo no DarkHub.
- **`16-visual-asset-studio`**: Ilustrações e diagramas de arquitetura no DarkHub.
- **`18-second-brain-knowledge`**: MCP Server e busca na base perpétua Segundo Cérebro.

### C. Sincronização e Endurecimento do Gate
- **Espelho `.claude/skills/`**: Sincronizado integralmente via `python scripts/sync_skills.py` (20 skills idênticas).
- **Gate de Drift (`tests/test_skills_drift.py`)**:
  - `KNOWN_FLAG_DRIFT_ALLOWLIST` zerada (nenhuma exceção de flag necessária).
  - Remoção das exceções de paths inexistentes (`references/*.md` e `state.json`) de `ALLOWLIST_PATH_EXCEPTIONS`.
  - 8/8 testes aprovados.

---

## 3. Confirmação das Ações do Owner (USR-96)
1. **GitHub Ruleset**: `main-requer-ci-verde` ativo, exigindo checks verdes de CI (`pr-validation` Ubuntu/Windows e `trusted-pr-policy`), com bypass para administradores de repositório para atualizações manuais de governança.
2. **MISSÃO**: `MISSION.md` atualizado com o objetivo primordial de fábrica de software autônoma e comitado diretamente no `main` (`e99fbf8`).
3. **Segurança**: Tokens de bot do Telegram rotacionados com sucesso no BotFather, Dokploy e nós locais.
