# USR-300 (e USR-244): CI mais rapida

Os caminhos `.github/*` e `core/harness/*` sao protegidos: o agente nao pode commita-los. A parte protegida
esta em `docs/proposals/USR-300-ci-speedup.patch` (altera `.github/workflows/ci.yml` e
`core/harness/runner.py`). O restante (nao protegido) ja vai no PR normal: `core/testing/shards.py`,
`scripts/ci_paths.py`, `tests/test_ci_shards_usr300.py`, ledger e OA-039.

## O que muda

1. **Shards no Windows (N=4)**. `runner.py --quick --shard I/N` (1-based) troca o alvo `tests` dos passos de
   pytest pela lista de arquivos do shard (`core/testing/shards.py`: hash SHA-256 estavel do caminho POSIX,
   mod N; respeita `--ignore`). Uniao dos shards = todos os arquivos, intersecao vazia (teste
   `test_partition_covers_all_files_exactly_once`). `--shard` forca `--no-cache --local`; shard sem teste no
   passo serial (pytest exit 5) conta como passe. Shards nao compartilham escrita (cada um em sua VM).
   Ubuntu continua sem shard.
2. **Filtro por caminho** (job `changes`, so em `pull_request`, usa `scripts/ci_paths.py` da base confiavel).
   Leve (pula os jobs pesados) somente se TODOS os arquivos estiverem em: `.factory/demands/**`,
   `.factory/owner_actions/**`, `docs/**`, `*.md` na raiz (exceto AGENTS.md, CLAUDE.md, FACTORY_RULES.md,
   MISSION.md). `.factory/config/**`, `.factory/holdout/**`, workflows, harness e qualquer outro caminho sao
   pesados; lista vazia ou falha de diff = pesado. Escolha deliberada: `*.md` aninhados (ex. SKILL.md) nao sao
   leves, pois skills/testes podem le-los. Pushes na main NAO sao filtrados (deploy segue o main-validation).
   O guard roda sempre (no job `changes` e nos jobs pesados; `trusted-pr-policy` inalterado).
   Caminho leve (USR-244): job `ledger-fast` roda os testes de ledger
   (`test_demands*.py`, `test_demand_id_allocator.py`, `test_ledger_hygiene.py`,
   `test_owner_actions_core.py`, `test_owner_actions_conflict_merge.py`).
3. **concurrency**: grupo por evento + numero do PR (ou ref); `cancel-in-progress` so em
   `pull_request`/`pull_request_target`; push na main nunca cancela.

## Nomes de checks (importante)

Os jobs pesados agora se chamam `pr-tests (...)` e `main-tests (...)`. Para manter os checks obrigatorios, dois
jobs agregadores sempre executados (`if: always()`) tem exatamente os nomes antigos:
`pr-validation (ubuntu-latest)` e `pr-validation (windows-latest)`; falham se o job pesado (ou o
`ledger-fast`, no caminho leve) nao for `success`; jobs pulados contam como ok. Mantenha obrigatorios esses
dois nomes e o do `trusted-pr-policy`; NAO exija `pr-tests (...)` (nomes variam). Na push, o job
`main-validation` virou agregador de `main-tests` (4 shards Windows + Ubuntu), entao
`continuous-deployment` (`needs: main-validation`) so roda com todos verdes. Bootstrap: o PR/commit que
aplica o patch tem que ir por push direto (a base ainda nao tem `ci_paths.py`/`shards.py`).

## Como aplicar

```powershell
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac worktree add C:\dev\DarkFac\.worktrees\publish-usr300 -b publish/usr-300 origin/main
git -C C:\dev\DarkFac\.worktrees\publish-usr300 apply --check docs\proposals\USR-300-ci-speedup.patch
git -C C:\dev\DarkFac\.worktrees\publish-usr300 apply docs\proposals\USR-300-ci-speedup.patch
```

Depois siga o OA-039 (guardas rapidos, portao `runner.py --quick` com `[HARNESS_PASS]`, push, `gh run list`).

## Como validar

- `python -m pytest -q -p no:cacheprovider tests/test_ci_shards_usr300.py tests/test_clock_bomb_guard.py tests/test_harness_contract.py tests/test_hub_coverage.py tests/test_safe_git_state.py`
- `python C:\dev\DarkFac\core\harness\runner.py --quick --shard 1/4` (e 2/4..4/4): soma dos `[TEST_COUNT]` = execucao completa.
- PR so de docs: CI < 2 min; PR de codigo: <= 8 min; main: sem cancelamento.

## Tempos esperados

Windows ~1.069 s em 1 job -> ~270-350 s por shard + ~90 s de setup (~6-7 min), desbalanceamento por hash pode
adicionar ao pior shard; Ubuntu ~173 s + setup. Caminho leve: ~1-1,5 min (checkout, pip com cache, 7 arquivos
de teste). Se o pior shard passar de 8 min, o proximo passo e balancear por duracao (pesos), fora deste escopo.
