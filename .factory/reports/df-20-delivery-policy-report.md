# DF-20 — Delivery policy and GitHub integration

## Identidade e escopo

- Ticket: `DF-20` (`user-demand`)
- Owner: Codex
- Branch: `codex/df-20-delivery-policy`
- Worktree: `C:\\dev\\DarkFac`
- SHA-base: `66c0ec80bfe53dd11e5d164f8e8c5a623936ec21`
- SHA-final (implementation commit): `30ebbd7e472f3c84536a7f15f5b22df7b8f534a7`
- Canaletto: fora do escopo; nenhum arquivo do experimento foi alterado.

## Entrega

- `core/integrations/github.py`: adaptador GitHub read-only com transporte injetável, normalização de PR/check-runs, validação de SHA e erros sem vazamento de token.
- `core/orchestrator/delivery.py`: `DeliveryPolicy` fail-closed para identidade da PR, risco, mergeabilidade e checks no SHA exato; `MergeQueue` persistível com fingerprint e idempotência.
- `tests/test_delivery_policy.py`: 12 testes focais cobrindo checks stale, ausência/falha, divergências de SHA, risco, draft/estado, replay/conflict, persistência e transporte offline.
- `docs/AUTONOMY_POLICY.md`: classes de risco, regras de elegibilidade, merge queue, idempotência e limites de escopo.

## Validação

| Comando | Resultado | Exit code |
| --- | --- | ---: |
| `python core/harness/terminal_env.py --check` | `[TERMINAL_ENV_PASS]` | 0 |
| `python -m pytest tests/test_delivery_policy.py tests/test_context_policy.py tests/test_agent_evals.py tests/test_orchestrator_state.py tests/test_factory_vertical.py tests/test_ci_policy.py -v` | 49 passed | 0 |
| `python core/harness/runner.py --quick` | `[HARNESS_PASS]`; 372 passed, 1 skipped; 373 descobertos | 0 |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | 372 passed, 1 skipped; 373 descobertos | 0 |
| `git diff --check` | sem erros; apenas avisos preexistentes de normalização CRLF | 0 |

## RCA / decisões

- `rca_delivery_01`: a primeira rodada focal falhou por fixtures que confundiam `candidate_sha` com `head_sha`, trocavam checks vazios pelos defaults e não recalculavam checks ao simular novo head. Os helpers foram corrigidos antes dos gates finais.
- Checks são válidos somente quando `status=completed`, `conclusion=success` e `head_sha` coincide exatamente com o candidato.
- Classes C/D nunca entram na fila automática; PR draft, fechada, não mergeável ou com mergeabilidade desconhecida são bloqueadas.
- Repetição da mesma chave/fingerprint retorna o mesmo `queue_id`; reutilização da chave com pedido diferente falha fechado.

## Estado residual

Preservadas fora do commit do DF-20:

- `.factory/demands/demands.json`
- `.factory/infra/decisions/ADR-002-vps-and-paas-orchestration.md`
- `.factory/infra/inventory.json`
- `.factory/infra/roadmap.json`
- `.factory/infra/roadmap.md`
- `.factory/learning/learning_ledger.json` (registro do ciclo de autoaperfeiçoamento)
- `core/infra/inventory.py`
- `core/infra/models.py`
- `tests/test_infra.py`
- `tests/test_roadmap_scale.py`

Entrega remota permanece pendente de autorização explícita para publicar código no GitHub.
