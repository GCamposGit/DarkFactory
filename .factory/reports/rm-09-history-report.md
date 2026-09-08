# RM-09 — Histórico de snapshots e comparação entre versões

## Contrato PIV

- **Ticket:** RM-09 (`user-demand` do operador, derivado do roadmap aprovado)
- **Objetivo:** preservar versões recentes do snapshot operacional e permitir comparação determinística entre duas versões, mantendo proveniência antes/depois.
- **Owner:** Codex
- **Branch:** `codex/rm-09-history`
- **Worktree:** `C:\dev\DarkFac`
- **SHA base:** `5aa865482b7e4685c4138657d97d9be6af483bd2`
- **Commit de implementação:** `c4568da`
- **SHA final da implementação:** `c4568da`

## Escopo e non-goals

- Contratos Pydantic para resumo de snapshot, histórico e mudanças.
- Retenção local bounded no `RoadmapSnapshotStore`, sem duplicar snapshots com o mesmo hash.
- Comparação por campos estáveis, com objetos `before`/`after` completos para preservar proveniência.
- Superfícies library, CLI (`history`/`compare`) e HTTP (`/roadmap/history` e `/roadmap/history/compare`).
- Associação de relatórios RM como evidência, com invalidação de fingerprint e proteção contra menções editoriais falsas.
- Fora deste incremento: banco de dados, persistência durável entre reinícios, sincronização remota e edição do roadmap.

## Arquivos alterados

- `core/roadmap/models.py`
- `core/roadmap/store.py`
- `core/roadmap/service.py`
- `core/roadmap/sources.py`
- `core/roadmap/cli.py`
- `core/roadmap/__init__.py`
- `hub/backend/api.py`
- `tests/test_roadmap_history.py`
- `docs/ROADMAP_OPERACIONAL.md`
- `.factory/context_intelligence.json`

## Validação

- `python -m pytest tests/test_roadmap_history.py -v` — **4 passed**.
- `python core/harness/runner.py --quick` — **[HARNESS_PASS]**, 340 descobertos, 339 passed, 1 skipped.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` — **339 passed, 1 skipped**.
- `node --check hub/frontend/roadmap.js` — passou.
- `python -m core.benchmarks.cli status` — benchmark diário de 2026-09-07 já existente; nenhuma execução de rede repetida.
- `python core/router/model_router.py recommend ... --offline` — fallback local selecionado após falha de acesso ao adaptador Antigravity.
- `git show --check --oneline c4568da` — passou.

## Evidência do status do roadmap

O snapshot real após a correção de evidência reportou RM-01–RM-08 como `completed`, RM-09 como planejado antes deste relatório, zero bloqueios, zero conflitos e zero issues. Após este relatório, a compilação reporta RM-01–RM-09 como `completed`, sem issues. Este relatório é a evidência direta do RM-09.

## Estado residual e auditoria

- O guardrail determinístico foi bloqueado por `.factory/holdout/df15_factory_vertical.py`, arquivo protegido e pré-existente do trabalho DF-15; não foi modificado.
- O revisor local retornou `REJECT` por não receber os arquivos para inspeção, não por apontar defeito técnico; focal e gates determinísticos foram usados como evidência substituta.
- Permanecem unstaged as mudanças prévias do usuário em `.factory/learning/learning_ledger.json`, `hub/backend/service.py`, `hub/frontend/app.js`, `tests/test_ajustar_link_para_canalle.py` e artefatos DF-15.
