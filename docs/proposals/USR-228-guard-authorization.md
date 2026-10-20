# USR-228 - autorizacao verificavel de evolucao protegida (SQ-03)

## O que
`is_governance_evolution_authorized` em `core/orchestrator/guard.py` validava so a flag `DARKFAC_ALLOW_GOVERNANCE_EVOLUTION`. O patch `USR-228-guard-authorization.patch` troca isso por uma prova verificavel; o helper `core/governance/authorization.py` (nao protegido, ja neste branch) implementa a verificacao.

## Esquema
Registro `.factory/governance/authorizations/<id>.json` (campos exatos, `extra=forbid`):
`id`, `ticket_id`, `approver` (somente `owner`), `paths` (caminhos exatos, sem glob), `candidate_kind` (`tree` ou `commit`), `candidate` (hash de `HEAD^{tree}` ou SHA do commit candidato), `expires_at` (ISO 8601).

Regras (fail-closed, qualquer duvida bloqueia):
1. O registro e lido da BASE CONFIAVEL (`origin/main`, ou `--trusted-base <ref>`) via `git ls-tree`/`git show`, nunca da arvore do candidato. O agente de build so escreve o candidato, entao nao emite a propria autorizacao; o owner publica o registro na main (push direto/bypass admin, como os demais protegidos).
2. Todos os caminhos protegidos alterados precisam constar em `paths`; glob e `..` sao rejeitados.
3. `candidate` precisa igualar a arvore/commit do `HEAD` limpo (arvore suja bloqueia).
4. `expires_at` no futuro; aprovador na lista do owner; ticket presente.
5. A flag de ambiente sozinha nunca autoriza. `core/governance/*` passa a ser protegido.

Alternativa descartada: assinatura com segredo do owner (nao existe segredo owner-only no repositorio; a presenca na base ja exige permissao de escrita do owner).

Limite conhecido: no CI (`pull_request_target`) o candidato e o merge ref; o tree hash so casa se a base nao avancou. Se avancar, o owner reemite o registro. O guard base-owned roda do checkout `trusted`, cujo `core/governance` e importado via `sys.path` do proprio arquivo do guard; o CI exige `origin/main` resolvivel no checkout do candidato (`fetch-depth: 0` ja o garante) - sem ele o guard bloqueia.

## Aplicar
Worktree limpa de `origin/main`, depois:
`git apply --check docs/proposals/USR-228-guard-authorization.patch` e `git apply docs/proposals/USR-228-guard-authorization.patch`, commit local, portao `python C:\dev\DarkFac\core\harness\runner.py --quick`, push direto pelo owner (OA-045). Rollback: reverter o commit.

## Validar
`python -m pytest tests/test_governance_authorization.py tests/test_governance_guard.py tests/test_ci_policy.py -q`.
Negativos cobertos: flag isolada, registro ausente, registro so no candidato, expirado, outro candidato, outros caminhos, caminhos parciais, glob, aprovador nao-owner, campos extras, arvore suja, base ilegivel.
