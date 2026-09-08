# DF-23 — Protocolo de paralelismo com worktrees

## Identidade

- Owner: task Codex DF-23
- Branch: `codex/df-23-worktree-protocol`
- Worktree: `C:\Users\guigc\.codex\worktrees\f500\DarkFac`
- SHA-base: `de6442408427adaaf336a122c6dd7c4dbe2e9311`

## Escopo entregue

- Skill canônica e referência fail-closed WT-01 a WT-12.
- Espelho `.claude` gerado por `python scripts/sync_skills.py`.
- Teste determinístico de controles, drift e unicidade do DF-23 no roadmap.
- Critérios do DF-23 no roadmap, sem iniciar automação funcional.
- RCA sistêmico e RCAs das falhas observadas no retry.

## Validação

- `python -m pytest tests/test_worktree_protocol.py -v`: 1 passed, exit code 0.
- Skill Creator `quick_validate.py`: skill válida, exit code 0.
- Diff canônico versus espelho: vazio, exit code 0.
- `git diff --check`: exit code 0.
- `python core/harness/runner.py --quick`: `[HARNESS_FAIL]`, exit code 1;
  99 passed, 1 skipped e 2 falhas preexistentes no SHA-base.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py`: exit code 1;
  99 passed, 1 skipped e 2 falhas preexistentes no SHA-base.

As falhas residuais são `test_api_list_benchmark_domains` (espera 6 domínios,
implementação retorna 8) e `test_analyzer_fallback` (espera ao menos 2 padrões,
implementação retorna 1). Seus módulos não foram alterados para preservar o escopo.

O SHA final e o `git status` pós-commit são emitidos no handoff, pois incluir o hash
do próprio commit dentro dele criaria uma referência circular.
